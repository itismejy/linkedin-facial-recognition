"""Audio transcription + name/role/fun_fact extraction using Google Gemini."""
import io
import json
import logging
import os
import tempfile
import wave
from typing import Optional, Tuple

from google import genai

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000


def pcm_to_wav(pcm_chunks: list[bytes], sample_rate: int = SAMPLE_RATE) -> bytes:
    """Convert raw PCM 16-bit mono chunks to WAV bytes."""
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
    Transcribe audio and extract name, role, fun_fact using Google Gemini.
    Returns (name, role, fun_fact) — any can be None if not detected.

    Uses Gemini's multimodal capability to process audio directly.
    """
    api_key = os.getenv("GEMINI_API_KEY", "")
    if not pcm_chunks or not api_key:
        log.warning("transcribe_and_extract: no audio chunks or no GEMINI_API_KEY")
        return None, None, None

    tmp_wav = None
    try:
        wav_bytes = pcm_to_wav(pcm_chunks)

        # Write WAV to temp file for Gemini upload
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            f.write(wav_bytes)
            tmp_wav = f.name

        client = genai.Client(api_key=api_key)

        # Upload audio file to Gemini
        audio_file = client.files.upload(file=tmp_wav)

        response = client.models.generate_content(
            model="gemini-2.0-flash",
            contents=[
                audio_file,
                """Listen to this audio of someone introducing themselves at a networking event or group introduction.

Extract the following information in JSON format:
{
  "name": "their full name",
  "role": "their job title and/or company",
  "fun_fact": "any interesting personal fact they mentioned"
}

Rules:
- If you can't determine a field, set it to null
- For "role", combine job title and company if both mentioned (e.g. "PM @ Google")
- For "fun_fact", pick the most memorable/interesting thing they said about themselves
- Return ONLY valid JSON, no other text, no markdown code blocks"""
            ]
        )

        # Parse the JSON response
        text = response.text.strip()
        # Remove markdown code blocks if present
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[:-3].strip()
        if text.startswith("json"):
            text = text[4:].strip()

        result = json.loads(text)
        name = result.get("name")
        role = result.get("role")
        fun_fact = result.get("fun_fact")

        log.info("Gemini extracted: name=%s, role=%s, fun_fact=%s", name, role, fun_fact)
        return name, role, fun_fact

    except Exception as e:
        log.warning("transcribe_and_extract failed: %s", e)
        return None, None, None
    finally:
        if tmp_wav:
            try:
                os.unlink(tmp_wav)
            except OSError:
                pass
