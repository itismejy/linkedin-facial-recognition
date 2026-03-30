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
