"""Decode H.264 chunks to BGR frames via ffmpeg temp file."""
import tempfile
import subprocess
import os
import cv2
import numpy as np
from typing import Optional
import logging

log = logging.getLogger(__name__)

def extract_frame_from_h264(h264_chunks: list[bytes], h264_config: bytes = None) -> Optional[np.ndarray]:
    if not h264_chunks:
        return None
    raw = (h264_config or b"") + b"".join(h264_chunks)
    if len(raw) < 100:
        return None
    tmp_h264 = None
    tmp_jpg = None
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
        for f in (tmp_h264, tmp_jpg):
            if f:
                try:
                    os.unlink(f)
                except OSError:
                    pass
