"""Document rasterization utilities for page-level image generation via PyMuPDF."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import cast

import fitz  # PyMuPDF

from config import RENDER_ZOOM, document_slug, render_dir_for
from pipeline.checkpoint import get_status, init_db, set_status, should_process


def render_page(doc: fitz.Document, page_num: int, output_dir: Path, zoom: float) -> Path:
    """Render one 1-indexed page to PNG. Returns the saved file path."""
    mat = fitz.Matrix(zoom, zoom)
    page = cast(fitz.Page, doc[page_num - 1])  # fitz is 0-indexed
    pix = page.get_pixmap(matrix=mat)
    out = output_dir / f"page_{page_num:04d}.png"
    pix.save(str(out))
    return out


def render_pdf(
    pdf_path: Path,
    output_dir: Path | None = None,
    zoom: float = RENDER_ZOOM,
    start_page: int = 1,
    end_page: int | None = None,
) -> tuple[int, int]:
    """
    Render a range of pages from *pdf_path* to PNG files in *output_dir*.

    Skips pages already marked done in the checkpoint DB for this document.
    Retries previously-failed pages up to MAX_RETRIES times.

    Returns (n_done, n_failed).
    """
    if not pdf_path.exists():
        print(f"Error: PDF not found: {pdf_path}", file=sys.stderr)
        sys.exit(1)

    document = document_slug(pdf_path)
    output_dir = output_dir or render_dir_for(document)
    output_dir.mkdir(parents=True, exist_ok=True)
    init_db()

    try:
        doc = fitz.open(str(pdf_path))
    except Exception as exc:
        print(f"Error: cannot open PDF — {exc}", file=sys.stderr)
        sys.exit(1)

    if doc.is_encrypted:
        print("Error: PDF is encrypted. Decrypt the file and retry.", file=sys.stderr)
        doc.close()
        sys.exit(1)

    total = len(doc)
    end = min(end_page, total) if end_page else total

    if start_page < 1 or start_page > total:
        print(f"Error: start_page {start_page} out of range (1–{total}).", file=sys.stderr)
        doc.close()
        sys.exit(1)

    done = failed = skipped = 0

    for page_num in range(start_page, end + 1):
        if not should_process("render", document, page_num):
            status, attempts = get_status("render", document, page_num)
            if status == "done":
                skipped += 1
            else:
                print(f"  [render] page {page_num}: exhausted retries, skipping.")
                failed += 1
            continue

        try:
            out_path = render_page(doc, page_num, output_dir, zoom)
            set_status("render", document, page_num, "done", str(out_path))
            done += 1
            print(f"  [render] page {page_num}/{end} → {out_path.name}")
        except Exception as exc:
            set_status("render", document, page_num, "failed")
            failed += 1
            print(f"  [render] page {page_num}: FAILED — {exc}")

    doc.close()
    print(f"\nRender complete: {done} done, {skipped} skipped, {failed} failed.")
    return done, failed
