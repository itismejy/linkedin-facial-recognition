# Backend Implementation Plan — FaceTag

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add real-time face recognition + enrollment to the existing Rokid WebSocket server so it can identify people from the glasses video stream and send their name/role back for HUD display.

**Architecture:** Extend the existing `bridge()` WebSocket handler to decode H.264 frames, run face_recognition on them, match against a SQLite database, and send JSON results back. Enrollment is triggered by a text command from the glasses. Audio transcription uses Whisper API + Gemini for name/role extraction.

**Tech Stack:** Python, face_recognition (ageitgey), SQLite, OpenAI Whisper API, Google Gemini, websockets, asyncio

**Repo:** `/tmp/linkedin-facial-recognition/server/`

---

## Task 1: Database Module

**Files:**
- Create: `server/user_recognition_agent/database.py`

**Step 1: Create the SQLite database module**

```python
"""Person database for face embeddings and metadata."""
import sqlite3
import json
import numpy as np
from pathlib import Path
from typing import Optional
from dataclasses import dataclass

DB_PATH = Path(__file__).resolve().parent / "persons.db"

@dataclass
class Person:
    id: int
    name: str
    role: Optional[str]
    fun_fact: Optional[str]
    embedding: np.ndarray
    created_at: str

def init_db() -> None:
    """Create the persons table if it doesn't exist."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS persons (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            role TEXT,
            fun_fact TEXT,
            embedding TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_seen TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

def add_person(name: str, embedding: np.ndarray, role: str = None, fun_fact: str = None) -> int:
    """Insert a new person. Returns the person ID."""
    conn = sqlite3.connect(str(DB_PATH))
    embedding_json = json.dumps(embedding.tolist())
    cursor = conn.execute(
        "INSERT INTO persons (name, role, fun_fact, embedding) VALUES (?, ?, ?, ?)",
        (name, role, fun_fact, embedding_json)
    )
    person_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return person_id

def get_all_persons() -> list[Person]:
    """Load all persons with their embeddings."""
    conn = sqlite3.connect(str(DB_PATH))
    rows = conn.execute("SELECT id, name, role, fun_fact, embedding, created_at FROM persons").fetchall()
    conn.close()
    persons = []
    for row in rows:
        emb = np.array(json.loads(row[4]))
        persons.append(Person(id=row[0], name=row[1], role=row[2], fun_fact=row[3], embedding=emb, created_at=row[5]))
    return persons

def delete_person(person_id: int) -> None:
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("DELETE FROM persons WHERE id = ?", (person_id,))
    conn.commit()
    conn.close()
```

**Step 2: Commit**

```bash
git add server/user_recognition_agent/database.py
git commit -m "feat: add SQLite person database module"
```

---

## Task 2: Face Recognition Module

**Files:**
- Create: `server/user_recognition_agent/recognition.py`

**Step 1: Create the face recognition module**

```python
"""Face recognition: detect, embed, and match faces."""
import logging
import numpy as np
import face_recognition
from typing import Optional, Tuple
from .database import Person, get_all_persons

log = logging.getLogger(__name__)

MATCH_THRESHOLD = 0.6  # Lower = stricter. face_recognition uses distance (not similarity).

def extract_embedding(image_bgr: np.ndarray) -> Optional[np.ndarray]:
    """
    Extract a 128-dim face embedding from a BGR image.
    Returns None if no face detected.
    """
    # face_recognition expects RGB
    rgb = image_bgr[:, :, ::-1]
    encodings = face_recognition.face_encodings(rgb)
    if not encodings:
        return None
    return encodings[0]  # Take the first (largest) face

def match_face(embedding: np.ndarray, persons: list[Person]) -> Optional[Tuple[Person, float]]:
    """
    Match an embedding against all known persons.
    Returns (person, confidence) or None if no match.
    Confidence = 1 - distance (higher = better match).
    """
    if not persons:
        return None
    
    known_embeddings = [p.embedding for p in persons]
    distances = face_recognition.face_distance(known_embeddings, embedding)
    
    best_idx = int(np.argmin(distances))
    best_distance = distances[best_idx]
    
    if best_distance < MATCH_THRESHOLD:
        confidence = round(1.0 - best_distance, 2)
        return persons[best_idx], confidence
    
    return None

def decode_h264_frame(h264_bytes: bytes) -> Optional[np.ndarray]:
    """
    Decode a single H.264 frame to BGR using OpenCV.
    This is a best-effort approach — H.264 NAL units may need
    accumulation for proper decoding.
    """
    # We'll use a persistent VideoCapture in the main loop instead.
    # This function is a placeholder — actual decoding happens via
    # ffmpeg subprocess or cv2.VideoCapture on a pipe.
    pass
```

