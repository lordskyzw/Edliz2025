#!/usr/bin/env python3
"""Turn a scanned PDF into per-page text.

The EDLIZ editions circulate as scans: the PDF has no text layer, so
``pypdf`` returns empty strings for every page. This script renders each page
to a PNG with poppler's ``pdftoppm`` and OCRs it with ``tesseract``, writing
``<out>/page-0001.txt`` and a ``<out>/index.json`` manifest.

Pages that already carry a text layer are copied straight through, so it is
safe to run this on a born-digital PDF as well.

Usage:
    python scripts/ocr_pdf.py data/EDLIZ-2020.pdf --out data/text/edliz-2020

Requires poppler (``pdftoppm``) and ``tesseract`` on PATH:
    brew install poppler tesseract
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

DPI = 200
MIN_TEXT_CHARS = 200  # below this we assume the page is a scan or a plate


def page_count(pdf: Path) -> int:
    from pypdf import PdfReader

    return len(PdfReader(str(pdf)).pages)


def embedded_text(pdf: Path) -> list[str]:
    from pypdf import PdfReader

    return [(p.extract_text() or "") for p in PdfReader(str(pdf)).pages]


def render(pdf: Path, page: int, dpi: int, out_dir: Path) -> Path:
    """Render one 1-based page to PNG and return the path."""
    prefix = out_dir / f"render-{page:04d}"
    subprocess.run(
        [
            "pdftoppm",
            "-f", str(page),
            "-l", str(page),
            "-r", str(dpi),
            "-gray",
            "-png",
            str(pdf),
            str(prefix),
        ],
        check=True,
        capture_output=True,
    )
    # pdftoppm zero-pads to the width of the last page number, so glob.
    matches = sorted(out_dir.glob(f"render-{page:04d}*.png"))
    if not matches:
        raise RuntimeError(f"pdftoppm produced nothing for page {page}")
    return matches[0]


def ocr_one(args: tuple[str, int, int, str]) -> tuple[int, str]:
    pdf_s, page, dpi, tmp_s = args
    pdf, tmp = Path(pdf_s), Path(tmp_s)
    img = render(pdf, page, dpi, tmp)
    # --psm 6 keeps table rows on their own lines, which matters here: the
    # dosage tables are most of the book.
    proc = subprocess.run(
        ["tesseract", str(img), "stdout", "-l", "eng", "--psm", "6"],
        capture_output=True,
        text=True,
        check=False,
    )
    img.unlink(missing_ok=True)
    text = proc.stdout if proc.returncode == 0 else ""
    return page, text.strip()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("pdf", type=Path)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--dpi", type=int, default=DPI)
    ap.add_argument("--workers", type=int, default=max(2, (os.cpu_count() or 4) // 2))
    ap.add_argument("--first", type=int, default=1)
    ap.add_argument("--last", type=int, default=0, help="0 means to the end")
    ap.add_argument("--force", action="store_true", help="re-OCR pages already done")
    ap.add_argument("--text-layer", action="store_true",
                    help="use the PDF's own text layer instead of OCR")
    args = ap.parse_args()

    if not args.pdf.exists():
        print(f"no such file: {args.pdf}", file=sys.stderr)
        return 2

    args.out.mkdir(parents=True, exist_ok=True)
    total = page_count(args.pdf)
    last = args.last or total
    pages = list(range(args.first, min(last, total) + 1))

    existing: dict[int, int] = {}
    for p in pages:
        f = args.out / f"page-{p:04d}.txt"
        if f.exists() and not args.force:
            existing[p] = len(f.read_text(encoding="utf-8", errors="replace"))

    todo = [p for p in pages if p not in existing]
    print(f"{args.pdf.name}: {total} pages, {len(existing)} already done, {len(todo)} to do",
          file=sys.stderr)

    if args.text_layer:
        texts = embedded_text(args.pdf)
        for p in todo:
            t = texts[p - 1].strip()
            (args.out / f"page-{p:04d}.txt").write_text(t, encoding="utf-8")
            existing[p] = len(t)
        todo = []

    if todo:
        with tempfile.TemporaryDirectory(prefix="edliz-ocr-") as tmp:
            jobs = [(str(args.pdf), p, args.dpi, tmp) for p in todo]
            with ProcessPoolExecutor(max_workers=args.workers) as pool:
                for i, (page, text) in enumerate(pool.map(ocr_one, jobs), 1):
                    (args.out / f"page-{page:04d}.txt").write_text(text, encoding="utf-8")
                    existing[page] = len(text)
                    if i % 10 == 0 or i == len(jobs):
                        print(f"  OCR {i}/{len(jobs)} (page {page}, {len(text)} chars)",
                              file=sys.stderr)

    blank = [p for p in pages if existing.get(p, 0) < MIN_TEXT_CHARS]
    manifest = {
        "pdf": str(args.pdf),
        "pages": total,
        "ocr": [p for p in sorted(existing)],
        "likely_blank_pages": blank,
        "dpi": args.dpi,
    }
    (args.out / "index.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"done: {len(existing)} pages in {args.out}; "
          f"{len(blank)} pages look blank/plate", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
