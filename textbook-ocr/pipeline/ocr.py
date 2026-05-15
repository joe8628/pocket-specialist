"""Stage 2: Surya OCR on rendered page images.

Input:  checkpoints/rendered/page_{N:04d}.png
Output: checkpoints/ocr/page_{N:04d}.json

JSON schema:
  { "page_num": int, "image_path": str, "image_width": int, "image_height": int,
    "blocks": [ TextBlock.to_dict(), ... ] }   # column-reordered, block_type=unknown
"""
from __future__ import annotations
import gc
import json
import sys
from pathlib import Path
from typing import Optional

import torch
from PIL import Image

from config import OCR_DIR, RENDER_DIR
from pipeline.checkpoint import init_db, set_status, should_process, get_status
from pipeline.layout import reorder_columns
from pipeline.models import BlockType, BoundingBox, TextBlock


def _load_surya():
    try:
        from surya.detection import DetectionPredictor
        from surya.foundation import FoundationPredictor
        from surya.recognition import RecognitionPredictor
        from surya.settings import settings
    except ImportError:
        print("Error: surya-ocr is not installed. Run: pip install surya-ocr", file=sys.stderr)
        sys.exit(1)

    print("  loading detection model...")
    det = DetectionPredictor()
    print("  loading recognition model...")
    foundation = FoundationPredictor(checkpoint=settings.RECOGNITION_MODEL_CHECKPOINT)
    rec = RecognitionPredictor(foundation)
    return det, rec, foundation


def _surya_ocr_page(
    image: Image.Image,
    det_predictor,
    rec_predictor,
) -> list[TextBlock]:
    """Run Surya on one page image. Returns TextBlocks (not yet column-reordered)."""
    results = rec_predictor([image], det_predictor=det_predictor)
    blocks: list[TextBlock] = []
    for line in results[0].text_lines:
        if not line.text.strip():
            continue
        x0, y0, x1, y1 = line.bbox
        blocks.append(TextBlock(
            bbox=BoundingBox(x0=x0, y0=y0, x1=x1, y1=y1),
            raw_text=line.text,
            confidence=float(line.confidence or 0.0),
            block_type=BlockType.UNKNOWN,
        ))
    return blocks


def ocr_pages(
    render_dir: Path = RENDER_DIR,
    ocr_dir: Path = OCR_DIR,
    start_page: Optional[int] = None,
    end_page: Optional[int] = None,
) -> tuple[int, int]:
    """
    Run Surya OCR on all rendered PNGs that haven't been processed yet.

    Loads models once, processes pages in order, writes per-page JSON, returns (done, failed).
    """
    def _pnum(p: Path) -> int:
        return int(p.stem.split("_")[1])

    pngs = sorted(render_dir.glob("page_*.png"))
    if not pngs:
        print(f"Error: no rendered pages found in {render_dir}. Run Stage 1 first.", file=sys.stderr)
        return 0, 0

    if start_page or end_page:
        lo, hi = start_page or 1, end_page or _pnum(pngs[-1])
        pngs = [p for p in pngs if lo <= _pnum(p) <= hi]

    ocr_dir.mkdir(parents=True, exist_ok=True)
    init_db()

    # Pre-check before loading models
    to_process: list[Path] = []
    skipped = pre_failed = 0
    for png in pngs:
        pn = _pnum(png)
        if not should_process("ocr", pn):
            status, _ = get_status("ocr", pn)
            if status == "done":
                skipped += 1
            else:
                print(f"  [ocr] page {pn}: exhausted retries, skipping.")
                pre_failed += 1
        else:
            to_process.append(png)

    if not to_process:
        print(f"OCR: all pages already processed ({skipped} done, {pre_failed} exhausted).")
        return 0, pre_failed

    print("Loading Surya OCR (GPU)...")
    det_predictor, rec_predictor, foundation = _load_surya()
    print("Surya OCR loaded.")

    done = failed = 0
    for png in to_process:
        pn = _pnum(png)
        try:
            image = Image.open(png).convert("RGB")
            w, h = image.size

            blocks = _surya_ocr_page(image, det_predictor, rec_predictor)
            ordered = reorder_columns(blocks, page_width=float(w))

            record = {
                "page_num":     pn,
                "image_path":   str(png),
                "image_width":  w,
                "image_height": h,
                "blocks":       [b.to_dict() for b in ordered],
            }
            out_path = ocr_dir / f"page_{pn:04d}.json"
            out_path.write_text(json.dumps(record, indent=2, ensure_ascii=False))

            set_status("ocr", pn, "done", str(out_path))
            done += 1
            print(f"  [ocr] page {pn} → {out_path.name}  ({len(ordered)} blocks)")

        except Exception as exc:
            set_status("ocr", pn, "failed")
            failed += 1
            print(f"  [ocr] page {pn}: FAILED — {exc}")

    # Unload Surya from VRAM before next stage
    del rec_predictor, det_predictor, foundation
    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
    print("  Surya OCR unloaded from GPU.")

    print(f"\nOCR complete: {done} done, {skipped} skipped, {failed + pre_failed} failed.")
    return done, failed + pre_failed