**Step 2: Commit**

```bash
git add server/user_recognition_agent/recognition.py
git commit -m "feat: add face recognition module (detect, embed, match)"
```

---

## Task 3: H.264 Frame Decoder

**Files:**
- Create: `server/user_recognition_agent/decoder.py`

This is the trickiest part — decoding individual H.264 NAL units from the WebSocket stream into BGR frames.

**Step 1: Create the decoder**

```python
"""Decode H.264 NAL units from WebSocket stream into BGR frames."""
import logging
import subprocess
import tempfile
import numpy as np
import cv2
from typing import Optional
from threading import Lock

log = logging.getLogger(__name__)


class H264Decoder:
    """
    Accumulates H.264 NAL units and decodes them to BGR frames
    using ffmpeg as a subprocess pipe.
    """

    def __init__(self, width: int = 640, height: int = 480):
        self.width = width
        self.height = height
        self._process = None
        self._lock = Lock()
        self._frame_size = width * height * 3  # BGR
        self._start_ffmpeg()

    def _start_ffmpeg(self):
        """Start ffmpeg process that reads H.264 from stdin and outputs raw BGR frames."""
        try:
            self._process = subprocess.Popen(
                [
                    "ffmpeg",
                    "-loglevel", "error",
                    "-f", "h264",
                    "-i", "pipe:0",
                    "-f", "rawvideo",
                    "-pix_fmt", "bgr24",
                    "-vf", f"scale={self.width}:{self.height},transpose=2",
                    "pipe:1",
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            log.info("H264Decoder: ffmpeg process started")
        except FileNotFoundError:
            log.error("ffmpeg not found — cannot decode H.264 frames")
            self._process = None

    def decode(self, h264_bytes: bytes) -> Optional[np.ndarray]:
        """
        Feed H.264 bytes and try to read a decoded BGR frame.
        Returns None if no frame is available yet.
        """
        if self._process is None or self._process.poll() is not None:
            return None

        with self._lock:
            try:
                self._process.stdin.write(h264_bytes)
                self._process.stdin.flush()
            except (BrokenPipeError, OSError):
                log.warning("H264Decoder: ffmpeg pipe broken, restarting")
                self._start_ffmpeg()
                return None

        # Non-blocking read of one frame
        # Note: this is tricky with pipes. We'll use a separate thread reader.
        return None

    def close(self):
        if self._process:
            try:
                self._process.stdin.close()
                self._process.terminate()
            except Exception:
                pass
```

**NOTE:** The pipe-based approach above has blocking issues. A simpler hackathon approach: accumulate H.264 chunks for N seconds, write to a temp file, use `cv2.VideoCapture` to extract a frame. OR — since the server already saves 10-second MP4 clips, we can grab frames from those. But for real-time, we should decode inline.

**Alternative (simpler, recommended for hackathon):** Use the JPEG frames the glasses can also send (frame type 0x02). Check if the glasses app can be configured to send JPEG snapshots instead of/alongside H.264. If not, use the temp-file approach:

