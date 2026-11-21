"""Turning OCR page files into the units we index.

Only corrections that cannot be confused with a dose are applied here. The
OCR of an EDLIZ scan merges table columns and misreads route abbreviations
(`ivisc` for `iv/sc`), and a wrong dose is worse than a missing one, so
numbers are left exactly as the OCR found them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# Route abbreviations only. These are never dosages.
_ROUTE_FIXES = [
    (re.compile(r"\biv[/|]?isc\b"), "iv/sc"),
    (re.compile(r"\biv[/|]?lim\b"), "iv/im"),
    (re.compile(r"\biv[/|]?lpo\b"), "iv/po"),
    (re.compile(r"\biv[/|]?im[/|]?po\b"), "iv/im/po"),
    (re.compile(r"\blv(?=\s*[/|])"), "iv"),
    (re.compile(r"\bpo(?=[A-Z])"), "po "),
]

_RULE = re.compile(r"_{3,}|-{4,}")
_SPACES = re.compile(r"[ \t]{2,}")
_HEADING = re.compile(
    r"^\s*(?:chapter\s+)?(\d{1,2}(?:\.\d{1,2}){0,2})\.?\s+([A-Z][A-Za-z0-9 ,'&/()-]{3,70})\s*$",
    re.MULTILINE,
)
_CAPS_HEADING = re.compile(r"^([A-Z][A-Z0-9 ,'&/()\-:.]{5,70})$")
_NOT_A_HEADING = {
    "GENERAL NOTE", "GENERAL NOTES", "NOTE", "NOTES", "CAUTION", "WARNING",
    "PRECAUTIONS", "KEY POINTS", "SUMMARY", "INTRODUCTION", "DEFINITION",
    "DEFINITIONS", "GENERAL GUIDELINES", "GUIDELINES",
}


def normalise(raw: str) -> str:
    text = raw.replace("\u2019", "'").replace("\u2013", "-").replace("\u2014", "-")
    text = _RULE.sub("", text)
    text = re.sub(r"(\d)\s*to\s*(\d)", r"\1 to \2", text)
    for pattern, repl in _ROUTE_FIXES:
        text = pattern.sub(repl, text)
    text = _SPACES.sub(" ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def page_label(text: str) -> str | None:
    """A truthful label for one page when no chapter can be established.

    The top of an EDLIZ page carries its own subject: a table caption or a
    heading. That is page-specific and therefore never wrong, unlike a chapter
    guessed from a distant divider.
    """
    bad = re.compile(r"(medicine\s*codes|adult\s*dose|freq|duration|^\W*$)", re.I)
    for line in text.splitlines()[:6]:
        line = line.strip()
        if len(line) < 4 or len(line) > 60:
            continue
        if not line[0].isupper():
            continue  # a sentence carried over from the previous page
        if line.endswith((",", ";", "-")):
            continue
        if "(" in line and ")" not in line:
            line = line.split("(")[0].strip()  # caption cut off by OCR
        if bad.search(line):
            continue  # table column headings
        if re.search(r",\s", line) and len(line.split()) > 5:
            continue  # a sentence, not a title
        if re.fullmatch(r"[A-Z]{2,6}\s*20\d\d", line):
            continue  # "EDLIZ 2020" read by OCR as "DUIS 2020"
        if re.fullmatch(r"[\d\s.,:;|/()-]+", line):
            continue
        if len(line.split()) == 1 and len(line) < 8:
            continue
        return line.rstrip(" .:")
    return None


def caps_candidates(text: str) -> list[str]:
    """Capitalised lines at the top of the page, in order.

    Only the head of the page counts: a running section header sits there,
    whereas capitals further down are table labels and figure captions that
    repeat for unrelated reasons.
    """
    out: list[str] = []
    head = [l.strip() for l in text.splitlines() if l.strip()][:3]
    for line in head:
        line = line.strip()
        if len(line) < 6 or "EDLIZ 20" in line or line[0].isdigit():
            continue
        if " 2020" in line:
            continue
        if not _CAPS_HEADING.match(line) or line.strip(" .:") in _NOT_A_HEADING:
            continue
        if len(line.split()) < 2:
            continue
        letters = [c for c in line if c.isalpha()]
        if letters and sum(c.isupper() for c in letters) / len(letters) > 0.85:
            out.append(line.strip(" .:"))
    return out


_PROSE = {
    "the", "is", "are", "was", "were", "should", "must", "if", "when", "this",
    "that", "these", "those", "give", "gives", "be", "been", "has", "have",
    "may", "can", "will", "would", "do", "does", "not", "any", "all", "each",
    "there", "their", "they", "you", "your", "we", "us", "it", "its",
}


def numbered_heading(text: str) -> str | None:
    best: tuple[int, str] | None = None
    # A chapter title sits at the top of its divider page. Looking further down
    # the page picks up table-of-contents lines that mention other chapters.
    head = "\n".join([l.strip() for l in text.splitlines() if l.strip()][:3])
    for m in _HEADING.finditer(head):
        number, title = m.group(1), m.group(2).strip()
        words = title.split()
        letters = [c for c in title if c.isalpha()]
        is_caps = bool(letters) and sum(c.isupper() for c in letters) / len(letters) > 0.8
        if len(words) > 8:
            continue
        if len(words) < 2 and not (is_caps and len(title) >= 6):
            continue  # "5 IMMUNISATION" is a chapter, "Some word" is not
        if re.search(r"\d\s*$", title):      # "Any visible Any visible 260"
            continue
        lowered = [w.lower().strip(".,;:") for w in words]
        if not is_caps and any(w in _PROSE for w in lowered):
            continue  # a sentence, not a title
        score = 2 if "." in number else 1
        if best is None or score >= best[0]:
            best = (score, f"{number} {title}")
    return best[1] if best else None


@dataclass
class Chunk:
    page: int
    index: int
    heading: str | None
    text: str


def _paragraphs(text: str) -> list[str]:
    blocks = [b.strip() for b in re.split(r"\n\s*\n", text)]
    out: list[str] = []
    for block in blocks:
        if not block:
            continue
        # A table block is many short lines; keep it whole so a drug row is
        # never split away from its dose.
        if len(block.splitlines()) > 3 and block.count("\n") > 3:
            out.append(block)
        else:
            out.extend(s.strip() for s in re.split(r"(?<=[.;:])\s+", block) if s.strip())
    return out


def chunk_page(page: int, raw: str, heading: str | None = None,
               target: int = 1000, overlap: int = 150) -> list[Chunk]:
    text = normalise(raw)
    if len(text) < 40:
        return []
    heading = heading or numbered_heading(text)
    chunks: list[Chunk] = []
    buf = ""
    for para in _paragraphs(text):
        if len(buf) + len(para) + 1 <= target:
            buf = f"{buf}\n{para}".strip()
            continue
        if buf:
            chunks.append(Chunk(page, len(chunks), heading, buf))
            buf = buf[-overlap:] if overlap else ""
        # A single oversized paragraph is hard-split on word boundaries.
        while len(para) > target:
            cut = para.rfind(" ", 0, target)
            cut = cut if cut > target // 2 else target
            chunks.append(Chunk(page, len(chunks), heading, para[:cut].strip()))
            para = para[cut - overlap:].strip() if overlap else para[cut:].strip()
        buf = f"{buf}\n{para}".strip()
    if buf.strip():
        chunks.append(Chunk(page, len(chunks), heading, buf))
    return chunks


def load_pages(text_dir: Path) -> list[tuple[int, str]]:
    pages: list[tuple[int, str]] = []
    for path in sorted(text_dir.glob("page-*.txt")):
        try:
            number = int(path.stem.split("-")[1])
        except (IndexError, ValueError):
            continue
        pages.append((number, path.read_text(encoding="utf-8", errors="replace")))
    return pages
