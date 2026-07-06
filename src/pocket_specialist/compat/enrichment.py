"""Document enrichment utilities for layout classification and formula extraction.

This module currently couples Surya layout detection and LaTeX extraction under
a shared GPU lifecycle. It enriches rendered-page OCR JSON with semantic block
types, equation crops, and equation LaTeX while the broader architecture moves
toward the spec-defined split between layout and formula subsystems.

Input:  checkpoints/rendered/page_{N:04d}.png
        checkpoints/ocr/page_{N:04d}.json

Output: checkpoints/equations/page_{N:04d}.json
        checkpoints/crops/page_{N:04d}_eq_{M:02d}.png
"""
from __future__ import annotations
import gc
import json
import re
import sys
from pathlib import Path
from typing import Optional

import torch
from PIL import Image

from pocket_specialist.core.config import EQUATION_CONF_THRESHOLD, FOOTER_STRIP_RATIO, HEADER_STRIP_RATIO, crops_dir_for, equations_dir_for, ocr_dir_for, render_dir_for
from pocket_specialist.core.dev_checkpoints import write_development_artifact, write_development_checkpoint
from pocket_specialist.core.progress import model_status, phase_complete, phase_error, phase_start, phase_validation, progress_bar
from pocket_specialist.storage.checkpoint import get_status, init_db, set_status, should_process
from pocket_specialist.core.models import BlockType, TextBlock


_RE_EQ_NUMBER = re.compile(r'^\s*\(\d+(?:\.\d+)*\)\s*$')

_LAYOUT_TO_BLOCKTYPE: dict[str, BlockType] = {
    "Text":          BlockType.TEXT,
    "SectionHeader": BlockType.HEADING,
    "Equation":      BlockType.EQUATION,
    "Figure":        BlockType.FIGURE,
    "Caption":       BlockType.CAPTION,
    "Table":         BlockType.TABLE,
    "ListItem":      BlockType.LIST_ITEM,
    "Footnote":      BlockType.FOOTNOTE,
}


# ── Model loaders ─────────────────────────────────────────────────────────────

def _load_layout():
    try:
        from surya.foundation import FoundationPredictor
        from surya.layout import LayoutPredictor
        from surya.settings import settings
    except ImportError as exc:
        raise RuntimeError("surya-ocr is not installed. Run: pip install surya-ocr") from exc
    foundation = FoundationPredictor(checkpoint=settings.LAYOUT_MODEL_CHECKPOINT)
    return LayoutPredictor(foundation), foundation


def _load_latex_ocr():
    try:
        from surya.foundation import FoundationPredictor
        from surya.recognition import RecognitionPredictor
        from surya.settings import settings
    except ImportError as exc:
        raise RuntimeError("surya-ocr is not installed. Run: pip install surya-ocr") from exc
    foundation = FoundationPredictor(checkpoint=settings.RECOGNITION_MODEL_CHECKPOINT)
    return RecognitionPredictor(foundation), foundation



# ── Helpers ───────────────────────────────────────────────────────────────────

def _reorder_by_reading_order(blocks: list[TextBlock], order_bboxes: list) -> list[TextBlock]:
    """Sort OCR blocks by reading order using ordered layout regions.

    Surya 0.17 emits layout boxes with a `position` field but no standalone
    `surya.ordering` predictor. Blocks whose centroid falls inside a known
    layout region inherit that region order; unassigned blocks are appended
    after, sorted by y-coordinate.
    """
    if not order_bboxes:
        return sorted(blocks, key=lambda b: b.bbox.y0)
    regions = sorted(order_bboxes, key=lambda ob: ob.position)

    def _rank(block: TextBlock) -> tuple[int, float]:
        cx = (block.bbox.x0 + block.bbox.x1) / 2
        cy = (block.bbox.y0 + block.bbox.y1) / 2
        for rank, ob in enumerate(regions):
            x0, y0, x1, y1 = ob.bbox
            if x0 <= cx <= x1 and y0 <= cy <= y1:
                return (rank, cy)
        return (len(regions), cy)

    return sorted(blocks, key=_rank)


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


def _strip_header_footer(blocks: list[TextBlock], page_height: float) -> list[TextBlock]:
    """Drop running-header/page-number (top HEADER_STRIP_RATIO) and footer blocks."""
    top = page_height * HEADER_STRIP_RATIO
    bot = page_height * FOOTER_STRIP_RATIO
    return [b for b in blocks if top < (b.bbox.y0 + b.bbox.y1) / 2 < bot]


