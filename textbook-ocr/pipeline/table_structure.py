"""TATR-based table structure recognition for pipeline Stage 3.

After Surya identifies a TABLE layout region, this module:
  1. Crops the table from the page image
  2. Runs Microsoft Table Transformer (TATR) to detect the row/column grid
  3. Assigns OCR text blocks to grid cells by centroid containment
  4. Returns a Markdown table string

Multi-page tables: pass column_headers from the previous page's table so that
continuation pages (no detected header row) re-use the same column labels.
"""
from __future__ import annotations

import torch
from PIL import Image

from pipeline.models import BoundingBox, TextBlock

_TATR_MODEL_ID = "microsoft/table-transformer-structure-recognition-v1.1-all"
_STRUCTURE_THRESHOLD = 0.5


def load_tatr() -> tuple:
    """Load TATR model and processor onto GPU (if available). Returns (model, processor)."""
    from transformers import AutoImageProcessor, TableTransformerForObjectDetection

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    processor = AutoImageProcessor.from_pretrained(_TATR_MODEL_ID)
    model = TableTransformerForObjectDetection.from_pretrained(_TATR_MODEL_ID)
    model = model.to(device).eval()
    return model, processor


def release_tatr(model) -> None:
    del model
    import gc
    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()


def _detect_structure(
    model, processor, image: Image.Image
) -> dict[str, list[list[float]]]:
    """Run TATR inference; return pixel bboxes [x0,y0,x1,y1] grouped by label."""
    inputs = processor(images=image, return_tensors="pt").to(model.device)
    with torch.no_grad():
        outputs = model(**inputs)

    # post_process_object_detection expects target_sizes on CPU
    target_sizes = torch.tensor([[image.height, image.width]])
    results = processor.post_process_object_detection(
        outputs, threshold=_STRUCTURE_THRESHOLD, target_sizes=target_sizes
    )[0]

    id2label = model.config.id2label
    grouped: dict[str, list[list[float]]] = {}
    for label, box in zip(results["labels"], results["boxes"]):
        name = id2label[label.item()]
        grouped.setdefault(name, []).append(box.tolist())
    return grouped


def _text_in_cell(
    blocks: list[TextBlock],
    cell_x0: float,
    cell_y0: float,
    cell_x1: float,
    cell_y1: float,
    crop_x0: float,
    crop_y0: float,
) -> str:
    """Collect OCR text from blocks whose centroid falls in a cell (page coords)."""
    px0 = cell_x0 + crop_x0
    py0 = cell_y0 + crop_y0
    px1 = cell_x1 + crop_x0
    py1 = cell_y1 + crop_y0
    texts = []
    for b in blocks:
        cx = (b.bbox.x0 + b.bbox.x1) / 2
        cy = (b.bbox.y0 + b.bbox.y1) / 2
        if px0 <= cx <= px1 and py0 <= cy <= py1:
            t = b.raw_text.strip()
            if t:
                texts.append(t)
    return " ".join(texts)


def _md_row(cells: list[str]) -> str:
    return "| " + " | ".join(c.replace("|", "\\|") for c in cells) + " |"


def extract_table_markdown(
    model,
    processor,
    table_crop: Image.Image,
    crop_bbox: BoundingBox,
    ocr_blocks: list[TextBlock],
    prev_headers: list[str] | None = None,
) -> tuple[str, list[str]]:
    """
    Detect row/column grid via TATR, assign OCR text to cells, return Markdown.

    Returns (markdown_table, column_headers). column_headers can be passed as
    prev_headers to the next page if the table continues across pages.
    Falls back to raw OCR text if TATR finds no structure.
    """
    structure = _detect_structure(model, processor, table_crop)

    rows = sorted(structure.get("table row", []), key=lambda b: b[1])
    cols = sorted(structure.get("table column", []), key=lambda b: b[0])
    header_regions = structure.get("table column header", [])

    if not rows or not cols:
        # No structure detected — fall back to whatever Surya gave us
        return "", prev_headers or []

    n_cols = len(cols)

    # Determine which rows are header rows
    header_row_indices: set[int] = set()
    if header_regions:
        hdr_y0 = min(r[1] for r in header_regions)
        hdr_y1 = max(r[3] for r in header_regions)
        for i, row in enumerate(rows):
            cy = (row[1] + row[3]) / 2
            if hdr_y0 <= cy <= hdr_y1:
                header_row_indices.add(i)

    # Build the cell grid: cell bbox = intersection of row band and column band
    grid: list[list[str]] = []
    for row in rows:
        row_cells = []
        for col in cols:
            text = _text_in_cell(
                ocr_blocks,
                max(row[0], col[0]),
                max(row[1], col[1]),
                min(row[2], col[2]),
                min(row[3], col[3]),
                crop_bbox.x0,
                crop_bbox.y0,
            )
            row_cells.append(text)
        grid.append(row_cells)

    if header_row_indices:
        hdr_rows = [grid[i] for i in sorted(header_row_indices)]
        data_rows = [grid[i] for i in range(len(rows)) if i not in header_row_indices]
        headers = [" ".join(r[j] for r in hdr_rows).strip() for j in range(n_cols)]
    elif prev_headers and len(prev_headers) == n_cols:
        # Continuation page: no header detected, reuse previous page's headers
        headers = prev_headers
        data_rows = grid
    else:
        headers = [""] * n_cols
        data_rows = grid

    lines: list[str] = []
    if any(headers):
        lines.append(_md_row(headers))
        lines.append(_md_row(["---"] * n_cols))
    for row_cells in data_rows:
        lines.append(_md_row(row_cells))

    return "\n".join(lines), headers