```python
"""Simple frame extractor — decode H.264 chunks via temp file."""
import tempfile
import subprocess
import cv2
import numpy as np
from typing import Optional
import logging

log = logging.getLogger(__name__)


def extract_frame_from_h264(h264_chunks: list[bytes]) -> Optional[np.ndarray]:
    """
    Write accumulated H.264 chunks to a temp file,
    use ffmpeg to extract a single JPEG frame, read it with OpenCV.
    """
    if not h264_chunks:
        return None

    raw = b"".join(h264_chunks)
    if len(raw) < 100:
        return None

    try:
        with tempfile.NamedTemporaryFile(suffix=".h264", delete=False) as vf:
            vf.write(raw)
            tmp_h264 = vf.name

        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as jf:
            tmp_jpg = jf.name

        subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error",
                "-f", "h264", "-i", tmp_h264,
                "-vframes", "1",
                "-vf", "transpose=2",
                tmp_jpg,
            ],
            check=True,
            timeout=5,
            capture_output=True,
        )

        frame = cv2.imread(tmp_jpg)
        return frame

    except Exception as e:
        log.warning("extract_frame_from_h264 failed: %s", e)
        return None
    finally:
        import os
        for f in (tmp_h264, tmp_jpg):
            try:
                os.unlink(f)
            except OSError:
                pass
```

**Step 2: Commit**

```bash
git add server/user_recognition_agent/decoder.py
git commit -m "feat: add H.264 frame decoder (temp-file approach)"
```

---

## Task 4: Integrate Recognition into WebSocket Handler

**Files:**
- Modify: `server/user_recognition_agent/server.py`

This is the main integration. Add face recognition into the existing `bridge()` function.

**Step 1: Add imports and state to server.py (top of file)**

Add after existing imports:
```python
import json as json_module
from .database import init_db, add_person, get_all_persons
from .recognition import extract_embedding, match_face
from .decoder import extract_frame_from_h264
```

**Step 2: Initialize DB in `main()`**

Add at the start of `async def main()`:
```python
init_db()
log.info("Person database initialized")
```

**Step 3: Add recognition logic to `bridge()`**

Inside `bridge()`, add these state variables after the existing buffer declarations:
```python
# Face recognition state
recognition_h264_buffer: list[bytes] = []
last_recognition_time = 0.0
RECOGNITION_INTERVAL = 2.0  # seconds between recognition attempts
audio_buffer_for_enroll: list[bytes] = []
AUDIO_BUFFER_SECONDS = 15  # keep last 15s of audio for enrollment
mode = "recall"  # "recall" or "learn"
```

Add a text message handler in the `async for message in glasses_ws:` loop (before the binary check):
```python
if isinstance(message, str):
    try:
        cmd = json_module.loads(message)
        if cmd.get("command") == "enroll":
            # Grab latest frame and run enrollment
            frame = await loop.run_in_executor(
                None,
                lambda: extract_frame_from_h264(list(recognition_h264_buffer))
            )
            if frame is not None:
                embedding = await loop.run_in_executor(
                    None,
                    lambda: extract_embedding(frame)
                )
                if embedding is not None:
                    # For MVP: use manual name from command, or "Unknown_N"
                    name = cmd.get("name", f"Person_{int(time.time())}")
                    role = cmd.get("role")
                    fun_fact = cmd.get("fun_fact")
                    person_id = add_person(name, embedding, role, fun_fact)
                    await glasses_ws.send(json_module.dumps({
                        "type": "enrolled",
                        "person": {"id": person_id, "name": name, "role": role}
                    }))
                    log.info("Enrolled person: %s (id=%d)", name, person_id)
                else:
                    await glasses_ws.send(json_module.dumps({
                        "type": "error",
                        "message": "No face detected in frame"
                    }))
            else:
                await glasses_ws.send(json_module.dumps({
                    "type": "error",
                    "message": "No video frames available"
                }))
        elif cmd.get("command") == "set_mode":
            mode = cmd.get("mode", "recall")
            log.info("Mode set to: %s", mode)
    except Exception as e:
        log.warning("Failed to parse text command: %s", e)
    continue
```

Inside the H.264 frame handler (where `frame_type == FRAME_TYPE_VIDEO_H264`), add after the existing buffer append:
```python
# Also buffer for recognition
recognition_h264_buffer.append(payload)
# Keep only last ~2 seconds of H.264 for frame extraction
if len(recognition_h264_buffer) > 20:  # ~2s at 10fps
    recognition_h264_buffer = recognition_h264_buffer[-20:]

# Run recognition every RECOGNITION_INTERVAL seconds
now = time.time()
if now - last_recognition_time >= RECOGNITION_INTERVAL:
    last_recognition_time = now
    
    async def do_recognition():
        frame = await loop.run_in_executor(
            None,
            lambda: extract_frame_from_h264(list(recognition_h264_buffer))
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
    
    asyncio.create_task(do_recognition())
```