def _extract_eq_numbers(blocks: list[TextBlock]) -> tuple[list[TextBlock], list[tuple[int, str]]]:
    """Remove standalone eq-number labels from OCR pass; return filtered blocks + pending tags.

    Each pending tag is (index_in_filtered_blocks, tag_string) so _apply_eq_tags can attach
    it after LaTeX OCR populates block.latex.
    """
    result: list[TextBlock] = []
    pending: list[tuple[int, str]] = []
    for b in blocks:
        if b.block_type == BlockType.EQUATION and _RE_EQ_NUMBER.match(b.raw_text):
            tag = b.raw_text.strip().strip("()")
            for i in reversed(range(len(result))):
                if result[i].block_type == BlockType.EQUATION:
                    pending.append((i, tag))
                    break
        else:
            result.append(b)
    return result, pending


def _apply_eq_tags(blocks: list[TextBlock], pending: list[tuple[int, str]]) -> None:
    """Attach \\tag{} labels to equation blocks after LaTeX OCR has populated block.latex."""
    for idx, tag in pending:
        if idx < len(blocks) and blocks[idx].latex:
            suffix = f"\\tag{{{tag}}}"
            if not blocks[idx].latex.endswith(suffix):
                blocks[idx].latex = blocks[idx].latex.rstrip() + f"\n{suffix}"


def _consolidate_figures(blocks: list[TextBlock]) -> list[TextBlock]:
    """Collapse consecutive FIGURE blocks (axis ticks, labels) into one placeholder."""
    result: list[TextBlock] = []
    in_run = False
    for b in blocks:
        if b.block_type == BlockType.FIGURE:
            if not in_run:
                result.append(b)
                in_run = True
        else:
            in_run = False
            result.append(b)
    return result


def _crop_equation(image: Image.Image, block: TextBlock, pad: float = 0.10) -> Image.Image:
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

