"""Thin client for the CAIMEX gateway (OpenAI-compatible).

Two things in here are not boilerplate:

* ``trust_env=False``. The corporate proxy must never see these requests. The
  macOS bypass list names ``econetai.local`` but not ``econetai.co.zw``, which
  is the host the gateway actually lives on, so a proxy-aware client hangs or
  gets a proxy error page instead of JSON.
* Embeddings are returned L2-normalised here, once, so every consumer can use
  a plain dot product as cosine similarity.
"""

from __future__ import annotations

import json
import time
from typing import Any, Iterator, Sequence

import httpx
import numpy as np

from .config import settings

RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}
EMBED_BATCH = 32


class CaimexError(RuntimeError):
    pass


def _client(timeout: float = 120.0) -> httpx.Client:
    return httpx.Client(
        base_url=settings.base_url,
        headers={
            "Authorization": f"Bearer {settings.api_key}",
            "Content-Type": "application/json",
        },
        timeout=httpx.Timeout(timeout, connect=10.0),
        trust_env=False,  # never route the internal gateway through the proxy
    )


def _post(path: str, payload: dict[str, Any], *, timeout: float = 120.0,
          attempts: int = 4) -> dict[str, Any]:
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            with _client(timeout) as c:
                r = c.post(path, json=payload)
            if r.status_code in RETRY_STATUS:
                last = CaimexError(f"{r.status_code} from {path}: {r.text[:200]}")
                time.sleep(min(2 ** attempt, 8) * 0.5)
                continue
            if r.status_code >= 400:
                raise CaimexError(f"{r.status_code} from {path}: {r.text[:400]}")
            return r.json()
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last = exc
            time.sleep(min(2 ** attempt, 8) * 0.5)
    raise CaimexError(f"{path} failed after {attempts} attempts: {last}")


# --------------------------------------------------------------------------- #
# Embeddings
# --------------------------------------------------------------------------- #

def embed(texts: Sequence[str], *, model: str | None = None) -> np.ndarray:
    """Return an (n, d) float32 array of unit vectors."""
    model = model or settings.embed_model
    out: list[list[float]] = []
    for start in range(0, len(texts), EMBED_BATCH):
        batch = [t if t.strip() else "(blank)" for t in texts[start:start + EMBED_BATCH]]
        data = _post("/embeddings", {"model": model, "input": batch})
        rows = sorted(data["data"], key=lambda d: d.get("index", 0))
        out.extend(row["embedding"] for row in rows)
    arr = np.asarray(out, dtype=np.float32)
    norms = np.linalg.norm(arr, axis=1, keepdims=True)
    return arr / np.clip(norms, 1e-8, None)


# --------------------------------------------------------------------------- #
# Chat
# --------------------------------------------------------------------------- #

def chat(messages: list[dict[str, Any]], *, tools: list[dict] | None = None,
         model: str | None = None, temperature: float = 0.1,
         max_tokens: int = 1600) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": model or settings.chat_model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    data = _post("/chat/completions", payload)
    try:
        return data["choices"][0]["message"]
    except (KeyError, IndexError) as exc:
        raise CaimexError(f"unexpected chat response: {json.dumps(data)[:300]}") from exc


def stream_chat(messages: list[dict[str, Any]], *, model: str | None = None,
                temperature: float = 0.1, max_tokens: int = 1600) -> Iterator[str]:
    """Yield answer deltas as they arrive. Used for the final synthesis pass."""
    payload = {
        "model": model or settings.chat_model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": True,
    }
    with _client(180.0) as c, c.stream("POST", "/chat/completions", json=payload) as r:
        if r.status_code >= 400:
            body = r.read().decode("utf-8", "replace")
            raise CaimexError(f"{r.status_code} streaming: {body[:300]}")
        for line in r.iter_lines():
            if not line or not line.startswith("data:"):
                continue
            chunk = line[5:].strip()
            if chunk == "[DONE]":
                break
            try:
                delta = json.loads(chunk)["choices"][0].get("delta") or {}
            except (json.JSONDecodeError, KeyError, IndexError):
                continue
            piece = delta.get("content")
            if piece:
                yield piece


# --------------------------------------------------------------------------- #
# Health
# --------------------------------------------------------------------------- #

def health() -> dict[str, Any]:
    """Ping the gateway without spending tokens.

    ``GET /models`` answers even with a bad key, so this proves reachability,
    not authorisation. The first real chat call is the auth check.
    """
    started = time.perf_counter()
    try:
        with _client(15.0) as c:
            r = c.get("/models")
        return {
            "reachable": r.status_code == 200,
            "status": r.status_code,
            "ms": round((time.perf_counter() - started) * 1000),
            "models": len(r.json().get("data", [])) if r.status_code == 200 else 0,
        }
    except Exception as exc:  # noqa: BLE001 - surfaced to the UI as a string
        return {"reachable": False, "status": None,
                "ms": round((time.perf_counter() - started) * 1000),
                "error": f"{type(exc).__name__}: {exc}"}
