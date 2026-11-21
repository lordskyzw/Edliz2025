"""SQLite storage: pages, chunks, vectors, digitised medicines.

One file holds everything, which keeps the app deployable as a pair of files
(``edliz.db`` plus the text directory) rather than a service stack. Vectors
live in a BLOB column as float32 and are searched with numpy; at 273 pages
that is a few thousand rows of 1024 floats, well under a second per query.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS corpora (
    id         INTEGER PRIMARY KEY,
    edition    TEXT NOT NULL,
    source     TEXT,
    pages      INTEGER,
    chunks     INTEGER,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pages (
    corpus_id INTEGER NOT NULL REFERENCES corpora(id) ON DELETE CASCADE,
    page      INTEGER NOT NULL,
    heading   TEXT,
    text      TEXT NOT NULL,
    PRIMARY KEY (corpus_id, page)
);

CREATE TABLE IF NOT EXISTS chunks (
    id        INTEGER PRIMARY KEY,
    corpus_id INTEGER NOT NULL REFERENCES corpora(id) ON DELETE CASCADE,
    page      INTEGER NOT NULL,
    ord       INTEGER NOT NULL,
    heading   TEXT,
    text      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS chunks_corpus_page ON chunks(corpus_id, page);

CREATE TABLE IF NOT EXISTS embeddings (
    chunk_id INTEGER PRIMARY KEY REFERENCES chunks(id) ON DELETE CASCADE,
    dim      INTEGER NOT NULL,
    vec      BLOB NOT NULL
);

CREATE TABLE IF NOT EXISTS medicines (
    id        INTEGER PRIMARY KEY,
    corpus_id INTEGER NOT NULL REFERENCES corpora(id) ON DELETE CASCADE,
    name      TEXT NOT NULL,
    name_key  TEXT NOT NULL,
    form      TEXT,
    section   TEXT,
    page      INTEGER NOT NULL,
    raw       TEXT
);
CREATE INDEX IF NOT EXISTS medicines_key ON medicines(corpus_id, name_key);

CREATE TABLE IF NOT EXISTS monographs (
    name_key   TEXT NOT NULL,
    corpus_id  INTEGER NOT NULL,
    json       TEXT NOT NULL,
    model      TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (name_key, corpus_id)
);

CREATE TABLE IF NOT EXISTS asks (
    id       INTEGER PRIMARY KEY,
    ts       TEXT NOT NULL,
    question TEXT NOT NULL,
    answer   TEXT NOT NULL,
    model    TEXT,
    ms       INTEGER,
    pages    TEXT
);
"""


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = path or settings.db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    _ensure_fts(conn)
    return conn


def _ensure_fts(conn: sqlite3.Connection) -> None:
    """Full-text index for drug names and rare terms, where vectors are weak."""
    try:
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts "
            "USING fts5(text, chunk_id UNINDEXED, tokenize='porter unicode61')"
        )
        conn.commit()
    except sqlite3.OperationalError:
        # sqlite built without FTS5; retrieval falls back to vectors only.
        pass


def has_fts(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='chunks_fts'"
    ).fetchone()
    return row is not None


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def active_corpus(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM corpora ORDER BY id DESC LIMIT 1").fetchone()


def vectors(conn: sqlite3.Connection, corpus_id: int) -> tuple[np.ndarray, np.ndarray]:
    """All chunk vectors for a corpus as (ids, matrix)."""
    rows = conn.execute(
        "SELECT e.chunk_id, e.dim, e.vec FROM embeddings e "
        "JOIN chunks c ON c.id = e.chunk_id WHERE c.corpus_id = ? ORDER BY e.chunk_id",
        (corpus_id,),
    ).fetchall()
    if not rows:
        return np.empty(0, dtype=np.int64), np.empty((0, 0), dtype=np.float32)
    dim = rows[0]["dim"]
    ids = np.fromiter((r["chunk_id"] for r in rows), dtype=np.int64, count=len(rows))
    matrix = np.frombuffer(b"".join(r["vec"] for r in rows), dtype=np.float32)
    return ids, matrix.reshape(len(rows), dim)


def blob(vec: np.ndarray) -> bytes:
    return np.asarray(vec, dtype=np.float32).tobytes()
