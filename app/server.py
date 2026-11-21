"""HTTP API and static UI.

Run:
    .venv/bin/uvicorn app.server:app --reload --port 8077

Every request opens its own SQLite connection. FastAPI runs sync endpoints on
a threadpool and SQLite connections are bound to the thread that made them,
so a shared connection would fail on the second concurrent request.
"""

from __future__ import annotations

import json
import queue
import threading

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import agent, caimex, db, medicines
from .config import ROOT, settings
from .retrieval import search as search_chunks

WEB = ROOT / "web"

app = FastAPI(title="EDLIZ Assistant", version="0.1.0")


def get_conn():
    conn = db.connect()
    try:
        yield conn
    finally:
        conn.close()


def _corpus(conn):
    corpus = db.active_corpus(conn)
    if corpus is None:
        raise HTTPException(503, "no corpus ingested; run: python -m app.ingest")
    return corpus


@app.get("/api/status")
def status(conn=Depends(get_conn)) -> dict:
    corpus = db.active_corpus(conn)
    return {
        "ready": corpus is not None,
        "corpus": dict(corpus) if corpus else None,
        "medicines": (conn.execute(
            "SELECT COUNT(DISTINCT name_key) FROM medicines WHERE corpus_id = ?",
            (corpus["id"],)).fetchone()[0] if corpus else 0),
        "configured_edition": settings.edition,
        "edition_drift": bool(corpus) and corpus["edition"] != settings.edition,
        "models": {"chat": settings.chat_model, "embed": settings.embed_model},
        "web_search": settings.web_search,
        "caimex": caimex.health(),
        "asked": conn.execute("SELECT COUNT(*) FROM asks").fetchone()[0],
    }


@app.post("/api/ask")
def ask(payload: dict) -> StreamingResponse:
    question = (payload.get("question") or "").strip()
    if not question:
        raise HTTPException(400, "question is required")
    if len(question) > 2000:
        raise HTTPException(400, "question too long")

    events: queue.Queue = queue.Queue()

    def worker() -> None:
        conn = db.connect()  # this thread's own connection
        try:
            agent.stream(conn, question, lambda ev: events.put(ev))
        except Exception as exc:  # noqa: BLE001 - reported to the browser
            events.put(("error", {"message": f"{type(exc).__name__}: {exc}"}))
        finally:
            events.put(("end", {}))
            conn.close()

    threading.Thread(target=worker, daemon=True).start()

    def sse():
        while True:
            kind, data = events.get()
            if kind == "end":
                break
            yield f"event: {kind}\ndata: {json.dumps(data)}\n\n"

    return StreamingResponse(
        sse(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/medicines")
def list_medicines(q: str = "", letter: str = "",
                   limit: int = Query(200, le=500), conn=Depends(get_conn)) -> dict:
    corpus = _corpus(conn)
    if q.strip():
        return {"items": medicines.search(conn, corpus["id"], q, limit=limit),
                "mode": "search"}
    return {"items": medicines.top(conn, corpus["id"], letter, limit), "mode": "browse"}


@app.get("/api/medicine")
def medicine_detail(name: str, conn=Depends(get_conn)) -> dict:
    corpus = _corpus(conn)
    rows = medicines.search(conn, corpus["id"], name, limit=5)
    if not rows:
        raise HTTPException(404, "medicine not in the digitised index")
    best = rows[0]
    detail = conn.execute(
        "SELECT name, form, section, page, raw FROM medicines "
        "WHERE corpus_id=? AND name_key=? ORDER BY page LIMIT 40",
        (corpus["id"], best["name_key"]),
    ).fetchall()
    pages = sorted({r["page"] for r in detail})
    placeholders = ",".join("?" * len(pages)) or "NULL"
    excerpts = conn.execute(
        f"SELECT page, text FROM pages WHERE corpus_id=? AND page IN ({placeholders}) "
        "ORDER BY page",
        [corpus["id"], *pages],
    ).fetchall()
    return {
        "name": best["name"],
        "form": best["form"],
        "section": best["section"],
        "pages": pages,
        "rows": [dict(r) for r in detail],
        "excerpts": [{"page": r["page"], "text": r["text"][:2500]} for r in excerpts],
    }


@app.get("/api/page/{number}")
def page(number: int, conn=Depends(get_conn)) -> dict:
    corpus = _corpus(conn)
    row = conn.execute(
        "SELECT page, heading, text FROM pages WHERE corpus_id=? AND page=?",
        (corpus["id"], number),
    ).fetchone()
    if not row:
        raise HTTPException(404, f"page {number} not in corpus")
    return dict(row)


@app.get("/api/search")
def search_preview(q: str, k: int = 8, conn=Depends(get_conn)) -> dict:
    corpus = _corpus(conn)
    hits = search_chunks(conn, corpus["id"], q, k=min(k, 20))
    return {"hits": [{"page": h.page, "heading": h.heading, "text": h.text[:600],
                      "score": h.score, "cos": h.extra.get("cos")} for h in hits]}


@app.get("/api/history")
def history(limit: int = 20, conn=Depends(get_conn)) -> dict:
    rows = conn.execute(
        "SELECT ts, question, answer, ms, pages FROM asks ORDER BY id DESC LIMIT ?",
        (min(limit, 100),),
    ).fetchall()
    return {"items": [dict(r) for r in rows]}


app.mount("/static", StaticFiles(directory=WEB), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB / "index.html")