def enrich_document(
    document: str,
    render_dir: Path | None = None,
    ocr_dir: Path | None = None,
    equations_dir: Path | None = None,
    crops_dir: Path | None = None,
    start_page: Optional[int] = None,
    end_page: Optional[int] = None,
    eq_threshold: float = EQUATION_CONF_THRESHOLD,
) -> tuple[int, int]:
    """Run Surya layout detection then Texify on equation crops. Returns (done, failed)."""

    phase_start("equations", document)

    def _pnum(p: Path) -> int:
        return int(p.stem.split("_")[1])

    render_dir = render_dir or render_dir_for(document)
    ocr_dir = ocr_dir or ocr_dir_for(document)
    equations_dir = equations_dir or equations_dir_for(document)
    crops_dir = crops_dir or crops_dir_for(document)

    ocr_jsons = sorted(ocr_dir.glob("page_*.json"))
    if not ocr_jsons:
        print(f"Error: no OCR output in {ocr_dir}. Run OCR extraction first.", file=sys.stderr)
        return 0, 0

    if start_page or end_page:
        lo, hi = start_page or 1, end_page or _pnum(ocr_jsons[-1])
        ocr_jsons = [p for p in ocr_jsons if lo <= _pnum(p) <= hi]

    to_process: list[Path] = []
    skipped = pre_failed = 0
    for jp in ocr_jsons:
        pn = _pnum(jp)
        if not should_process("equations", document, pn):
            status, _ = get_status("equations", document, pn)
            if status == "done":
                skipped += 1
            else:
                print(f"  [equations] page {pn}: exhausted retries, skipping.")
                pre_failed += 1
        else:
            to_process.append(jp)

    if not to_process:
        phase_complete("equations", f"all pages already processed ({skipped} done, {pre_failed} failed)")
        print(f"Equations: all pages already processed ({skipped} done).")
        return 0, pre_failed

    phase_validation("equations", f"queued {len(to_process)} page(s) output={equations_dir} crops={crops_dir}")
    equations_dir.mkdir(parents=True, exist_ok=True)
    crops_dir.mkdir(parents=True, exist_ok=True)
    init_db()

    # ── Phase 1: Surya layout detection + reading order across all pages ─────────
    model_status("equations-layout", "loading Surya Layout Predictor")
    print("Loading Surya Layout Predictor (GPU)...")
    layout_predictor, layout_foundation = _load_layout()
    print("Surya Layout predictor loaded.")
    model_status("equations-layout", "loaded")

    layout_by_page: dict[int, list] = {}
    for layout_index, jp in enumerate(to_process, 1):
        pn = _pnum(jp)
        progress_bar("equations-layout", layout_index, len(to_process), f"page {pn}")
        png = render_dir / f"page_{pn:04d}.png"
        if not png.exists():
            layout_by_page[pn] = []
            continue
        image = Image.open(png).convert("RGB")
        layout_results = layout_predictor([image])
        lboxes = layout_results[0].bboxes
        layout_by_page[pn] = lboxes
        eq_count = sum(1 for b in lboxes if b.label == "Equation")
        print(f"  [layout] page {pn}: {len(lboxes)} regions, {eq_count} equations")

    del layout_predictor, layout_foundation
    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
    print("  Surya Layout predictor unloaded from GPU.")
    model_status("equations-layout", "unloaded")

    # ── Phase 2: Surya LaTeX OCR on equation crops ───────────────────────────
    model_status("equations-latex", "loading Surya LaTeX OCR")
    print("Loading Surya LaTeX OCR (GPU)...")
    latex_predictor, latex_foundation = _load_latex_ocr()
    from surya.common.surya.schema import TaskNames
    print("Surya LaTeX OCR loaded.")
    model_status("equations-latex", "loaded")

    # Collect all crops across all pages first, then run one batched inference.
    page_data: list[tuple[int, dict, list[TextBlock], list[int], list[Image.Image], list[tuple[int, str]]]] = []
    all_crops: list[Image.Image] = []

    for crop_index, jp in enumerate(to_process, 1):
        pn = _pnum(jp)
        progress_bar("equations-crops", crop_index, len(to_process), f"page {pn}")
        png = render_dir / f"page_{pn:04d}.png"
        raw = json.loads(jp.read_text())
        blocks = [TextBlock.from_dict(b) for b in raw["blocks"]]
        blocks = _reorder_by_reading_order(blocks, layout_by_page.get(pn, []))
        _assign_block_types(blocks, layout_by_page.get(pn, []))
        blocks = _strip_header_footer(blocks, raw.get("image_height", 0))
        blocks, pending_tags = _extract_eq_numbers(blocks)
        blocks = _consolidate_figures(blocks)

        eq_indices: list[int] = []
        eq_crops:   list[Image.Image] = []

        if png.exists():
            page_image = Image.open(png).convert("RGB")
            for idx, block in enumerate(blocks):
                if block.block_type == BlockType.EQUATION:
                    crop = _crop_equation(page_image, block)
                    crop_path = crops_dir / f"page_{pn:04d}_eq_{len(eq_crops):02d}.png"
                    crop.save(str(crop_path))
                    write_development_artifact(document, "equations", crop_path.name, crop_path)
                    eq_crops.append(crop)
                    eq_indices.append(idx)

        page_data.append((pn, raw, blocks, eq_indices, eq_crops, pending_tags))
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
    model_status("equations-latex", "unloaded")

    # Write output JSONs
    done = failed = 0
    crop_offset = 0
    for output_index, (pn, raw, blocks, eq_indices, eq_crops, pending_tags) in enumerate(page_data, 1):
        progress_bar("equations-output", output_index, len(page_data), f"page {pn}")
        try:
            for i, idx in enumerate(eq_indices):
                latex = latex_list[crop_offset + i]
                if latex:
                    blocks[idx].latex = latex
                    blocks[idx].latex_confidence = 1.0
                else:
                    blocks[idx].block_type = BlockType.EQUATION_FAILED
            crop_offset += len(eq_crops)
            _apply_eq_tags(blocks, pending_tags)

            out = dict(raw, blocks=[b.to_dict() for b in blocks])
            out_path = equations_dir / f"page_{pn:04d}.json"
            out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False))

            set_status("equations", document, pn, "done", str(out_path))
            write_development_checkpoint(
                document,
                "equations",
                page=pn,
                summary={"output": str(out_path), "equation_crops": len(eq_crops)},
                artifacts={out_path.name: out_path},
            )
            done += 1
            print(f"  [equations] page {pn} → {out_path.name}  ({len(eq_crops)} equations extracted)")

        except Exception as exc:
            set_status("equations", document, pn, "failed")
            failed += 1
            print(f"  [equations] page {pn}: FAILED — {exc}")

    total_eq = sum(len(eq_crops) for _, _, _, _, eq_crops, _ in page_data)
    print(f"\nEquations complete: {done} done, {skipped} skipped, {failed + pre_failed} failed. ({total_eq} equation crops total)")
    return done, failed + pre_failed
