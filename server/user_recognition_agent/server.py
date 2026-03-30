"""
WebSocket server for Rokid Glasses (stream-only).

Glasses send H.264 video (0x03) and audio from the Rokid camera/mic. Audio may be
AAC (0x04) or PCM (0x01). The server buffers both streams and every 10 seconds
muxes them into a single MP4 in intermediate_data/ (video_*.mp4).
"""

import asyncio
import logging
import os
import subprocess
import tempfile
import time
import wave
from pathlib import Path
from datetime import datetime
from threading import Lock
from typing import Optional, Tuple

import cv2
import numpy as np
import websockets
from aiohttp import web
from dotenv import load_dotenv

import json as json_module
from .database import init_db, add_person, get_all_persons
from .recognition import extract_embedding, match_face
from .decoder import extract_frame_from_h264
from .transcription import transcribe_and_extract

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

import face_recognition

# ---------------------------------------------------------------------------
# Face Recognition Setup
# ---------------------------------------------------------------------------
KNOWN_FACES_DIR = Path(__file__).resolve().parent / "known_faces"
KNOWN_FACE_ENCODINGS = []
KNOWN_FACE_NAMES = []

def load_known_faces():
    """Load reference images from known_faces directory to use in face recognition."""
    KNOWN_FACES_DIR.mkdir(parents=True, exist_ok=True)
    log.info(f"Loading known faces from {KNOWN_FACES_DIR} ...")
    loaded_count = 0
    for filename in os.listdir(KNOWN_FACES_DIR):
        if filename.lower().endswith(('.jpg', '.jpeg', '.png')):
            filepath = KNOWN_FACES_DIR / filename
            name = os.path.splitext(filename)[0]
            try:
                img = face_recognition.load_image_file(str(filepath))
                encodings = face_recognition.face_encodings(img)
                if encodings:
                    KNOWN_FACE_ENCODINGS.append(encodings[0])
                    KNOWN_FACE_NAMES.append(name)
                    loaded_count += 1
                    log.info(f"Loaded known face: {name}")
                else:
                    log.warning(f"No face found in {filename}")
            except Exception as e:
                log.warning(f"Error loading {filename}: {e}")
    log.info(f"Finished loading {loaded_count} known faces.")

def run_face_recognition_sync(bgr_frame: np.ndarray) -> list[str]:
    """
    Run face recognition synchronously on a BGR frame.
    Returns a list of names for recognized faces.
    """
    if not KNOWN_FACE_ENCODINGS:
        return []

    # Convert BGR (OpenCV) to RGB (face_recognition)
    rgb_frame = bgr_frame[:, :, ::-1]
    
    # Optional: resize for faster processing
    small_frame = cv2.resize(rgb_frame, (0, 0), fx=0.5, fy=0.5)

    locations = face_recognition.face_locations(small_frame)
    if not locations:
        return []

    encodings = face_recognition.face_encodings(small_frame, locations)
    
    names_found = []
    for encoding in encodings:
        matches = face_recognition.compare_faces(KNOWN_FACE_ENCODINGS, encoding, tolerance=0.6)
        name = "Unknown"
        
        # Or use face_distance to find the best match
        face_distances = face_recognition.face_distance(KNOWN_FACE_ENCODINGS, encoding)
        best_match_index = np.argmin(face_distances)
        if matches[best_match_index]:
            name = KNOWN_FACE_NAMES[best_match_index]
            
        names_found.append(name)
    
    return names_found


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

HOST = "0.0.0.0"
PORT = int(os.getenv("PORT", 8765))
UPLOAD_PORT = int(os.getenv("UPLOAD_PORT", 8766))

# Public URL that zrok exposes for this local WebSocket server.
# Example: ws://your-reserved-name.public.zrok.io
ZROK_PUBLIC_URL = os.getenv("ZROK_PUBLIC_URL", "")

SEND_SAMPLE_RATE = 16000

# Binary frame types from glasses:
#   0x01 = audio (PCM 16 kHz),
#   0x02 = image (JPEG),
#   0x03 = encoded video stream (H.264/AVC),
#   0x04 = AAC-encoded audio (ADTS).
FRAME_TYPE_AUDIO = 0x01
FRAME_TYPE_IMAGE = 0x02
FRAME_TYPE_VIDEO_H264 = 0x03
FRAME_TYPE_AUDIO_AAC = 0x04
FRAME_TYPE_AUDIO_POST_ALG = 0x05  # Post-algorithm PCM (noise-suppressed, ch 0/1)

