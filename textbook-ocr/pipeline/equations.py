"""Stage 3: Surya layout detection + Surya LaTeX OCR on equation regions.

Two phases within one stage — single VRAM budget, sequential model loads:

  Phase 1 (Surya Layout):   identifies Equation / Text / Heading / Figure / Table regions
                            across all pages in a single model load, then unloads.
  Phase 2 (Surya LaTeX OCR): crops each equation region, converts to LaTeX using
                            RecognitionPredictor with TaskNames.block_without_boxes,
                            then unloads. No separate texify model needed.

Input:  checkpoints/rendered/page_{N:04d}.png   (from Stage 1)
        checkpoints/ocr/page_{N:04d}.json       (from Stage 2)

Output: checkpoints/equations/page_{N:04d}.json (OCR JSON enriched with block_type + latex)
        checkpoints/crops/page_{N:04d}_eq_{M:02d}.png
"""
from __future__ import annotations
import gc
import json
import sys
from pathlib import Path
from typing import Optional

import torch
from PIL import Image

from config import CROPS_DIR, EQUATION_CONF_THRESHOLD, EQUATIONS_DIR, OCR_DIR, RENDER_DIR
from pipeline.checkpoint import get_status, init_db, set_status, should_process
from pipeline.models import BlockType, TextBlock


_LAYOUT_TO_BLOCKTYPE: dict[str, BlockType] = {
    "Text":          BlockType.TEXT,
    "SectionHeader": BlockType.HEADING,
    "Equation":      BlockType.EQUATION,
    "Figure":        BlockType.FIGURE,
    "Caption":       BlockType.CAPTION,
    "Table":         BlockType.TABLE,
    "ListItem":      BlockType.LIST_ITEM,
}


# ── Model loaders ─────────────────────────────────────────────────────────────

def _load_layout():
    try:
        from surya.foundation import FoundationPredictor
        from surya.layout import LayoutPredictor
        from surya.settings import settings
    except ImportError:
        print("Error: surya-ocr is not installed. Run: pip install surya-ocr", file=sys.stderr)
        sys.exit(1)
    foundation = FoundationPredictor(checkpoint=settings.LAYOUT_MODEL_CHECKPOINT)
    return LayoutPredictor(foundation), foundation


def _load_latex_ocr():
    try:
        from surya.foundation import FoundationPredictor
        from surya.recognition import RecognitionPredictor
        from surya.settings import settings
    except ImportError:
        print("Error: surya-ocr is not installed. Run: pip install surya-ocr", file=sys.stderr)
        sys.exit(1)
    foundation = FoundationPredictor(checkpoint=settings.RECOGNITION_MODEL_CHECKPOINT)
    return RecognitionPredictor(foundation), foundation


# ── Helpers ───────────────────────────────────────────────────────────────────

def _assign_block_types(blocks: list[TextBlock], layout_boxes: list) -> list[TextBlock]:
    """
    Tag each OCR block with the layout region whose bounding box contains its centroid.
    Prefer the smallest (most specific) enclosing region on overlap.
    """
    for block in blocks:
        cx = (block.bbox.x0 + block.bbox.x1) / 2
        cy = (block.bbox.y0 + block.bbox.y1) / 2
        best_label, best_area = None, float("inf")
        for lbox in layout_boxes:
            x0, y0, x1, y1 = lbox.bbox
            if x0 <= cx <= x1 and y0 <= cy <= y1:
                area = (x1 - x0) * (y1 - y0)
                if area < best_area:
                    best_area = area
                    best_label = lbox.label
        if best_label:
            block.block_type = _LAYOUT_TO_BLOCKTYPE.get(best_label, BlockType.UNKNOWN)
    return blocks


def _crop_equation(image: Image.Image, block: TextBlock, pad: float = 0.05) -> Image.Image:
    """Crop an equation region from the page image with proportional padding."""
    x0, y0, x1, y1 = block.bbox.x0, block.bbox.y0, block.bbox.x1, block.bbox.y1
    pw = max(1, int((x1 - x0) * pad))
    ph = max(1, int((y1 - y0) * pad))
    iw, ih = image.size
    return image.crop((
        max(0,  int(x0) - pw),
        max(0,  int(y0) - ph),
        min(iw, int(x1) + pw),
        min(ih, int(y1) + ph),
    ))


# ── Public API ────────────────────────────────────────────────────────────────

