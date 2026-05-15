"""Layout utilities: column-order detection and block reordering.

Used by Stage 2 (OCR) to linearize multi-column pages into correct reading order
before writing per-page JSON.
"""
from __future__ import annotations
from pipeline.models import TextBlock, BoundingBox


def _x_center(block: TextBlock) -> float:
    return (block.bbox.x0 + block.bbox.x1) / 2.0


def _find_column_divider(blocks: list[TextBlock], page_width: float) -> float | None:
    """
    Return the x-coordinate of the column boundary if a two-column layout is
    detected, or None for single-column pages.

    Detection: find the largest gap between consecutive x-centers of blocks
    that falls within the middle 40% of page width (20%–60%). A gap must be
    at least 10% of page width to count as a column split.

    Validation: reject the candidate divider if more than N//4 blocks span
    across it (x0 < divider < x1) — a real column boundary has blocks sitting
    fully within their column, not crossing it.
    """
    if len(blocks) < 4:
        return None

    x_centers = sorted(_x_center(b) for b in blocks)
    mid_lo = page_width * 0.20
    mid_hi = page_width * 0.80
    min_gap = page_width * 0.10

    best_gap = 0.0
    best_divider: float | None = None

    for i in range(1, len(x_centers)):
        gap = x_centers[i] - x_centers[i - 1]
        mid = (x_centers[i] + x_centers[i - 1]) / 2.0
        if mid_lo <= mid <= mid_hi and gap >= min_gap and gap > best_gap:
            best_gap = gap
            best_divider = mid

    if best_divider is None:
        return None

    crossing = sum(1 for b in blocks if b.bbox.x0 < best_divider < b.bbox.x1)
    if crossing > len(blocks) // 4:
        return None

    return best_divider


def reorder_columns(blocks: list[TextBlock], page_width: float | None = None) -> list[TextBlock]:
    """
    Return *blocks* sorted in correct reading order across columns.

    For single-column pages: sort by y0 (top-to-bottom).
    For two-column pages: sort each column by y0, then concatenate left + right.
    Three or more columns are not handled in V1; they fall back to single-column
    y0 sort.
    """
    if not blocks:
        return blocks

    width = page_width or max(b.bbox.x1 for b in blocks)
    divider = _find_column_divider(blocks, width)

    if divider is None:
        return sorted(blocks, key=lambda b: b.bbox.y0)

    left  = sorted([b for b in blocks if _x_center(b) <= divider], key=lambda b: b.bbox.y0)
    right = sorted([b for b in blocks if _x_center(b) >  divider], key=lambda b: b.bbox.y0)
    return left + right


def bbox_from_paddle(paddle_box: list) -> BoundingBox:
    """
    Convert a PaddleOCR bounding box (list of four [x, y] corner points in
    clockwise order starting from top-left) to a BoundingBox.

        [[x0,y0], [x1,y0], [x1,y1], [x0,y1]]

    PaddleOCR can return slightly rotated quads for skewed text; we take the
    axis-aligned bounding rectangle.
    """
    xs = [pt[0] for pt in paddle_box]
    ys = [pt[1] for pt in paddle_box]
    return BoundingBox(x0=min(xs), y0=min(ys), x1=max(xs), y1=max(ys))
