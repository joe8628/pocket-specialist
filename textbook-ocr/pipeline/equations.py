"""Stage 3: Surya layout detection + UniMERNet LaTeX OCR on equation regions.

Two phases within one stage — single VRAM budget, sequential model loads:

  Phase 1 (Surya Layout):   identifies Equation / Text / Heading / Figure / Table regions
                            across all pages in a single model load, then unloads.
                            Reading order within a page falls back to y-coordinate sort
                            (surya.ordering was removed in surya-ocr 0.17).
  Phase 2 (UniMERNet):      crops each equation region, converts to LaTeX using
                            UniMERNet (wanderkid/unimernet_base), then unloads.

Input:  checkpoints/rendered/page_{N:04d}.png   (from Stage 1)
        checkpoints/ocr/page_{N:04d}.json       (from Stage 2)

Output: checkpoints/equations/page_{N:04d}.json (OCR JSON enriched with block_type + latex)
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

from config import CROPS_DIR, EQUATION_CONF_THRESHOLD, EQUATIONS_DIR, FOOTER_STRIP_RATIO, HEADER_STRIP_RATIO, OCR_DIR, RENDER_DIR
from pipeline.checkpoint import get_status, init_db, set_status, should_process
from pipeline.models import BlockType, TextBlock


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
        print(f"Error: failed to import surya-ocr layout modules: {exc}", file=sys.stderr)
        sys.exit(1)
    foundation = FoundationPredictor(checkpoint=settings.LAYOUT_MODEL_CHECKPOINT)
    return LayoutPredictor(foundation), foundation


def _load_unimernet(device: torch.device) -> tuple:
    # Backfill three helpers that moved from modeling_utils → pytorch_utils in transformers >4.42
    import transformers.modeling_utils as _mu
    if not hasattr(_mu, "apply_chunking_to_forward"):
        from transformers.pytorch_utils import (
            apply_chunking_to_forward,
            find_pruneable_heads_and_indices,
            prune_linear_layer,
        )
        _mu.apply_chunking_to_forward = apply_chunking_to_forward
        _mu.find_pruneable_heads_and_indices = find_pruneable_heads_and_indices
        _mu.prune_linear_layer = prune_linear_layer

    try:
        from huggingface_hub import snapshot_download
        from omegaconf import OmegaConf
        import unimernet.models  # trigger registry population
        from unimernet.models.unimernet.unimernet import UniMERModel
        from unimernet.processors.formula_processor import FormulaImageEvalProcessor
    except ImportError:
        print("Error: unimernet is not installed. See textbook-ocr/requirements.txt.", file=sys.stderr)
        sys.exit(1)

    model_dir = snapshot_download("wanderkid/unimernet_base")
    model_cfg = OmegaConf.create({
        "arch": "unimernet",
        "model_type": "unimernet",
        "load_finetuned": False,
        "load_pretrained": True,
        "pretrained": str(Path(model_dir) / "pytorch_model.pth"),
        "tokenizer_name": "nougat",
        "tokenizer_config": {"path": model_dir},
        "model_name": model_dir,
        "model_config": {"max_seq_len": 384, "model_name": model_dir},
    })
    # unimernet 0.2.3's CustomMBartDecoder doesn't declare _supports_sdpa, but
    # transformers >=4.43 defaults to SDPA and raises if the model doesn't support it.
    # Force eager attention on the decoder config before the model is built.
    from unimernet.models.unimernet import encoder_decoder as _ed
    _orig_mbart_init = _ed.CustomMBartDecoder.__init__
    def _mbart_eager_init(self, config):
        config._attn_implementation = "eager"
        _orig_mbart_init(self, config)
    _ed.CustomMBartDecoder.__init__ = _mbart_eager_init
    try:
        model = UniMERModel.from_config(model_cfg).to(device)
    finally:
        _ed.CustomMBartDecoder.__init__ = _orig_mbart_init
    model.eval()

    # CustomMBartDecoder.forward uses past_key_values[0][0].shape[2] (old tuple-of-tuples API).
    # Transformers >=4.46 passes EncoderDecoderCache where that indexing returns None.
    # Disabling _supports_cache_class forces the legacy format during generate().
    _ed.CustomVisionEncoderDecoderModel._supports_cache_class = False

    vis_cfg = OmegaConf.create({"name": "formula_image_eval", "image_size": [192, 672]})
    vis_processor = FormulaImageEvalProcessor.from_config(vis_cfg)
    return model, vis_processor



# ── Helpers ───────────────────────────────────────────────────────────────────

def _reorder_by_reading_order(blocks: list[TextBlock], order_bboxes: list) -> list[TextBlock]:
    """Sort OCR blocks by reading order from OrderPredictor results.

    Each order_bbox has .position (int, reading order rank) and .bbox ([x0,y0,x1,y1]).
    Blocks whose centroid falls inside a known region are ranked by that region's position;
    unassigned blocks are appended after, sorted by y-coordinate.
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

    # ── Phase 1: Surya layout detection across all pages ─────────────────────────
    print("Loading Surya Layout Predictor (GPU)...")
    layout_predictor, layout_foundation = _load_layout()
    print("Surya Layout Predictor loaded.")

    layout_by_page: dict[int, list] = {}
    for jp in to_process:
        pn = _pnum(jp)
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
    print("  Surya Layout Predictor unloaded from GPU.")

    # ── Phase 2: UniMERNet LaTeX OCR on equation crops ───────────────────────
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print("Loading UniMERNet (wanderkid/unimernet_base, GPU)...")
    uni_model, vis_processor = _load_unimernet(device)
    print("UniMERNet loaded.")

    # Collect all crops across all pages first, then run batched inference.
    page_data: list[tuple[int, dict, list[TextBlock], list[int], list[Image.Image], list[tuple[int, str]]]] = []
    all_crops: list[Image.Image] = []

    for jp in to_process:
        pn = _pnum(jp)
        png = render_dir / f"page_{pn:04d}.png"
        raw = json.loads(jp.read_text())
        blocks = [TextBlock.from_dict(b) for b in raw["blocks"]]
        blocks = _reorder_by_reading_order(blocks, [])
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
                    eq_crops.append(crop)
                    eq_indices.append(idx)

        page_data.append((pn, raw, blocks, eq_indices, eq_crops, pending_tags))
        all_crops.extend(eq_crops)

    # Batched inference — UniMERNet processes fixed-size [1, 192, 672] tensors
    _BATCH = 32
    latex_list: list[str] = []
    if all_crops:
        for i in range(0, len(all_crops), _BATCH):
            batch = all_crops[i : i + _BATCH]
            images = torch.stack([vis_processor(c) for c in batch]).to(device)
            with torch.no_grad():
                output = uni_model.generate({"image": images})
            latex_list.extend(output["pred_str"])

    del uni_model, vis_processor
    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
    print("  UniMERNet unloaded from GPU.")

    # ── Phase 2b: TATR table structure recognition ────────────────────────────
    from pipeline.table_structure import extract_table_markdown, load_tatr, release_tatr

    _table_pages = {pn for pn, _, blocks, _, _, _ in page_data
                    if any(b.block_type == BlockType.TABLE for b in blocks)}

    tatr_model = tatr_proc = None
    if _table_pages:
        print("Loading TATR for table structure recognition (GPU)...")
        tatr_model, tatr_proc = load_tatr()
        print("TATR loaded.")

    # Write output JSONs
    done = failed = 0
    crop_offset = 0
    prev_table_headers: list[str] = []
    for pn, raw, blocks, eq_indices, eq_crops, pending_tags in page_data:
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

            # Table structure: run TATR on each TABLE block
            if tatr_model and pn in _table_pages:
                png = render_dir / f"page_{pn:04d}.png"
                if png.exists():
                    page_image = Image.open(png).convert("RGB")
                    for b in blocks:
                        if b.block_type != BlockType.TABLE:
                            continue
                        crop = _crop_equation(page_image, b, pad=0.02)
                        md, prev_table_headers = extract_table_markdown(
                            tatr_model, tatr_proc, crop, b.bbox, blocks,
                            prev_headers=prev_table_headers or None,
                        )
                        if md:
                            b.raw_text = md
            elif pn not in _table_pages:
                prev_table_headers = []

            out = dict(raw, blocks=[b.to_dict() for b in blocks])
            out_path = equations_dir / f"page_{pn:04d}.json"
            out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False))

            set_status("equations", pn, "done", str(out_path))
            done += 1
            tbl_count = sum(1 for b in blocks if b.block_type == BlockType.TABLE)
            print(f"  [equations] page {pn} → {out_path.name}  ({len(eq_crops)} equations, {tbl_count} tables)")

        except Exception as exc:
            set_status("equations", pn, "failed")
            failed += 1
            print(f"  [equations] page {pn}: FAILED — {exc}")

    if tatr_model:
        release_tatr(tatr_model)
        print("  TATR unloaded from GPU.")

    total_eq = sum(len(eq_crops) for _, _, _, _, eq_crops, _ in page_data)
    print(f"\nEquations complete: {done} done, {skipped} skipped, {failed + pre_failed} failed. ({total_eq} equation crops total)")
    return done, failed + pre_failed
