"""Hybrid retrieval over the corpus.

Vector search alone loses drug names: `bge-m3` will happily return a page
about malaria for a query about `artemether-lumefantrine 20/120` because the
prose is semantically close. Keyword search alone loses questions phrased in
a patient's words. So the two are fused by reciprocal rank, and a chunk that
both methods like wins.

Fusion, not score averaging: the two scores are not on the same scale and
normalising them hides that.
"""

from __future__ import annotations

import re
import sqlite3
import threading
from dataclasses import dataclass, field

import numpy as np

from . import caimex, db

_CACHE: dict[int, tuple[np.ndarray, np.ndarray]] = {}
_LOCK = threading.Lock()

STOP = {
    "the", "a", "an", "of", "for", "in", "on", "to", "and", "or", "is", "are",
    "what", "how", "do", "does", "should", "can", "with", "at", "by", "be",
    "it", "this", "that", "from", "as", "if", "any", "my", "i",
}


@dataclass
class Hit:
    chunk_id: int
    page: int
    heading: str | None
    text: str
    score: float = 0.0
    vector_rank: int | None = None
    keyword_rank: int | None = None
    extra: dict = field(default_factory=dict)


def _matrix(conn: sqlite3.Connection, corpus_id: int) -> tuple[np.ndarray, np.ndarray]:
    with _LOCK:
        cached = _CACHE.get(corpus_id)
        if cached is None:
            cached = db.vectors(conn, corpus_id)
            _CACHE[corpus_id] = cached
        return cached


def reset_cache() -> None:
    with _LOCK:
        _CACHE.clear()


def _vector_search(conn: sqlite3.Connection, corpus_id: int, query: str,
                   limit: int = 40) -> list[tuple[int, float]]:
    ids, matrix = _matrix(conn, corpus_id)
    if ids.size == 0:
        return []
    q = caimex.embed([query])[0]
    if matrix.shape[1] != q.shape[0]:
        return []  # index built with a different embedding model
    scores = matrix @ q
    take = min(limit, scores.size)
    top = np.argpartition(-scores, take - 1)[:take]
    top = top[np.argsort(-scores[top])]
    return [(int(ids[i]), float(scores[i])) for i in top]


def _fts_query(query: str) -> str | None:
    terms = [t for t in re.findall(r"[A-Za-z0-9][A-Za-z0-9./-]*", query)
             if t.lower() not in STOP and len(t) > 1]
    if not terms:
        return None
    return " OR ".join(f'"{t}"' for t in terms[:12])


def _keyword_search(conn: sqlite3.Connection, corpus_id: int, query: str,
                    limit: int = 40) -> list[tuple[int, float]]:
    if not db.has_fts(conn):
        return []
    match = _fts_query(query)
    if not match:
        return []
    try:
        rows = conn.execute(
            "SELECT f.chunk_id, bm25(chunks_fts) AS rank FROM chunks_fts f "
            "JOIN chunks c ON c.id = f.chunk_id "
            "WHERE chunks_fts MATCH ? AND c.corpus_id = ? "
            "ORDER BY rank LIMIT ?",
            (match, corpus_id, limit),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [(int(r["chunk_id"]), -float(r["rank"])) for r in rows]


def search(conn: sqlite3.Connection, corpus_id: int, query: str,
           k: int = 8) -> list[Hit]:
    """Return the k best chunks for `query`, fused across both searches."""
    vector = _vector_search(conn, corpus_id, query)
    keyword = _keyword_search(conn, corpus_id, query)

    fused: dict[int, Hit] = {}
    for rank, (chunk_id, score) in enumerate(vector, 1):
        fused.setdefault(chunk_id, Hit(chunk_id, 0, None, "", score))
        fused[chunk_id].vector_rank = rank
        fused[chunk_id].extra["cos"] = round(score, 4)
    for rank, (chunk_id, _score) in enumerate(keyword, 1):
        hit = fused.setdefault(chunk_id, Hit(chunk_id, 0, None, ""))
        hit.keyword_rank = rank

    def rrf(hit: Hit) -> float:
        total = 0.0
        if hit.vector_rank:
            total += 1.0 / (60 + hit.vector_rank)
        if hit.keyword_rank:
            total += 1.0 / (60 + hit.keyword_rank)
        if hit.vector_rank and hit.keyword_rank:
            total *= 1.15  # reward agreement between the two signals
        return total

    ranked = sorted(fused.values(), key=rrf, reverse=True)[: max(k * 4, 24)]
    if not ranked:
        return []

    placeholders = ",".join("?" * len(ranked))
    rows = conn.execute(
        f"SELECT id, page, heading, text FROM chunks WHERE id IN ({placeholders})",
        [h.chunk_id for h in ranked],
    ).fetchall()
    by_id = {r["id"]: r for r in rows}
    out: list[Hit] = []
    for hit in ranked:
        row = by_id.get(hit.chunk_id)
        if not row:
            continue
        hit.page, hit.heading, hit.text = row["page"], row["heading"], row["text"]
        hit.score = round(rrf(hit), 6)
        out.append(hit)
    return out[:k]


def context_block(hits: list[Hit], max_chars: int = 12000) -> str:
    """Numbered excerpts for the prompt, each tagged with its page."""
    parts: list[str] = []
    used = 0
    for i, hit in enumerate(hits, 1):
        header = f"[S{i}] page {hit.page}" + (f" - {hit.heading}" if hit.heading else "")
        body = hit.text[:2600]
        if used + len(body) > max_chars:
            break
        used += len(body)
        parts.append(f"{header}\n{body}")
    return "\n\n".join(parts)