def process_equations(
    render_dir: Path = RENDER_DIR,
    ocr_dir: Path = OCR_DIR,
    equations_dir: Path = EQUATIONS_DIR,
    crops_dir: Path = CROPS_DIR,
    start_page: Optional[int] = None,
    end_page: Optional[int] = None,
    eq_threshold: float = EQUATION_CONF_THRESHOLD,
) -> tuple[int, int]:
    """Run Surya layout detection then Texify on equation crops. Returns (done, failed)."""

    def _pnum(p: Path) -> int:
        return int(p.stem.split("_")[1])

    ocr_jsons = sorted(ocr_dir.glob("page_*.json"))
    if not ocr_jsons:
        print(f"Error: no OCR output in {ocr_dir}. Run Stage 2 first.", file=sys.stderr)
        return 0, 0

    if start_page or end_page:
        lo, hi = start_page or 1, end_page or _pnum(ocr_jsons[-1])
        ocr_jsons = [p for p in ocr_jsons if lo <= _pnum(p) <= hi]

    to_process: list[Path] = []
    skipped = pre_failed = 0
    for jp in ocr_jsons:
        pn = _pnum(jp)
        if not should_process("equations", pn):
            status, _ = get_status("equations", pn)
            if status == "done":
                skipped += 1
            else:
                print(f"  [equations] page {pn}: exhausted retries, skipping.")
                pre_failed += 1
        else:
            to_process.append(jp)

    if not to_process:
        print(f"Equations: all pages already processed ({skipped} done).")
        return 0, pre_failed

    equations_dir.mkdir(parents=True, exist_ok=True)
    crops_dir.mkdir(parents=True, exist_ok=True)
    init_db()

    # ── Phase 1: Surya layout detection across all pages ──────────────────────
    print("Loading Surya Layout Predictor (GPU)...")
    layout_predictor, layout_foundation = _load_layout()
    print("Surya Layout loaded.")

    layout_by_page: dict[int, list] = {}
    for jp in to_process:
        pn = _pnum(jp)
        png = render_dir / f"page_{pn:04d}.png"
        if not png.exists():
            layout_by_page[pn] = []
            continue
        image = Image.open(png).convert("RGB")
        results = layout_predictor([image])
        layout_by_page[pn] = results[0].bboxes
        eq_count = sum(1 for b in results[0].bboxes if b.label == "Equation")
        print(f"  [layout] page {pn}: {len(results[0].bboxes)} regions, {eq_count} equations")

    del layout_predictor, layout_foundation
    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
    print("  Surya Layout unloaded from GPU.")

    # ── Phase 2: Surya LaTeX OCR on equation crops ───────────────────────────
    print("Loading Surya LaTeX OCR (GPU)...")
    latex_predictor, latex_foundation = _load_latex_ocr()
    from surya.common.surya.schema import TaskNames
    print("Surya LaTeX OCR loaded.")

    # Collect all crops across all pages first, then run one batched inference.
    page_data: list[tuple[int, dict, list[TextBlock], list[int], list[Image.Image]]] = []
    all_crops: list[Image.Image] = []

    for jp in to_process:
        pn = _pnum(jp)
        png = render_dir / f"page_{pn:04d}.png"
        raw = json.loads(jp.read_text())
        blocks = [TextBlock.from_dict(b) for b in raw["blocks"]]
        _assign_block_types(blocks, layout_by_page.get(pn, []))

        eq_indices: list[int] = []
        eq_crops:   list[Image.Image] = []

        if png.exists():
            page_image = Image.open(png).convert("RGB")
            for idx, block in enumerate(blocks):
                if block.block_type == BlockType.EQUATION:
                    crop = _crop_equation(page_image, block)
                    crop_path = crops_dir / f"page_{pn:04d}_eq_{len(eq_crops):02d}.png"
                    crop.save(str(crop_path))
                    eq_crops.append(crop)
                    eq_indices.append(idx)

        page_data.append((pn, raw, blocks, eq_indices, eq_crops))
        all_crops.extend(eq_crops)

    # Batch inference over all crops at once
    if all_crops:
        tasks  = [TaskNames.block_without_boxes] * len(all_crops)
        bboxes = [[[0, 0, c.width, c.height]] for c in all_crops]
        results = latex_predictor(all_crops, tasks, bboxes=bboxes)
        latex_list = [
            r.text_lines[0].text.strip() if r.text_lines else ""
            for r in results
        ]
    else:
        latex_list = []

    del latex_predictor, latex_foundation
    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
    print("  Surya LaTeX OCR unloaded from GPU.")

    # Write output JSONs
    done = failed = 0
    crop_offset = 0
    for pn, raw, blocks, eq_indices, eq_crops in page_data:
        try:
            for i, idx in enumerate(eq_indices):
                latex = latex_list[crop_offset + i]
                if latex:
                    blocks[idx].latex = latex
                    blocks[idx].latex_confidence = 1.0
                else:
                    blocks[idx].block_type = BlockType.EQUATION_FAILED
            crop_offset += len(eq_crops)

            out = dict(raw, blocks=[b.to_dict() for b in blocks])
            out_path = equations_dir / f"page_{pn:04d}.json"
            out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False))

            set_status("equations", pn, "done", str(out_path))
            done += 1
            print(f"  [equations] page {pn} → {out_path.name}  ({len(eq_crops)} equations extracted)")

        except Exception as exc:
            set_status("equations", pn, "failed")
            failed += 1
            print(f"  [equations] page {pn}: FAILED — {exc}")

    total_eq = sum(len(eq_crops) for _, _, _, _, eq_crops in page_data)
    print(f"\nEquations complete: {done} done, {skipped} skipped, {failed + pre_failed} failed. ({total_eq} equation crops total)")
    return done, failed + pre_failed