**Step 4: Commit**

```bash
git add server/user_recognition_agent/
git commit -m "feat: integrate face recognition into WebSocket handler"
```

---

## Task 5: Audio Transcription + Gemini Extraction (Stretch)

**Files:**
- Create: `server/user_recognition_agent/transcription.py`

**Step 1: Create transcription module**

```python
"""Audio transcription (Whisper) + name extraction (Gemini)."""
import logging
import os
import tempfile
import wave
from typing import Optional, Tuple
from google import genai

log = logging.getLogger(__name__)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
SAMPLE_RATE = 16000


def pcm_to_wav(pcm_chunks: list[bytes], sample_rate: int = SAMPLE_RATE) -> bytes:
    """Convert raw PCM chunks to WAV bytes."""
    import io
    raw = b"".join(pcm_chunks)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(raw)
    return buf.getvalue()


async def transcribe_and_extract(pcm_chunks: list[bytes]) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Transcribe audio and extract name, role, fun_fact using Gemini.
    Returns (name, role, fun_fact) — any can be None.
    """
    if not pcm_chunks or not GEMINI_API_KEY:
        return None, None, None

    try:
        wav_bytes = pcm_to_wav(pcm_chunks)
        
        # Write to temp file for Gemini
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            f.write(wav_bytes)
            tmp_wav = f.name

        client = genai.Client(api_key=GEMINI_API_KEY)
        
        # Upload audio and ask Gemini to extract info
        audio_file = client.files.upload(file=tmp_wav)
        
        response = client.models.generate_content(
            model="gemini-2.0-flash",
            contents=[
                audio_file,
                """Listen to this audio of someone introducing themselves.
                Extract the following in JSON format:
                {"name": "their name", "role": "their job/role", "fun_fact": "any interesting fact they mentioned"}
                If you can't determine a field, set it to null.
                Return ONLY the JSON, no other text."""
            ]
        )
        
        import json
        result = json.loads(response.text.strip())
        return result.get("name"), result.get("role"), result.get("fun_fact")

    except Exception as e:
        log.warning("Transcription/extraction failed: %s", e)
        return None, None, None
    finally:
        try:
            os.unlink(tmp_wav)
        except OSError:
            pass
```

**Step 2: Wire into enrollment in server.py**

Update the enroll handler to use transcription when audio is available:
```python
from .transcription import transcribe_and_extract

# In the enroll handler, after getting the embedding:
if pcm_buffer:  # audio available
    name_extracted, role_extracted, fact_extracted = await transcribe_and_extract(list(pcm_buffer[-240:]))  # last ~15s
    name = name_extracted or cmd.get("name", f"Person_{int(time.time())}")
    role = role_extracted or cmd.get("role")
    fun_fact = fact_extracted or cmd.get("fun_fact")
```

**Step 3: Commit**

```bash
git add server/user_recognition_agent/transcription.py
git commit -m "feat: add Whisper+Gemini audio transcription for enrollment"
```

---

## Task 6: Update requirements.txt

**Files:**
- Modify: `server/requirements.txt`

**Step 1: Add face_recognition and dependencies**

Add to requirements.txt:
```
face_recognition>=1.3.0
dlib>=19.24.0
```

**Step 2: Commit**

```bash
git add server/requirements.txt
git commit -m "chore: add face_recognition to requirements"
```

---

## Execution Order (Optimized for Speed)

1. **Task 6** — Update requirements, `pip install` (dlib takes a minute to compile)
2. **Task 1** — Database module (5 min)
3. **Task 2** — Recognition module (5 min)
4. **Task 3** — H.264 decoder (5 min)
5. **Task 4** — Wire everything into server.py (15 min)
6. **Task 5** — Audio transcription stretch goal (10 min)

**Total: ~40 minutes**

Start `pip install face_recognition dlib` FIRST — dlib compilation takes time and can run in the background while you code.

---

*Created: 2026-03-28 | Hackathon Backend Plan*
