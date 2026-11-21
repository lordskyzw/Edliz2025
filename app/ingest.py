"""Build the searchable corpus from OCR text.

    python -m app.ingest --text data/text/edliz-2020 --edition "8th edition (2020)"

Re-running replaces the previous corpus, so switching editions is one command.
Embeddings are computed here, once, and stored; queries embed one string.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from . import caimex, db, medicines, retrieval, textproc


def build(text_dir: Path, edition: str, db_path: Path | None = None,
          source: str | None = None) -> dict:
    if not text_dir.exists():
        raise SystemExit(f"no OCR text at {text_dir}; run scripts/ocr_pdf.py first")

    pages = textproc.load_pages(text_dir)
    if not pages:
        raise SystemExit(f"{text_dir} contains no page-*.txt files")

    # Pass 1: a capitalised line is only a section title if it repeats across
    # pages. Front matter (funder names, committee lists) appears once and is
    # never accepted as a heading.
    caps_by_page: dict[int, list[str]] = {}
    seen_on: dict[str, int] = {}
    for number, raw in pages:
        caps = textproc.caps_candidates(textproc.normalise(raw))
        caps_by_page[number] = caps
        for c in set(caps):
            seen_on[c] = seen_on.get(c, 0) + 1

    headings: dict[int, str | None] = {}
    for number, raw in pages:
        text = textproc.normalise(raw)
        heading = textproc.numbered_heading(text)
        if not heading:
            heading = next((c for c in caps_by_page.get(number, [])
                            if seen_on.get(c, 0) >= 2), None)
        headings[number] = heading

    # A section runs until the next one starts. Where detection missed a
    # heading, carry the previous one forward so the list has a usable column
    # instead of a blank one.
    last: str | None = None
    last_page = 0
    for number in sorted(headings):
        if headings[number]:
            last, last_page = headings[number], number
        elif last and number - last_page <= 3:
            headings[number] = last
        else:
            headings[number] = None

    # Still nothing? Use the page's own header. It describes that page only,
    # so it is true even when the chapter cannot be established.
    for number, raw in pages:
        if not headings[number]:
            headings[number] = textproc.page_label(textproc.normalise(raw))

    conn = db.connect(db_path)
    started = time.perf_counter()

    conn.execute("DELETE FROM corpora")  # cascades to pages/chunks/embeddings
    if db.has_fts(conn):
        conn.execute("DELETE FROM chunks_fts")
    cur = conn.execute(
        "INSERT INTO corpora (edition, source, created_at) VALUES (?,?,?)",
        (edition, source or str(text_dir), db.now()),
    )
    corpus_id = int(cur.lastrowid)

    chunks: list[textproc.Chunk] = []
    for number, raw in pages:
        text = textproc.normalise(raw)
        conn.execute(
            "INSERT OR REPLACE INTO pages (corpus_id, page, heading, text) VALUES (?,?,?,?)",
            (corpus_id, number, headings[number], text),
        )
        chunks.extend(textproc.chunk_page(number, raw, headings[number]))
    conn.commit()
    print(f"  {len(pages)} pages -> {len(chunks)} chunks", file=sys.stderr)

    ids: list[int] = []
    for ch in chunks:
        cur = conn.execute(
            "INSERT INTO chunks (corpus_id, page, ord, heading, text) VALUES (?,?,?,?,?)",
            (corpus_id, ch.page, ch.index, ch.heading, ch.text),
        )
        ids.append(cur.lastrowid)
        if db.has_fts(conn):
            conn.execute(
                "INSERT INTO chunks_fts (text, chunk_id) VALUES (?, ?)",
                (ch.text, cur.lastrowid),
            )
    conn.commit()

    print(f"  embedding {len(chunks)} chunks with {caimex.settings.embed_model}",
          file=sys.stderr)
    vectors = caimex.embed([c.text for c in chunks])
    conn.executemany(
        "INSERT OR REPLACE INTO embeddings (chunk_id, dim, vec) VALUES (?,?,?)",
        [(cid, vectors.shape[1], db.blob(vec)) for cid, vec in zip(ids, vectors)],
    )
    conn.commit()

    stats = medicines.extract(conn, corpus_id)
    print(f"  digitised {stats['rows']} medicine rows "
          f"({stats['distinct_medicines']} distinct)", file=sys.stderr)

    conn.execute(
        "UPDATE corpora SET edition=?, source=?, pages=?, chunks=?, created_at=? WHERE id=?",
        (edition, source or str(text_dir), len(pages), len(chunks), db.now(), corpus_id),
    )
    conn.commit()
    retrieval.reset_cache()

    elapsed = round(time.perf_counter() - started, 1)
    print(f"done in {elapsed}s: corpus {corpus_id} '{edition}'", file=sys.stderr)
    return {"corpus_id": corpus_id, "pages": len(pages), "chunks": len(chunks),
            **stats, "seconds": elapsed}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--text", type=Path, required=True, help="directory of page-*.txt")
    ap.add_argument("--edition", default="8th edition (2020)")
    ap.add_argument("--source")
    ap.add_argument("--db", type=Path)
    args = ap.parse_args()
    build(args.text, args.edition, args.db, args.source)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