INTERMEDIATE_DATA_DIR = Path(__file__).resolve().parent / "intermediate_data"


def ensure_only_video_clips_in_intermediate_data() -> None:
    """Remove any leftover frame_*.jpg from old runs; we only save video_*.mp4 now."""
    if not INTERMEDIATE_DATA_DIR.exists():
        return
    removed = 0
    for p in INTERMEDIATE_DATA_DIR.iterdir():
        if p.suffix.lower() == ".jpg" and p.name.startswith("frame_"):
            try:
                p.unlink()
                removed += 1
            except OSError:
                pass
    if removed:
        log.info("Removed %d old frame_*.jpg from intermediate_data (only video clips are saved now)", removed)


VIDEO_CLIP_SECONDS = 10  # length of each saved clip in seconds
# Must match ConnectViewModel.kt KEY_FRAME_RATE (10) so video duration = frame_count/10 = 10s clip
VIDEO_FPS = 10


class AudioTimeline:
    """
    Fixed-duration timeline that places PCM samples at wall-clock offsets.

    Pre-allocates a zero-filled int16 array for the full clip duration.
    When audio arrives, the caller computes the elapsed time since the
    interval started, and this class writes the samples at the correct
    position. At the end of the interval the array is a properly-aligned
    WAV-ready track (silence where nobody was speaking).
    """

    def __init__(self, sample_rate: int, duration_seconds: int) -> None:
        self.sample_rate = sample_rate
        self.duration_seconds = duration_seconds
        self.total_samples = sample_rate * duration_seconds
        self._data = np.zeros(self.total_samples, dtype=np.int16)
        # First chunk establishes the starting position; subsequent chunks
        # are placed contiguously to avoid jitter from wall-clock delays.
        self._start_sample: Optional[int] = None
        self._cursor_samples: int = 0
        self.has_audio = False

    def append_at(self, offset_seconds: float, pcm_bytes: bytes) -> None:
        """Write PCM samples into the timeline, aligned to the first-chunk offset.

        The first call uses offset_seconds to determine where the track should
        begin in the clip. Later calls ignore offset_seconds and place audio
        immediately after the previous chunk, which preserves smooth playback
        even if network/event timing is jittery.
        """
        n_samples = len(pcm_bytes) // 2
        if n_samples == 0:
            return

        if self._start_sample is None:
            start_sample = int(offset_seconds * self.sample_rate)
            if start_sample < 0:
                start_sample = 0
            if start_sample >= self.total_samples:
                return
            self._start_sample = start_sample

        start = self._start_sample + self._cursor_samples
        if start >= self.total_samples:
            return

        end = min(start + n_samples, self.total_samples)
        actual = end - start
        if actual <= 0:
            return

        samples = np.frombuffer(pcm_bytes[: actual * 2], dtype=np.int16)
        self._data[start:end] = samples
        self._cursor_samples += actual
        self.has_audio = True

    def to_wav_bytes(self) -> bytes:
        """Return the timeline as a complete in-memory WAV file."""
        import io
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(self.sample_rate)
            wf.writeframes(self._data.tobytes())
        return buf.getvalue()

    def write_wav(self, path: str) -> None:
        """Dump the timeline to a WAV file on disk."""
        with wave.open(path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(self.sample_rate)
            wf.writeframes(self._data.tobytes())


def process_frame(jpeg_bytes: bytes) -> Tuple[Optional[np.ndarray], Optional[bytes]]:
    """
    Fast path: no computer vision processing.
    Returns (BGR frame for video, JPEG bytes for Gemini).

    We no longer decode/rotate/contrast-enhance frames here; the raw JPEG
    bytes from the glasses are forwarded directly to Gemini. Video clips
    still rely on BGR frames, but since we don't populate that buffer
    anymore, clips will effectively be disabled.
    """
    # No processing, just pass the JPEG bytes through for Gemini.
    return None, jpeg_bytes


def write_video_clip(frames: list, fps: float = VIDEO_FPS) -> Optional[Path]:
    """Write BGR frames to an MP4 file (video only)."""
    if not frames:
        return None
    INTERMEDIATE_DATA_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    path = INTERMEDIATE_DATA_DIR / f"video_{ts}.mp4"
    try:
        h, w = frames[0].shape[:2]
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        writer = cv2.VideoWriter(str(path), fourcc, float(fps), (w, h))
        for frame in frames:
            writer.write(frame)
        writer.release()
        log.info("Saved video (no audio) %s (%d frames, fps=%.2f, ~%.1f s)", path, len(frames), fps, len(frames) / fps)
        return path
    except Exception as e:
        log.warning("Failed to write video %s: %s", path, e)
        return None


def write_av_clip(frames: list, audio_chunks: list[bytes]) -> Optional[Path]:
    """
    Write BGR frames and raw PCM audio to a single MP4 file.
    If there's not enough audio, save video-only instead of dropping everything.
    """
    if not frames:
        log.warning("write_av_clip called with 0 frames, skipping")
        return None

    INTERMEDIATE_DATA_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    out_path = INTERMEDIATE_DATA_DIR / f"video_{ts}.mp4"

    effective_fps = max(1.0, min(30.0, len(frames) / float(VIDEO_CLIP_SECONDS)))
    log.info("write_av_clip: %d frames, effective_fps=%.2f, audio_chunks=%d", len(frames), effective_fps, len(audio_chunks))

    raw_audio = b"".join(audio_chunks)
    has_audio = len(raw_audio) >= SEND_SAMPLE_RATE * 2  # at least ~1s of audio

    if not has_audio:
        log.info("Not enough audio (%d bytes), saving video-only clip", len(raw_audio))
        video_path = write_video_clip(frames, fps=effective_fps)
        if video_path and video_path != out_path:
            try:
                video_path.rename(out_path)
            except OSError:
                return video_path
        return out_path if (video_path is not None) else None

    video_only_path = write_video_clip(frames, fps=effective_fps)
    if not video_only_path:
        return None

    try:
        with tempfile.NamedTemporaryFile(suffix=".pcm", delete=False) as f:
            f.write(raw_audio)
            tmp_pcm = f.name

        try:
            subprocess.run(
                [
                    "ffmpeg", "-y", "-loglevel", "error",
                    "-i", str(video_only_path),
                    "-f", "s16le", "-ar", str(SEND_SAMPLE_RATE), "-ac", "1",
                    "-i", tmp_pcm,
                    "-c:v", "copy", "-c:a", "aac",
                    str(out_path),
                ],
                check=True,
                timeout=30,
                capture_output=True,
            )
            log.info("Saved AV clip %s (video_frames=%d, audio_bytes=%d)", out_path, len(frames), len(raw_audio))
            return out_path
        finally:
            try:
                os.unlink(tmp_pcm)
            except OSError:
                pass
            try:
                video_only_path.unlink()
            except OSError:
                pass
    except subprocess.CalledProcessError as e:
        log.warning("ffmpeg mux failed: %s — falling back to video-only", e.stderr.decode() if e.stderr else e)
        return write_video_clip(frames, fps=effective_fps)
    except FileNotFoundError:
        log.warning("ffmpeg not found; saving video-only clip")
        return write_video_clip(frames, fps=effective_fps)
    except Exception as e:
        log.warning("Failed to write AV clip %s: %s — falling back to video-only", out_path, e)
        return write_video_clip(frames, fps=effective_fps)


def write_h264_av_clip(
    h264_chunks: list[bytes],
    mic_timeline: AudioTimeline,
    model_timeline: Optional[AudioTimeline],
) -> Optional[Path]:
    """
    Write H.264 video + time-aligned WAV audio tracks to a single MP4.

    Both AudioTimeline objects are the same duration as the video clip and
    already have their samples placed at the correct wall-clock positions,
    so ffmpeg just needs to mix them together.
    """
    if not h264_chunks:
        return None

    INTERMEDIATE_DATA_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    out_path = INTERMEDIATE_DATA_DIR / f"video_{ts}.mp4"

    tmp_h264 = tmp_mic = tmp_model = None
    try:
        raw_video = b"".join(h264_chunks)
        if len(raw_video) < 4:
            return None

        with tempfile.NamedTemporaryFile(suffix=".h264", delete=False) as vf:
            vf.write(raw_video)
            tmp_h264 = vf.name

        tmp_mic_f = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        tmp_mic = tmp_mic_f.name
        tmp_mic_f.close()
        mic_timeline.write_wav(tmp_mic)

        has_model = model_timeline is not None and model_timeline.has_audio
        if has_model:
            tmp_model_f = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
            tmp_model = tmp_model_f.name
            tmp_model_f.close()
            model_timeline.write_wav(tmp_model)

        if has_model and tmp_model:
            cmd = [
                "ffmpeg", "-y", "-loglevel", "error",
                "-f", "h264", "-i", tmp_h264,
                "-i", tmp_mic,
                "-i", tmp_model,
                "-filter_complex",
                "[1:a][2:a]amix=inputs=2:normalize=0[aout]",
                "-map", "0:v",
                "-map", "[aout]",
                "-c:v", "copy",
                "-metadata:s:v:0", "rotate=90",
                "-c:a", "aac",
                "-ac", "1",
                str(out_path),
            ]
        else:
            cmd = [
                "ffmpeg", "-y", "-loglevel", "error",
                "-f", "h264", "-i", tmp_h264,
                "-i", tmp_mic,
                "-map", "0:v",
                "-map", "1:a",
                "-c:v", "copy",
                "-metadata:s:v:0", "rotate=90",
                "-c:a", "aac",
                "-ac", "1",
                str(out_path),
            ]

        subprocess.run(cmd, check=True, timeout=60, capture_output=True)
        log.info(
            "Saved H264 AV clip %s (video=%d bytes, mic=%s, model=%s)",
            out_path, len(raw_video),
            "yes" if mic_timeline.has_audio else "silent",
            "yes" if has_model else "none",
        )
        return out_path
    except subprocess.CalledProcessError as e:
        log.warning("ffmpeg H264 AV mux failed: %s", e.stderr.decode() if e.stderr else e)
        return None
    except FileNotFoundError:
        log.warning("ffmpeg not found; install ffmpeg to save H264 AV clips")
        return None
    except Exception as e:
        log.warning("Failed to write H264 AV clip %s: %s", out_path, e)
        return None
    finally:
        for tmp in (tmp_h264, tmp_mic, tmp_model):
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass


def write_h264_clip(h264_chunks: list[bytes]) -> Optional[Path]:
    """Write buffered H.264 chunks to an MP4 file using ffmpeg. Returns path or None."""
    if not h264_chunks:
        return None
    INTERMEDIATE_DATA_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    out_path = INTERMEDIATE_DATA_DIR / f"video_{ts}.mp4"
    try:
        raw = b"".join(h264_chunks)
        if len(raw) < 4:
            return None
        with tempfile.NamedTemporaryFile(suffix=".h264", delete=False) as f:
            f.write(raw)
            tmp = f.name
        try:
            subprocess.run(
                [
                    "ffmpeg", "-y", "-loglevel", "error",
                    "-f", "h264", "-i", tmp,
                    "-c", "copy",
                    "-metadata:s:v:0", "rotate=90",
                    str(out_path),
                ],
                check=True,
                timeout=30,
                capture_output=True,
            )
            log.info("Saved H264 video %s (%d bytes)", out_path, len(raw))
            return out_path
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    except subprocess.CalledProcessError as e:
        log.warning("ffmpeg failed (H264): %s", e.stderr.decode() if e.stderr else e)
        return None
    except FileNotFoundError:
        log.warning("ffmpeg not found; install ffmpeg to save H264 clips")
        return None
    except Exception as e:
        log.warning("Failed to write H264 video %s: %s", out_path, e)
        return None


def write_h264_audio_clip(
    h264_chunks: list[bytes],
    audio_chunks: list[bytes],
    h264_config: Optional[bytes] = None,
    sample_rate: int = SEND_SAMPLE_RATE,
    audio_is_aac: bool = False,
    video_fps: Optional[float] = None,
) -> Optional[Path]:
    """
    Mux raw H.264 video + audio into a single MP4 in intermediate_data.

    audio_is_aac=True  → audio_chunks contain AAC ADTS frames (0x04, from Rokid).
    audio_is_aac=False → audio_chunks contain raw PCM s16le (0x01, from test client).
    Prepends h264_config (SPS/PPS) so each clip is decodable from the start.
    video_fps: if set, used as H.264 input framerate so duration = len(h264_chunks)/video_fps
               (e.g. 280 frames at 28 fps = 10s). If None, uses VIDEO_FPS.
    """
    if not h264_chunks:
        log.debug("write_h264_audio_clip: no h264_chunks, skipping")
        return None
    # Derive fps so video duration = frame_count/fps = VIDEO_CLIP_SECONDS (matches 10s audio)
    if video_fps is not None:
        fps = video_fps
    elif VIDEO_CLIP_SECONDS > 0:
        fps = max(1.0, min(60.0, len(h264_chunks) / VIDEO_CLIP_SECONDS))
    else:
        fps = VIDEO_FPS
    raw_audio = b"".join(audio_chunks) if audio_chunks else b""
    log.info(
        "[clip] write_h264_audio_clip: h264_chunks=%d, audio_chunks=%d, audio_bytes=%d, config_len=%d, is_aac=%s, video_fps=%.1f",
        len(h264_chunks), len(audio_chunks), len(raw_audio), len(h264_config or b""), audio_is_aac, fps,
    )
    INTERMEDIATE_DATA_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    out_path = INTERMEDIATE_DATA_DIR / f"video_{ts}.mp4"
    tmp_h264 = tmp_audio = None
    try:
        raw_video = (h264_config or b"") + b"".join(h264_chunks)
        if len(raw_video) < 4:
            log.warning("[clip] raw_video too small (%d bytes), skipping", len(raw_video))
            return None
        with tempfile.NamedTemporaryFile(suffix=".h264", delete=False) as f:
            f.write(raw_video)
            tmp_h264 = f.name

        if raw_audio:
            if audio_is_aac:
                log.info("[clip] audio is AAC ADTS (%d bytes)", len(raw_audio))
                with tempfile.NamedTemporaryFile(suffix=".aac", delete=False) as f:
                    f.write(raw_audio)
                    tmp_audio = f.name
                audio_input = ["-f", "aac", "-i", tmp_audio]
                audio_codec = ["-c:a", "copy"]
            else:
                log.info("[clip] audio is PCM s16le %d Hz mono (%d bytes)", sample_rate, len(raw_audio))
                with tempfile.NamedTemporaryFile(suffix=".pcm", delete=False) as f:
                    f.write(raw_audio)
                    tmp_audio = f.name
                audio_input = ["-f", "s16le", "-ar", str(sample_rate), "-ac", "1", "-i", tmp_audio]
                audio_codec = ["-c:a", "aac", "-b:a", "128k"]

        if tmp_audio:
            # AAC encoder priming: MediaCodec buffers ~2048 samples before
            # producing the first AAC frame, so audio lags video at the start.
            # Compensate with -itsoffset on the audio input (negative = shift
            # audio earlier relative to video).
            AAC_PRIMING_MS = 150  # tweak if still misaligned
            audio_offset = f"-{AAC_PRIMING_MS / 1000:.3f}"
            cmd = [
                "ffmpeg", "-y", "-loglevel", "error",
                "-f", "h264", "-r", str(fps), "-i", tmp_h264,
                "-itsoffset", audio_offset, *audio_input,
                "-map", "0:v", "-map", "1:a",
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
                "-vf", "transpose=2",
                *audio_codec,
                "-fflags", "+genpts",
                str(out_path),
            ]
            subprocess.run(cmd, check=True, timeout=60, capture_output=True)
            audio_label = "aac" if audio_is_aac else "pcm"
            log.info(
                "Saved H264+audio clip %s (video=%d B, audio=%d B [%s])",
                out_path, len(raw_video), len(raw_audio), audio_label,
            )
            try:
                probe = subprocess.run(
                    ["ffprobe", "-v", "error", "-select_streams", "a",
                     "-show_entries", "stream=codec_type,sample_rate,codec_name",
                     "-of", "csv=p=0", str(out_path)],
                    capture_output=True, text=True, timeout=5,
                )
                if probe.returncode == 0 and probe.stdout.strip():
                    log.info("[clip] ffprobe audio: %s", probe.stdout.strip())
                else:
                    log.warning("[clip] ffprobe: NO audio stream in %s", out_path)
            except Exception as e:
                log.debug("[clip] ffprobe check failed: %s", e)
        else:
            cmd = [
                "ffmpeg", "-y", "-loglevel", "error",
                "-f", "h264", "-r", str(fps), "-i", tmp_h264,
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
                "-vf", "transpose=2",
                str(out_path),
            ]
            subprocess.run(cmd, check=True, timeout=60, capture_output=True)
            log.warning("[clip] No audio received — saved video-only")
        log.info("[clip] Done: %s", out_path)
        return out_path
    except subprocess.CalledProcessError as e:
        log.warning("ffmpeg H264+audio mux failed: %s", e.stderr.decode() if e.stderr else e)
        return None
    except FileNotFoundError:
        log.warning("ffmpeg not found; install ffmpeg to save clips")
        return None
    except Exception as e:
        log.warning("Failed to write H264+audio clip %s: %s", out_path, e)
        return None
    finally:
        for tmp in (tmp_h264, tmp_audio):
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass


async def bridge(glasses_ws) -> None:
    """
    Non-assistant mode: receive H.264 video (0x03) and audio from client,
    buffer them, and every VIDEO_CLIP_SECONDS write a combined MP4.

    Audio may arrive as:
      0x04 (AAC ADTS) — preferred, from MediaCodec encoder on Rokid.
      0x01 (PCM s16le 16kHz) — fallback from test client or legacy builds.
    AAC is preferred when available; PCM is used as fallback.
    """
    client_addr = glasses_ws.remote_address
    log.info("Glasses connected (stream-only): %s", client_addr)
    h264_buffer: list[bytes] = []
    aac_buffer: list[bytes] = []   # AAC ADTS frames from 0x04
    pcm_buffer: list[bytes] = []   # raw PCM from 0x01 (fallback)
    post_alg_buffer: list[bytes] = []  # post-algorithm PCM from 0x05 (noise-suppressed, for enrollment)
    h264_config: Optional[bytes] = None
    buffer_lock = Lock()
    loop = asyncio.get_event_loop()

    async def save_clip_every_interval() -> None:
        nonlocal h264_config
        interval_num = 0
        while True:
            await asyncio.sleep(VIDEO_CLIP_SECONDS)
            interval_num += 1
            with buffer_lock:
                h264_snapshot = list(h264_buffer)
                aac_snapshot = list(aac_buffer)
                pcm_snapshot = list(pcm_buffer)
                config = h264_config
                h264_buffer.clear()
                aac_buffer.clear()
                pcm_buffer.clear()
            video_bytes = sum(len(c) for c in h264_snapshot)
            aac_bytes = sum(len(c) for c in aac_snapshot)
            pcm_bytes = sum(len(c) for c in pcm_snapshot)
            log.info(
                "[stream_only] interval %d: video_chunks=%d (%d B) | aac_chunks=%d (%d B) | pcm_chunks=%d (%d B) | config=%d B",
                interval_num, len(h264_snapshot), video_bytes,
                len(aac_snapshot), aac_bytes,
                len(pcm_snapshot), pcm_bytes,
                len(config or b""),
            )
            if not h264_snapshot:
                log.info("[stream_only] interval %d: no video, skipping", interval_num)
                continue
            # Prefer AAC; fall back to PCM
            if aac_snapshot:
                audio_data = aac_snapshot
                audio_is_aac = True
            elif pcm_snapshot:
                audio_data = pcm_snapshot
                audio_is_aac = False
            else:
                audio_data = []
                audio_is_aac = False
            try:
                await loop.run_in_executor(
                    None,
                    lambda h=h264_snapshot, a=audio_data, c=config, is_aac=audio_is_aac: (
                        write_h264_audio_clip(h, a, h264_config=c, audio_is_aac=is_aac)
                    ),
                )
            except Exception as e:
                log.warning("stream_only save clip failed: %s", e)

    first_pcm = first_aac = first_video = True

    # Face recognition state
    recognition_h264_buffer: list[bytes] = []
    last_recognition_time = 0.0
    RECOGNITION_INTERVAL = 2.0  # seconds between recognition attempts

    last_stats_time = time.monotonic()
    STATS_INTERVAL = 2.0

    save_task: Optional[asyncio.Task] = None
    try:
        h264_codec = av.CodecContext.create("h264", "r")
    except Exception as e:
        log.warning("Could not create PyAV H264 codec: %s", e)

        async for message in glasses_ws:
            if isinstance(message, str):
                try:
                    cmd = json_module.loads(message)
                    command = cmd.get("command", "")

                    if command == "enroll":
                        # Grab latest frame and run enrollment
                        frame = await loop.run_in_executor(
                            None,
                            lambda chunks=list(recognition_h264_buffer), cfg=h264_config: extract_frame_from_h264(chunks, cfg)
                        )
                        if frame is not None:
                            embedding = await loop.run_in_executor(
                                None,
                                lambda f=frame: extract_embedding(f)
                            )
                            if embedding is not None:
                                # Try auto-extraction from audio first
                                name = cmd.get("name")
                                role = cmd.get("role")
                                fun_fact = cmd.get("fun_fact")

                                if not name and post_alg_buffer:
                                    # Use noise-suppressed audio for transcription
                                    try:
                                        extracted_name, extracted_role, extracted_fact = await transcribe_and_extract(
                                            list(post_alg_buffer)
                                        )
                                        name = extracted_name or name
                                        role = extracted_role or role
                                        fun_fact = extracted_fact or fun_fact
                                    except Exception as e:
                                        log.warning("Audio extraction failed: %s", e)

                                # Fallback if still no name
                                if not name:
                                    name = f"Person_{int(time.time())}"
                                person_id = add_person(name, embedding, role, fun_fact)
                                await glasses_ws.send(json_module.dumps({
                                    "type": "enrolled",
                                    "person": {"id": person_id, "name": name, "role": role}
                                }))
                                log.info("Enrolled person: %s (id=%d)", name, person_id)
                            else:
                                await glasses_ws.send(json_module.dumps({
                                    "type": "error", "message": "No face detected in frame"
                                }))
                        else:
                            await glasses_ws.send(json_module.dumps({
                                "type": "error", "message": "No video frames available"
                            }))

                except Exception as e:
                    log.warning("Failed to handle text command: %s", e)
                continue

            if isinstance(message, bytes) and len(message) >= 1:
                frame_type = message[0]
                payload = message[1:]
                if frame_type == FRAME_TYPE_AUDIO:
                    if first_pcm:
                        log.info("[stream_only] first 0x01 (PCM audio), len=%d", len(payload))
                        first_pcm = False
                    with buffer_lock:
                        pcm_buffer.append(payload)
                elif frame_type == FRAME_TYPE_AUDIO_AAC:
                    if first_aac:
                        log.info("[stream_only] first 0x04 (AAC audio), len=%d", len(payload))
                        first_aac = False
                    with buffer_lock:
                        aac_buffer.append(payload)
                elif frame_type == FRAME_TYPE_AUDIO_POST_ALG:
                    # Post-algorithm audio (noise-suppressed) — best for speech recognition
                    post_alg_buffer.append(payload)
                    # Keep last ~15 seconds of audio (16kHz * 2 bytes * 15s = 480KB)
                    max_chunks = 240  # ~15s at typical chunk sizes
                    if len(post_alg_buffer) > max_chunks:
                        post_alg_buffer[:] = post_alg_buffer[-max_chunks:]
                elif frame_type == FRAME_TYPE_VIDEO_H264:
                    if first_video:
                        log.info("[stream_only] first 0x03 (video), len=%d (stored as config)", len(payload))
                        first_video = False
                    with buffer_lock:
                        if h264_config is None:
                            h264_config = payload
                        h264_buffer.append(payload)

                    # Buffer for face recognition
                    recognition_h264_buffer.append(payload)
                    if len(recognition_h264_buffer) > 20:
                        recognition_h264_buffer = recognition_h264_buffer[-20:]

                    # Run recognition periodically
                    now_recog = time.time()
                    if now_recog - last_recognition_time >= RECOGNITION_INTERVAL:
                        last_recognition_time = now_recog

                        async def do_recognition(chunks=list(recognition_h264_buffer), cfg=h264_config):
                            try:
                                frame = await loop.run_in_executor(
                                    None,
                                    lambda: extract_frame_from_h264(chunks, cfg)
                                )
                                if frame is None:
                                    return
                                embedding = await loop.run_in_executor(
                                    None,
                                    lambda: extract_embedding(frame)
                                )
                                if embedding is None:
                                    return
                                persons = get_all_persons()
                                result = match_face(embedding, persons)
                                if result:
                                    person, confidence = result
                                    await glasses_ws.send(json_module.dumps({
                                        "type": "recognition",
                                        "matched": True,
                                        "person": {
                                            "name": person.name,
                                            "role": person.role,
                                            "fun_fact": person.fun_fact,
                                            "confidence": confidence,
                                        }
                                    }))
                                    log.info("Recognized: %s (%.0f%%)", person.name, confidence * 100)
                            except Exception as e:
                                log.warning("Recognition failed: %s", e)

                        asyncio.create_task(do_recognition())
                else:
                    log.debug("[stream_only] unknown frame_type=0x%02x len=%d", frame_type, len(payload))
            now = time.monotonic()
            if now - last_stats_time >= STATS_INTERVAL:
                last_stats_time = now
                with buffer_lock:
                    v_c, v_b = len(h264_buffer), sum(len(c) for c in h264_buffer)
                    aac_c, aac_b = len(aac_buffer), sum(len(c) for c in aac_buffer)
                    pcm_c, pcm_b = len(pcm_buffer), sum(len(c) for c in pcm_buffer)
                log.info(
                    "[stream_only] buffers: video %d/%d B | aac %d/%d B | pcm %d/%d B",
                    v_c, v_b, aac_c, aac_b, pcm_c, pcm_b,
                )
    except websockets.exceptions.ConnectionClosedOK:
        log.info("Connection closed cleanly: %s", client_addr)
    except websockets.exceptions.ConnectionClosedError as exc:
        log.warning("Connection closed with error: %s — %s", client_addr, exc)
    except Exception as exc:
        log.exception("Unexpected error for %s: %s", client_addr, exc)
    finally:
        if save_task and not save_task.done():
            save_task.cancel()
            try:
                await save_task
            except asyncio.CancelledError:
                pass
        log.info("Glasses disconnected: %s", client_addr)


async def main() -> None:
    ensure_only_video_clips_in_intermediate_data()
    init_db()
    log.info("Person database initialized")
    log.info("Starting WebSocket server on ws://%s:%d (user recognition - stream only)", HOST, PORT)
    log.info("Starting HTTP upload server on http://%s:%d/upload_clip", HOST, UPLOAD_PORT)

    if ZROK_PUBLIC_URL:
        log.info("Using zrok public WebSocket URL: %s", ZROK_PUBLIC_URL)
        log.info("Glasses should connect to the zrok URL above.")
    else:
        log.warning("ZROK_PUBLIC_URL is not set.")

    async def handle_upload_clip(request: web.Request) -> web.Response:
        try:
            body = await request.read()
            if not body:
                return web.Response(status=400, text="empty body")
            INTERMEDIATE_DATA_DIR.mkdir(parents=True, exist_ok=True)
            ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
            filename = request.query.get("name") or f"clip_{ts}.mp4"
            filename = os.path.basename(filename)
            out_path = INTERMEDIATE_DATA_DIR / filename
            with open(out_path, "wb") as f:
                f.write(body)
            log.info("Saved uploaded clip %s (%d bytes)", out_path, len(body))
            return web.json_response({"status": "ok", "path": str(out_path.name)})
        except Exception as e:
            log.exception("upload_clip failed: %s", e)
            return web.Response(status=500, text="failed")

    app = web.Application()
    app.router.add_post("/upload_clip", handle_upload_clip)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, HOST, UPLOAD_PORT)

    async def ws_handler(ws) -> None:
        await bridge(ws)

    ws_server = await websockets.serve(ws_handler, HOST, PORT)
    await site.start()

    try:
        await asyncio.Future()  # run forever
    finally:
        ws_server.close()
        await ws_server.wait_closed()
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
