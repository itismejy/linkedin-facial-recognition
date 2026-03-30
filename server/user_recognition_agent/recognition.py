"""Face recognition: detect, embed, and match faces."""
import logging
import numpy as np
import face_recognition
from typing import Optional, Tuple
from .database import Person, get_all_persons

log = logging.getLogger(__name__)

MATCH_THRESHOLD = 0.6

def extract_embedding(image_bgr: np.ndarray) -> Optional[np.ndarray]:
    rgb = image_bgr[:, :, ::-1]
    encodings = face_recognition.face_encodings(rgb)
    if not encodings:
        return None
    return encodings[0]

def match_face(embedding: np.ndarray, persons: list[Person]) -> Optional[Tuple[Person, float]]:
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
