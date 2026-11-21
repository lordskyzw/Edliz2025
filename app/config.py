"""Settings, read from the environment or a .env file next to the project."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Minimal .env reader: KEY=value, # comments, no interpolation.

    Deliberately does not override variables already in the environment, so
    `CAIMEX_API_KEY=... uvicorn ...` still wins over the file.
    """
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


_load_dotenv(ROOT / ".env")


def _flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off", ""}


def _path(name: str, default: str) -> Path:
    p = Path(os.environ.get(name, default)).expanduser()
    return p if p.is_absolute() else ROOT / p


@dataclass(frozen=True)
class Settings:
    base_url: str = field(default_factory=lambda: os.environ.get(
        "CAIMEX_BASE_URL", "https://caimex.econetai.co.zw:2052/v1").rstrip("/"))
    api_key: str = field(default_factory=lambda: os.environ.get("CAIMEX_API_KEY", ""))

    chat_model: str = field(default_factory=lambda: os.environ.get(
        "EDLIZ_CHAT_MODEL", "deepseek-v4-flash"))
    embed_model: str = field(default_factory=lambda: os.environ.get(
        "EDLIZ_EMBED_MODEL", "bge-m3"))
    rerank_model: str = field(default_factory=lambda: os.environ.get(
        "EDLIZ_RERANK_MODEL", "bge-reranker-v2-m3"))

    edition: str = field(default_factory=lambda: os.environ.get(
        "EDLIZ_EDITION", "8th edition (2020)"))
    db_path: Path = field(default_factory=lambda: _path("EDLIZ_DB", "data/edliz.db"))
    text_dir: Path = field(default_factory=lambda: _path(
        "EDLIZ_TEXT_DIR", "data/text/edliz-2020"))

    web_search: bool = field(default_factory=lambda: _flag("EDLIZ_WEB_SEARCH", True))
    max_web_results: int = field(
        default_factory=lambda: int(os.environ.get("EDLIZ_MAX_WEB_RESULTS", "5")))

    top_k: int = field(default_factory=lambda: int(os.environ.get("EDLIZ_TOP_K", "8")))

    @property
    def configured(self) -> bool:
        return bool(self.api_key) and bool(self.base_url)


settings = Settings()
