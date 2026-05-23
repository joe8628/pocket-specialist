"""OCR extraction on rendered page images via the Phase A provider layer.

Input:  document-scoped checkpoints/rendered/page_{N:04d}.png
Output: document-scoped checkpoints/ocr/page_{N:04d}.json

JSON schema:
  { "page_num": int, "image_path": str, "image_width": int, "image_height": int,
    "blocks": [ TextBlock.to_dict(), ... ], "ocr_provider": str, "ocr_metadata": dict }
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

from PIL import Image

from pocket_specialist.core.config import ocr_dir_for, render_dir_for
from pocket_specialist.storage.checkpoint import get_status, init_db, set_status, should_process
from pocket_specialist.core.gpu import gpu_scheduler
from pocket_specialist.ocr.providers import SuryaOCRProvider
from pocket_specialist.core.models import TextBlock


def _pnum(path: Path) -> int:
    return int(path.stem.split("_")[1])


def ocr_pages(
    document: str,
    render_dir: Path | None = None,
    ocr_dir: Path | None = None,
    start_page: Optional[int] = None,
    end_page: Optional[int] = None,
) -> tuple[int, int]:
    """Run OCR on rendered PNGs for one document. Returns (done, failed)."""

    render_dir = render_dir or render_dir_for(document)
    ocr_dir = ocr_dir or ocr_dir_for(document)

    pngs = sorted(render_dir.glob("page_*.png"))
    if not pngs:
        print(f"Error: no rendered pages found in {render_dir}. Run rasterization first.", file=sys.stderr)
        return 0, 0

    if start_page or end_page:
        lo, hi = start_page or 1, end_page or _pnum(pngs[-1])
        pngs = [path for path in pngs if lo <= _pnum(path) <= hi]

    ocr_dir.mkdir(parents=True, exist_ok=True)
    init_db()

    to_process: list[Path] = []
    skipped = pre_failed = 0
    for png in pngs:
        page_num = _pnum(png)
        if not should_process("ocr", document, page_num):
            status, _ = get_status("ocr", document, page_num)
            if status == "done":
                skipped += 1
            else:
                print(f"  [ocr] page {page_num}: exhausted retries, skipping.")
                pre_failed += 1
        else:
            to_process.append(png)

    if not to_process:
        print(f"OCR: all pages already processed ({skipped} done, {pre_failed} exhausted).")
        return 0, pre_failed

    provider = SuryaOCRProvider()
    done = failed = 0

    print("Loading OCR provider (GPU)...")
    with gpu_scheduler.claim("ocr"):
        provider.load()
        print(f"OCR provider loaded: {provider.name}.")

        for png in to_process:
            page_num = _pnum(png)
            try:
                image = Image.open(png).convert("RGB")
                width, height = image.size
                image_bytes = png.read_bytes()
                result = provider.extract(image_bytes=image_bytes, region_type="page")
                blocks = [TextBlock.from_dict(block) for block in result.typed_content["blocks"]]
                ordered = sorted(blocks, key=lambda block: block.bbox.y0)

                record = {
                    "page_num": page_num,
                    "image_path": str(png),
                    "image_width": width,
                    "image_height": height,
                    "blocks": [block.to_dict() for block in ordered],
                    "ocr_provider": result.provider,
                    "ocr_metadata": result.extraction_metadata,
                }
                out_path = ocr_dir / f"page_{page_num:04d}.json"
                out_path.write_text(json.dumps(record, indent=2, ensure_ascii=False))

                set_status("ocr", document, page_num, "done", str(out_path))
                done += 1
                print(
                    f"  [ocr] page {page_num} → {out_path.name}  "
                    f"({len(ordered)} blocks, {result.latency_ms} ms)"
                )
            except Exception as exc:
                set_status("ocr", document, page_num, "failed")
                failed += 1
                print(f"  [ocr] page {page_num}: FAILED — {exc}")

        provider.offload()

    print(f"\nOCR complete: {done} done, {skipped} skipped, {failed + pre_failed} failed.")
    return done, failed + pre_failed
