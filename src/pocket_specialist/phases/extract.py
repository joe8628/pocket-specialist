"""Phase B structured extraction orchestration."""

from __future__ import annotations

import json
import re
import threading
from dataclasses import asdict
from pathlib import Path

from PIL import Image, ImageDraw

from pocket_specialist.storage.checkpoint import DAGNodeState, init_db, set_status, should_process

from pocket_specialist.core.cif import CanonicalIntermediateFormat, ProcessingArtifact, ProvenanceRecord, SourceCoords, StructuredBlock
from pocket_specialist.core.config import get_settings
from pocket_specialist.core.dag import DAGExecutor, TaskRunResult
from pocket_specialist.core.dev_checkpoints import write_development_artifact, write_development_checkpoint
from pocket_specialist.core.gpu import gpu_scheduler
from pocket_specialist.core.progress import model_status, page_stage_complete, page_stage_start, phase_complete, phase_error, phase_start, phase_validation, progress_bar, region_stage_complete, region_stage_start, start_timer
from pocket_specialist.core.tasks import ExtractionTask, PageUnit
from pocket_specialist.handlers.intake import (
    PDFContentType,
    SourceKind,
    classify_document,
    get_pdf_native_blocks,
    ingest_docx_to_cif,
    ingest_epub_to_cif,
    ingest_html_to_cif,
    ingest_odt_to_cif,
    ingest_tabular_to_cif,
    ingest_text_to_cif,
    ingest_xlsx_to_cif,
    render_pdf_page_to_bytes,
)
from pocket_specialist.layout.providers import LayoutRegion, LayoutResult, build_layout_provider, crop_region_image
from pocket_specialist.formula.providers import FormulaExtractor, FormulaResult, build_formula_extractor
from pocket_specialist.formula.symbolic import inline_formula_candidates, looks_symbolic, symbolic_latex
from pocket_specialist.ocr.providers import OCRProvider, build_fallback_ocr_provider, build_primary_ocr_provider


_REGION_TO_BLOCK_TYPE = {
    "text": "TextBlock",
    "heading": "TextBlock",
    "table": "TableBlock",
    "formula": "FormulaBlock",
    "code": "CodeBlock",
    "figure": "FigureBlock",
    "key_value": "KeyValueBlock",
    "list": "ListBlock",
    "footer": "TextBlock",
    "header": "TextBlock",
}

_LAYOUT_REGION_TYPES = set(_REGION_TO_BLOCK_TYPE)
_OCR_REGION_TYPES = {"text", "heading", "table", "code", "key_value", "list", "footer", "header", "figure"}
_DIGITAL_NATIVE_RESIDUAL_OCR_TYPES = {"table", "figure", "formula", "code", "key_value"}


def _should_use_page_level_ocr(settings, regions: list[LayoutRegion]) -> bool:
    threshold = int(getattr(settings.ocr, "page_fallback_region_threshold", 0) or 0)
    if threshold <= 0:
        return False
    ocr_region_count = len([region for region in regions if region.region_type in _OCR_REGION_TYPES])
    return ocr_region_count >= threshold


def _filter_native_pdf_residual_regions(settings, regions: list[LayoutRegion]) -> list[LayoutRegion]:
    if not getattr(settings.ocr, "skip_residual_text_regions_for_native_pdf", True):
        return regions
    return [region for region in regions if region.region_type in _DIGITAL_NATIVE_RESIDUAL_OCR_TYPES]


def _block_type_counts(blocks: list[StructuredBlock]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for block in blocks:
        counts[block.block_type] = counts.get(block.block_type, 0) + 1
    return counts


def _layout_region_counts(layout: LayoutResult) -> dict[str, int]:
    counts: dict[str, int] = {}
    for region in layout.regions:
        counts[region.region_type] = counts.get(region.region_type, 0) + 1
    return counts


def _layout_checkpoint_artifacts(layout_path: Path, image_bytes: bytes, layout_result: LayoutResult, provider_name: str) -> dict[str, object]:
    provider_slug = provider_name.strip().lower().replace(" ", "-").replace("_", "-")
    artifacts: dict[str, object] = {f"{layout_path.stem}_{provider_slug}.json": layout_path}
    for region in layout_result.regions:
        artifacts[f"{layout_path.stem}_{provider_slug}_{region.region_id}_{region.region_type}.png"] = crop_region_image(image_bytes, region.bbox)
    return artifacts


def _profile_summary(profile) -> dict[str, object]:
    return {
        "source_kind": profile.source_kind.value,
        "source_path": str(profile.source_path),
        "mime_type": profile.mime_type,
        "page_count": profile.page_count,
        "pdf_content_type": profile.pdf_content_type.value,
        "text_extractable": profile.text_extractable,
        "metadata": profile.metadata,
    }


def _document_checkpoint(document: str, stage: str, cif: CanonicalIntermediateFormat, document_path: Path) -> None:
    write_development_checkpoint(
        document,
        stage,
        summary={
            "output": str(document_path),
            "block_count": len(cif.blocks),
            "artifact_count": len(cif.artifacts),
            "block_types": _block_type_counts(cif.blocks),
        },
        artifacts={document_path.name: document_path},
    )


_TABLE_HEADER_SPLIT_RE = re.compile(r"\s{2,}")
_TABLE_SEPARATOR_CHARS = {"-", ":", "="}


def _stringify_table_value(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _normalize_table_headers(headers: list[object]) -> list[str]:
    normalized: list[str] = []
    seen: dict[str, int] = {}
    for idx, header in enumerate(headers, 1):
        candidate = _stringify_table_value(header) or f"column_{idx}"
        count = seen.get(candidate, 0) + 1
        seen[candidate] = count
        normalized.append(candidate if count == 1 else f"{candidate}_{count}")
    return normalized


def _table_row_objects(headers: list[str], rows: list[list[object]]) -> list[dict[str, str]]:
    normalized_rows: list[dict[str, str]] = []
    for row in rows:
        normalized_rows.append({header: _stringify_table_value(row[idx]) if idx < len(row) else "" for idx, header in enumerate(headers)})
    return normalized_rows


def _is_table_separator_row(cells: list[str]) -> bool:
    cleaned = [cell.replace(" ", "") for cell in cells if cell.strip()]
    return bool(cleaned) and all(set(cell) <= _TABLE_SEPARATOR_CHARS for cell in cleaned)


def _split_table_line(line: str) -> list[str]:
    stripped = line.strip()
    if not stripped:
        return []
    if "|" in stripped:
        parts = [part.strip() for part in stripped.strip("|").split("|")]
        return [part for part in parts if part or len(parts) > 1]
    if "	" in stripped:
        return [part.strip() for part in stripped.split("	")]
    for delimiter in (",", ";"):
        if delimiter in stripped:
            parts = [part.strip() for part in stripped.split(delimiter)]
            if len(parts) > 1:
                return parts
    parts = [part.strip() for part in _TABLE_HEADER_SPLIT_RE.split(stripped) if part.strip()]
    if len(parts) > 1:
        return parts
    return [stripped]


def _looks_like_header_row(cells: list[str]) -> bool:
    non_empty = [cell for cell in cells if cell]
    if len(non_empty) != len(cells) or not non_empty:
        return False
    if len({cell.lower() for cell in non_empty}) != len(non_empty):
        return False
    alpha_cells = sum(any(ch.isalpha() for ch in cell) for cell in non_empty)
    digit_cells = sum(cell.replace(".", "", 1).isdigit() for cell in non_empty)
    return alpha_cells >= max(1, len(non_empty) - digit_cells)


def _table_contract_from_matrix(matrix: list[list[object]], *, caption: str | None = None) -> dict[str, object]:
    cleaned_rows = [[_stringify_table_value(cell) for cell in row] for row in matrix if any(_stringify_table_value(cell) for cell in row)]
    if not cleaned_rows:
        return {"type": "TableBlock", "headers": [], "rows": [], "caption": caption}

    cleaned_rows = [row for row in cleaned_rows if not _is_table_separator_row(row)]
    if not cleaned_rows:
        return {"type": "TableBlock", "headers": [], "rows": [], "caption": caption}

    if len(cleaned_rows) == 1 and len(cleaned_rows[0]) == 1:
        headers = ["value"]
        rows = [{"value": cleaned_rows[0][0]}]
        return {"type": "TableBlock", "headers": headers, "rows": rows, "caption": caption}

    if len(cleaned_rows) > 1 and _looks_like_header_row(cleaned_rows[0]):
        headers = _normalize_table_headers(cleaned_rows[0])
        body = cleaned_rows[1:]
    else:
        width = max(len(row) for row in cleaned_rows)
        headers = [f"column_{idx}" for idx in range(1, width + 1)]
        body = cleaned_rows
    return {"type": "TableBlock", "headers": headers, "rows": _table_row_objects(headers, body), "caption": caption}


def _table_contract_from_text(text: str, *, caption: str | None = None) -> dict[str, object]:
    matrix = [_split_table_line(line) for line in text.splitlines() if line.strip()]
    matrix = [row for row in matrix if row]
    return _table_contract_from_matrix(matrix, caption=caption)


def _table_contract_from_ocr_payload(payload: dict[str, object], fallback_text: str) -> dict[str, object]:
    caption = payload.get("caption") if isinstance(payload.get("caption"), str) else None
    headers_obj = payload.get("headers")
    rows_obj = payload.get("rows")
    if isinstance(headers_obj, list) and isinstance(rows_obj, list):
        headers = _normalize_table_headers(list(headers_obj))
        if rows_obj and all(isinstance(row, dict) for row in rows_obj):
            if not headers:
                first_row = rows_obj[0]
                headers = _normalize_table_headers(list(first_row.keys()))
            rows = [
                {header: _stringify_table_value(row.get(header, "")) for header in headers}
                for row in rows_obj
                if isinstance(row, dict)
            ]
            return {"type": "TableBlock", "headers": headers, "rows": rows, "caption": caption}
        if all(isinstance(row, list) for row in rows_obj):
            return _table_contract_from_matrix(([headers] if headers else []) + list(rows_obj), caption=caption)
    return _table_contract_from_text(fallback_text, caption=caption)


def _layout_region_to_block_type(region_type: str) -> str:
    return _REGION_TO_BLOCK_TYPE.get(region_type, "TextBlock")


def _bbox_area(bbox: tuple[int, int, int, int]) -> int:
    return max(0, bbox[2] - bbox[0]) * max(0, bbox[3] - bbox[1])


def _bbox_intersection_area(left: tuple[int, int, int, int], right: tuple[int, int, int, int]) -> int:
    x0 = max(left[0], right[0])
    y0 = max(left[1], right[1])
    x1 = min(left[2], right[2])
    y1 = min(left[3], right[3])
    if x1 <= x0 or y1 <= y0:
        return 0
    return (x1 - x0) * (y1 - y0)


def _polygon_bbox_overlap_area(polygon: list[tuple[int, int]], bbox: tuple[int, int, int, int]) -> int:
    if len(polygon) < 3:
        return 0
    polygon_x = [point[0] for point in polygon]
    polygon_y = [point[1] for point in polygon]
    left = min(min(polygon_x), bbox[0])
    top = min(min(polygon_y), bbox[1])
    right = max(max(polygon_x), bbox[2])
    bottom = max(max(polygon_y), bbox[3])
    width = max(1, right - left)
    height = max(1, bottom - top)
    mask = Image.new("L", (width, height), 0)
    draw = ImageDraw.Draw(mask)
    draw.polygon([(x - left, y - top) for x, y in polygon], fill=1)
    crop_box = (
        max(0, bbox[0] - left),
        max(0, bbox[1] - top),
        min(width, bbox[2] - left),
        min(height, bbox[3] - top),
    )
    if crop_box[2] <= crop_box[0] or crop_box[3] <= crop_box[1]:
        return 0
    return mask.crop(crop_box).histogram()[1]


def _region_overlap_score(bbox: tuple[int, int, int, int], region: LayoutRegion) -> tuple[float, float, int]:
    bbox_area = _bbox_area(bbox)
    if bbox_area <= 0:
        return 0.0, 0.0, 0
    overlap_area = _polygon_bbox_overlap_area(region.polygon, bbox) if region.polygon else _bbox_intersection_area(bbox, region.bbox)
    if overlap_area <= 0:
        return 0.0, 0.0, 0
    region_area = _bbox_area(region.bbox)
    union_area = max(1, bbox_area + region_area - overlap_area)
    coverage = overlap_area / bbox_area
    iou = overlap_area / union_area
    return coverage, iou, overlap_area


def _match_region(bbox: tuple[int, int, int, int], layout: LayoutResult) -> LayoutRegion | None:
    best: LayoutRegion | None = None
    best_score = (0.0, 0.0, 0.0, 0, 0)
    for region in layout.regions:
        coverage, iou, overlap_area = _region_overlap_score(bbox, region)
        if overlap_area <= 0:
            continue
        score = (coverage, iou, region.confidence, -_bbox_area(region.bbox), overlap_area)
        if score > best_score:
            best = region
            best_score = score
    return best


def _matched_layout_region_ids(native_blocks, layout: LayoutResult) -> set[str]:
    matched: set[str] = set()
    for native in native_blocks:
        region = _match_region(native.bbox, layout)
        if region is None or region.region_type == "formula":
            continue
        matched.add(region.region_id)
    return matched


def _artifact_uri_for_path(path: Path, project_root: Path | None) -> str:
    if project_root is not None:
        try:
            return path.relative_to(project_root).as_posix()
        except ValueError:
            pass
    return str(path)


def _write_figure_artifact(
    *,
    document: str,
    page_num: int,
    block_id: str,
    artifact_bytes: bytes | None,
    artifact_root: Path | None,
    project_root: Path | None,
) -> str | None:
    if artifact_bytes is None:
        return None
    settings = get_settings()
    artifact_base = artifact_root or settings.paths.artifact_path
    root_path = project_root or settings.paths.project_root
    artifact_dir = artifact_base / document / "figures"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = artifact_dir / f"{block_id}.png"
    artifact_path.write_bytes(artifact_bytes)
    return _artifact_uri_for_path(artifact_path, root_path)


def _figure_contract(
    *,
    caption: str | None,
    alt_text: str,
    embedded_text: list[str] | None,
    artifact_uri: str | None,
) -> dict[str, object]:
    return {
        "type": "FigureBlock",
        "caption": caption,
        "alt_text": alt_text,
        "embedded_text": embedded_text,
        "artifact_uri": artifact_uri,
    }


def _artifacts_from_blocks(blocks: list[StructuredBlock]) -> list[ProcessingArtifact]:
    artifacts: list[ProcessingArtifact] = []
    seen_uris: set[str] = set()
    for block in blocks:
        if block.block_type != "FigureBlock":
            continue
        artifact_uri = block.content.get("artifact_uri")
        if not isinstance(artifact_uri, str) or not artifact_uri or artifact_uri in seen_uris:
            continue
        seen_uris.add(artifact_uri)
        metadata: dict[str, object] = {"block_id": block.block_id}
        if block.page is not None:
            metadata["page"] = block.page
        if block.source_coords is not None and block.source_coords.bbox is not None:
            metadata["bbox"] = list(block.source_coords.bbox)
        artifacts.append(
            ProcessingArtifact(
                artifact_id=f"{block.block_id}-artifact",
                artifact_type="figure",
                uri=artifact_uri,
                metadata=metadata,
            )
        )
    return artifacts


def _parse_key_value_pairs(text: str) -> dict[str, str]:
    pairs: dict[str, str] = {}
    for line in [part.strip() for part in text.splitlines() if part.strip()]:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        pairs[key.strip()] = value.strip()
    return pairs


def _native_contract(region_type: str, text: str, *, artifact_uri: str | None = None) -> dict[str, object]:
    if region_type == "heading":
        return {"type": "TextBlock", "text": text, "heading_level": 1, "language": None}
    if region_type == "list":
        return {"type": "ListBlock", "items": [text]}
    if region_type == "code":
        return {"type": "CodeBlock", "language": None, "code": text}
    if region_type == "table":
        return _table_contract_from_text(text)
    if region_type == "key_value":
        return {"type": "KeyValueBlock", "pairs": _parse_key_value_pairs(text)}
    if region_type == "figure":
        return _figure_contract(caption=None, alt_text="", embedded_text=[text] if text else None, artifact_uri=artifact_uri)
    return {"type": "TextBlock", "text": text, "heading_level": None, "language": None}


def _ocr_contract(region_type: str, texts: list[str], payload: dict[str, object] | None = None, *, artifact_uri: str | None = None) -> dict[str, object]:
    merged_text = "\n".join(texts)
    if region_type == "heading":
        return {"type": "TextBlock", "text": merged_text, "heading_level": 1, "language": None}
    if region_type == "list":
        return {"type": "ListBlock", "items": texts}
    if region_type == "code":
        return {"type": "CodeBlock", "language": None, "code": merged_text}
    if region_type == "table":
        return _table_contract_from_ocr_payload(payload or {}, merged_text)
    if region_type == "key_value":
        return {"type": "KeyValueBlock", "pairs": _parse_key_value_pairs(merged_text)}
    if region_type == "figure":
        caption = payload.get("caption") if isinstance(payload, dict) and isinstance(payload.get("caption"), str) else None
        return _figure_contract(caption=caption, alt_text=merged_text, embedded_text=texts or None, artifact_uri=artifact_uri)
    return {"type": "TextBlock", "text": merged_text, "heading_level": None, "language": None}


def _formula_contract(result: FormulaResult) -> dict[str, object]:
    return {
        "type": "FormulaBlock",
        "latex": result.latex,
        "mathml": result.mathml,
        "inline": result.is_inline,
        "provider": result.provider,
    }


def _symbolic_formula_contract(raw_formula_text: str, *, inline: bool) -> dict[str, object]:
    return {
        "type": "FormulaBlock",
        "latex": symbolic_latex(raw_formula_text),
        "mathml": None,
        "inline": inline,
        "provider": "ocr-fallback",
        "raw_formula_text": raw_formula_text,
    }


def _formula_fallback_contract(raw_formula_text: str = "", *, inline: bool = False) -> dict[str, object]:
    if looks_symbolic(raw_formula_text):
        return _symbolic_formula_contract(raw_formula_text, inline=inline)
    return {"type": "FormulaFallback", "raw_formula_text": raw_formula_text, "provider": "ocr-fallback", "inline": inline}


def _inline_formula_blocks(
    *,
    document: str,
    page_num: int,
    parent_block_id: str,
    text: str,
    reading_order: int,
    bbox: tuple[int, int, int, int] | None,
    provider: str,
) -> list[StructuredBlock]:
    blocks: list[StructuredBlock] = []
    for idx, candidate in enumerate(inline_formula_candidates(text), 1):
        blocks.append(
            StructuredBlock(
                block_id=f"{parent_block_id}-inline-formula-{idx:04d}",
                doc_id=document,
                block_type="FormulaBlock",
                content=_symbolic_formula_contract(candidate.latex, inline=True),
                section_path=[],
                reading_order=reading_order + idx,
                page=page_num,
                source_coords=SourceCoords(page=page_num, bbox=bbox),
                provenance=ProvenanceRecord(
                    source_stage="formula_route",
                    provider="ocr-fallback",
                    lineage=[parent_block_id],
                    metadata={
                        "route": "inline_heuristic",
                        "source_provider": provider,
                        "raw_formula_text": candidate.raw_text,
                        "char_start": candidate.start,
                        "char_end": candidate.end,
                    },
                ),
            )
        )
    return blocks


def _guard_native_region_type(region: LayoutRegion | None, text: str) -> tuple[str, str | None]:
    if region is None:
        return "text", None
    provider_label = str(region.metadata.get("provider_label") or "").strip().lower()
    if provider_label in {"figure_title", "caption"}:
        return "text", "caption_text"
    if region.region_type == "formula":
        return "text", "formula_crop_only"
    return region.region_type, None


def _native_page_blocks(
    document: str,
    page_num: int,
    native_blocks,
    layout: LayoutResult,
    *,
    page_image_bytes: bytes | None = None,
    artifact_root: Path | None = None,
    project_root: Path | None = None,
) -> list[StructuredBlock]:
    blocks: list[StructuredBlock] = []
    for idx, native in enumerate(native_blocks, 1):
        region = _match_region(native.bbox, layout)
        if region is not None and region.region_type == "formula":
            continue
        region_type, route_guard = _guard_native_region_type(region, native.text)
        block_type = _layout_region_to_block_type(region_type)
        block_id = f"{document}-page-{page_num:04d}-native-{idx:04d}"
        artifact_uri = None
        if region_type == "figure":
            artifact_bytes = crop_region_image(page_image_bytes, native.bbox) if page_image_bytes is not None else None
            artifact_uri = _write_figure_artifact(
                document=document,
                page_num=page_num,
                block_id=block_id,
                artifact_bytes=artifact_bytes,
                artifact_root=artifact_root,
                project_root=project_root,
            )
        metadata = {
            "region_type": region_type,
            "layout_region_id": region.region_id if region else None,
            "matched_layout_region_type": region.region_type if region else None,
            "layout_provider_label": region.metadata.get("provider_label") if region else None,
        }
        if route_guard is not None:
            metadata["route_guard"] = route_guard
        if artifact_uri is not None:
            metadata["artifact_uri"] = artifact_uri
        block = StructuredBlock(
            block_id=block_id,
            doc_id=document,
            block_type=block_type,
            content=_native_contract(region_type, native.text, artifact_uri=artifact_uri),
            section_path=[],
            reading_order=region.reading_order if region else idx,
            page=page_num,
            source_coords=SourceCoords(page=page_num, bbox=native.bbox),
            provenance=ProvenanceRecord(
                source_stage="structured_extract",
                provider="fitz-native-text",
                metadata=metadata,
            ),
        )
        blocks.append(block)
        if region_type != "formula":
            blocks.extend(
                _inline_formula_blocks(
                    document=document,
                    page_num=page_num,
                    parent_block_id=block.block_id,
                    text=native.text,
                    reading_order=block.reading_order,
                    bbox=native.bbox,
                    provider="fitz-native-text",
                )
            )
    return sorted(blocks, key=lambda block: block.reading_order)


def _extract_with_fallback(primary: OCRProvider, fallback: OCRProvider | None, crop_bytes: bytes, region_type: str):
    try:
        return primary.extract(crop_bytes, region_type)
    except Exception:
        if fallback is None:
            raise
        return fallback.extract(crop_bytes, region_type)


def _formula_page_blocks(
    document: str,
    page_num: int,
    image_bytes: bytes,
    formula_regions: list[LayoutRegion],
    formula_extractor: FormulaExtractor | None = None,
) -> list[StructuredBlock]:
    blocks: list[StructuredBlock] = []
    settings = get_settings()
    ordered_regions = sorted(formula_regions, key=lambda item: item.reading_order)
    formula_total = len(ordered_regions)
    for block_index, region in enumerate(ordered_regions, 1):
        region_started_at = start_timer()
        region_stage_start(page_num, "formula", block_index, formula_total, region.region_id, region.region_type)
        crop_bytes = crop_region_image(image_bytes, region.bbox)
        write_development_artifact(document, "formula", f"page_{page_num:04d}_{region.region_id}.png", crop_bytes)
        formula_result: FormulaResult | None = None
        formula_error: str | None = None
        formula_deferred = getattr(settings.formula, "defer", False)
        if not settings.formula.enabled:
            formula_error = "formula extraction disabled"
        elif formula_deferred:
            formula_error = "formula extraction deferred"
        elif formula_extractor is not None:
            try:
                formula_result = formula_extractor.extract(crop_bytes)
            except Exception as exc:
                formula_error = str(exc)
                raise

        content = _formula_contract(formula_result) if formula_result is not None else _formula_fallback_contract("", inline=False)
        blocks.append(
            StructuredBlock(
                block_id=f"{document}-page-{page_num:04d}-formula-{block_index:04d}",
                doc_id=document,
                block_type="FormulaBlock",
                content=content,
                section_path=[],
                reading_order=region.reading_order,
                page=page_num,
                source_coords=SourceCoords(page=page_num, bbox=region.bbox),
                provenance=ProvenanceRecord(
                    source_stage="formula_extract" if formula_result is not None else "structured_extract",
                    provider=formula_result.provider if formula_result is not None else "layout-routing",
                    confidence=formula_result.confidence if formula_result is not None else region.confidence,
                    metadata={
                        "layout_region_id": region.region_id,
                        "region_type": region.region_type,
                        "formula_latency_ms": formula_result.latency_ms if formula_result is not None else None,
                        "fallback_reason": formula_error,
                    },
                ),
            )
        )
        region_stage_complete(page_num, "formula", block_index, formula_total, region.region_id, region.region_type, region_started_at)
    return blocks


def _ocr_page_blocks(
    document: str,
    page_num: int,
    image_bytes: bytes,
    layout: LayoutResult,
    primary_provider: OCRProvider,
    fallback_provider: OCRProvider | None,
    regions: list[LayoutRegion] | None = None,
    *,
    artifact_root: Path | None = None,
    project_root: Path | None = None,
) -> list[StructuredBlock]:
    blocks: list[StructuredBlock] = []
    block_index = 0
    ordered_regions = sorted(regions or layout.regions, key=lambda item: item.reading_order)
    ocr_total = len([region for region in ordered_regions if region.region_type in _OCR_REGION_TYPES])
    ocr_current = 0
    for region in ordered_regions:
        if region.region_type not in _OCR_REGION_TYPES:
            continue

        block_type = _layout_region_to_block_type(region.region_type)
        ocr_current += 1
        region_started_at = start_timer()
        region_stage_start(page_num, "ocr", ocr_current, ocr_total, region.region_id, region.region_type)
        crop_bytes = crop_region_image(image_bytes, region.bbox)
        write_development_artifact(document, "ocr", f"page_{page_num:04d}_{region.region_id}.png", crop_bytes)
        result = _extract_with_fallback(primary_provider, fallback_provider, crop_bytes, region.region_type)
        texts = [str(block["raw_text"]).strip() for block in result.typed_content["blocks"] if str(block["raw_text"]).strip()]
        if not texts and region.region_type != "figure":
            region_stage_complete(page_num, "ocr", ocr_current, ocr_total, region.region_id, region.region_type, region_started_at)
            continue
        block_index += 1
        block_id = f"{document}-page-{page_num:04d}-ocr-{block_index:04d}"
        artifact_uri = None
        if region.region_type == "figure":
            artifact_uri = _write_figure_artifact(
                document=document,
                page_num=page_num,
                block_id=block_id,
                artifact_bytes=crop_bytes,
                artifact_root=artifact_root,
                project_root=project_root,
            )
        metadata = {
            "region_type": region.region_type,
            "layout_region_id": region.region_id,
            "ocr_mode": result.extraction_metadata.get("mode"),
        }
        if artifact_uri is not None:
            metadata["artifact_uri"] = artifact_uri
        ocr_block = StructuredBlock(
            block_id=block_id,
            doc_id=document,
            block_type=block_type,
            content=_ocr_contract(region.region_type, texts, result.typed_content, artifact_uri=artifact_uri),
            section_path=[],
            reading_order=region.reading_order,
            page=page_num,
            source_coords=SourceCoords(page=page_num, bbox=region.bbox),
            provenance=ProvenanceRecord(
                source_stage="structured_extract",
                provider=result.provider,
                confidence=result.confidence,
                metadata=metadata,
            ),
        )
        blocks.append(ocr_block)
        if region.region_type in {"text", "heading", "list"}:
            blocks.extend(
                _inline_formula_blocks(
                    document=document,
                    page_num=page_num,
                    parent_block_id=ocr_block.block_id,
                    text="\n".join(texts),
                    reading_order=ocr_block.reading_order,
                    bbox=region.bbox,
                    provider=result.provider,
                )
            )
        region_stage_complete(page_num, "ocr", ocr_current, ocr_total, region.region_id, region.region_type, region_started_at)
    return blocks

def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def _write_layout_page(layout_result: LayoutResult, path: Path) -> None:
    _write_json(path, asdict(layout_result))


def _write_structured_page(document: str, page_num: int, blocks: list[StructuredBlock], profile_metadata: dict[str, object], path: Path) -> None:
    _write_json(
        path,
        {
            "doc_id": document,
            "page": page_num,
            "blocks": [asdict(block) for block in blocks],
            "metadata": profile_metadata,
        },
    )


def _write_page_layout_page(document: str, page_num: int, blocks: list[StructuredBlock], profile_metadata: dict[str, object], path: Path) -> None:
    _write_json(
        path,
        {
            "doc_id": document,
            "page": page_num,
            "type": "PageLayout",
            "blocks": [asdict(block) for block in blocks],
            "reading_order": [block.block_id for block in sorted(blocks, key=lambda item: (item.reading_order, item.block_id))],
            "metadata": profile_metadata,
        },
    )


def _layout_result_from_payload(payload: dict[str, object]) -> LayoutResult:
    regions_payload = payload.get("regions")
    regions: list[LayoutRegion] = []
    if isinstance(regions_payload, list):
        for idx, item in enumerate(regions_payload, 1):
            if not isinstance(item, dict):
                continue
            bbox_obj = item.get("bbox")
            if not isinstance(bbox_obj, list | tuple) or len(bbox_obj) != 4:
                continue
            polygon_obj = item.get("polygon")
            polygon = None
            if isinstance(polygon_obj, list):
                normalized_points: list[tuple[int, int]] = []
                for point in polygon_obj:
                    if isinstance(point, (list, tuple)) and len(point) == 2:
                        normalized_points.append((int(float(point[0])), int(float(point[1]))))
                polygon = normalized_points if len(normalized_points) >= 3 else None
            regions.append(
                LayoutRegion(
                    region_id=str(item.get("region_id") or f"region-{idx:04d}"),
                    region_type=str(item.get("region_type") or "text"),
                    bbox=tuple(int(float(value)) for value in bbox_obj),
                    confidence=float(item.get("confidence") or 0.0),
                    reading_order=int(item.get("reading_order") or idx - 1),
                    polygon=polygon,
                    metadata=item.get("metadata") if isinstance(item.get("metadata"), dict) else {},
                )
            )
    confidence = payload.get("layout_confidence")
    return LayoutResult(
        page_id=str(payload.get("page_id") or "page"),
        regions=regions,
        layout_confidence=float(confidence) if confidence is not None else None,
    )


def _source_coords_from_payload(payload: object) -> SourceCoords | None:
    if not isinstance(payload, dict):
        return None
    bbox_obj = payload.get("bbox")
    bbox = None
    if isinstance(bbox_obj, list | tuple) and len(bbox_obj) == 4:
        bbox = tuple(int(float(value)) for value in bbox_obj)
    return SourceCoords(
        page=int(payload["page"]) if isinstance(payload.get("page"), int) else None,
        bbox=bbox,
        polygons=payload.get("polygons") if isinstance(payload.get("polygons"), list) else None,
    )


def _provenance_from_payload(payload: object) -> ProvenanceRecord:
    if not isinstance(payload, dict):
        return ProvenanceRecord(source_stage="structured_extract")
    lineage = payload.get("lineage")
    metadata = payload.get("metadata")
    return ProvenanceRecord(
        source_stage=str(payload.get("source_stage") or "structured_extract"),
        provider=str(payload["provider"]) if payload.get("provider") is not None else None,
        confidence=float(payload["confidence"]) if payload.get("confidence") is not None else None,
        lineage=[str(item) for item in lineage] if isinstance(lineage, list) else [],
        metadata=metadata if isinstance(metadata, dict) else {},
    )


def _structured_block_from_payload(payload: dict[str, object]) -> StructuredBlock:
    section_path = payload.get("section_path")
    content = payload.get("content")
    return StructuredBlock(
        block_id=str(payload["block_id"]),
        doc_id=str(payload["doc_id"]),
        block_type=str(payload["block_type"]),
        content=content if isinstance(content, dict) else {},
        section_path=[str(item) for item in section_path] if isinstance(section_path, list) else [],
        reading_order=int(payload.get("reading_order") or 0),
        page=int(payload["page"]) if isinstance(payload.get("page"), int) else None,
        source_coords=_source_coords_from_payload(payload.get("source_coords")),
        provenance=_provenance_from_payload(payload.get("provenance")),
    )


def _load_structured_page_blocks(path: Path) -> list[StructuredBlock]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("structured page checkpoint is not an object")
    raw_blocks = payload.get("blocks")
    if not isinstance(raw_blocks, list):
        raise ValueError("structured page checkpoint is missing blocks")
    return [_structured_block_from_payload(block) for block in raw_blocks if isinstance(block, dict)]


def _add_blocks_to_cif(cif: CanonicalIntermediateFormat, blocks: list[StructuredBlock]) -> None:
    existing = {block.block_id for block in cif.blocks}
    for block in blocks:
        if block.block_id not in existing:
            cif.add_block(block)
            existing.add(block.block_id)
    existing_artifacts = {artifact.uri for artifact in cif.artifacts}
    for artifact in _artifacts_from_blocks(blocks):
        if artifact.uri not in existing_artifacts:
            cif.add_artifact(artifact)
            existing_artifacts.add(artifact.uri)


def _checkpoint_path_matches(checkpoint: DAGNodeState, expected_path: Path) -> bool:
    return checkpoint.path == str(expected_path) and expected_path.exists()


def _empty_layout_result(page_num: int) -> LayoutResult:
    return LayoutResult(page_id=f"page-{page_num:04d}", regions=[], layout_confidence=None)


def _write_layout_disabled_page(
    *,
    cif: CanonicalIntermediateFormat,
    document: str,
    page_num: int,
    page_blocks: list[StructuredBlock],
    structured_dir: Path,
    page_mode: str,
    page_layout: bool = False,
) -> None:
    structured_path = structured_dir / f"page_{page_num:04d}.json"
    if page_layout:
        _write_page_layout_page(document, page_num, page_blocks, {"page_mode": page_mode, "layout_enabled": False}, structured_path)
    else:
        _write_structured_page(document, page_num, page_blocks, {"page_mode": page_mode, "layout_enabled": False}, structured_path)
    for block in page_blocks:
        cif.add_block(block)
    for artifact in _artifacts_from_blocks(page_blocks):
        cif.add_artifact(artifact)
    set_status("structured", document, page_num, "done", str(structured_path))
    write_development_checkpoint(
        document,
        "structured",
        page=page_num,
        summary={
            "output": str(structured_path),
            "page_mode": page_mode,
            "block_count": len(page_blocks),
            "block_types": _block_type_counts(page_blocks),
        },
        artifacts={structured_path.name: structured_path},
    )


def _raw_block_bbox(raw_block: dict[str, object]) -> tuple[int, int, int, int] | None:
    bbox = raw_block.get("bbox") if isinstance(raw_block.get("bbox"), dict) else {}
    if not isinstance(bbox, dict):
        return None
    try:
        return (
            int(float(bbox.get("x0", 0))),
            int(float(bbox.get("y0", 0))),
            int(float(bbox.get("x1", 0))),
            int(float(bbox.get("y1", 0))),
        )
    except (TypeError, ValueError):
        return None


def _page_region_type(raw_block: dict[str, object]) -> str:
    candidate = str(raw_block.get("block_type", "text")).strip().lower()
    if candidate in _LAYOUT_REGION_TYPES:
        return candidate
    return "text"


def _ocr_result_to_page_blocks(document: str, page_num: int, result: OCRResult) -> list[StructuredBlock]:
    blocks: list[StructuredBlock] = []
    for idx, raw_block in enumerate(result.typed_content["blocks"], 1):
        text = str(raw_block.get("raw_text", "")).strip()
        region_type = _page_region_type(raw_block)
        if not text and region_type != "figure":
            continue
        content = _formula_fallback_contract(text, inline=False) if region_type == "formula" else _ocr_contract(region_type, [text] if text else [], {"blocks": [raw_block]})
        blocks.append(
            StructuredBlock(
                block_id=f"{document}-page-{page_num:04d}-ocr-{idx:04d}",
                doc_id=document,
                block_type=_layout_region_to_block_type(region_type),
                content=content,
                section_path=[],
                reading_order=idx,
                page=page_num,
                source_coords=SourceCoords(page=page_num, bbox=_raw_block_bbox(raw_block)),
                provenance=ProvenanceRecord(
                    source_stage="structured_extract",
                    provider=result.provider,
                    confidence=result.confidence,
                    metadata={
                        "ocr_mode": result.extraction_metadata.get("mode"),
                        "region_type": region_type,
                        "page_structured": True,
                    },
                ),
            )
        )
    return blocks


class _LockedOCRProvider:
    def __init__(self, provider: OCRProvider | None, gate) -> None:
        self._provider = provider
        self._gate = gate

    def extract(self, image_bytes: bytes, region_type: str):
        if self._provider is None:
            raise RuntimeError("OCR provider is not available")
        with self._gate:
            return self._provider.extract(image_bytes, region_type)


class _LazyFallbackOCRProvider:
    def __init__(self, get_provider) -> None:
        self._get_provider = get_provider

    def extract(self, image_bytes: bytes, region_type: str):
        provider = self._get_provider()
        if provider is None:
            raise RuntimeError("Fallback OCR provider is not available")
        return provider.extract(image_bytes, region_type)


class _LockedFormulaExtractor:
    def __init__(self, extractor: FormulaExtractor, lock: threading.Lock) -> None:
        self._extractor = extractor
        self._lock = lock

    def extract(self, image_bytes: bytes) -> FormulaResult:
        with self._lock:
            return self._extractor.extract(image_bytes)



def _page_task(document: str, page_num: int, task_name: str, *, page_mode: str, dependencies: list[str] | None = None, resource: str = "cpu") -> tuple[ExtractionTask, PageUnit]:
    task_id = f"{document}:{task_name}:{page_num:04d}"
    return (
        ExtractionTask(
            task_id=task_id,
            task_type=task_name,
            dependencies=list(dependencies or []),
            input_refs=[f"page:{page_num:04d}"],
            resource_requirements={"resource": resource},
        ),
        PageUnit(
            unit_id=f"{document}:page:{page_num:04d}",
            doc_id=document,
            metadata={"page": page_num, "page_mode": page_mode},
        ),
    )



def _run_layout_disabled_pdf_graph(
    *,
    source_path: Path,
    profile,
    document: str,
    settings,
    structured_dir: Path,
    cif: CanonicalIntermediateFormat,
) -> tuple[int, int]:
    page_modes = profile.metadata.get("page_modes", [])
    tasks: list[ExtractionTask] = []
    units: dict[str, PageUnit] = {}
    for page_num in range(1, profile.page_count + 1):
        page_mode = page_modes[page_num - 1] if page_num - 1 < len(page_modes) else profile.pdf_content_type.value
        task, unit = _page_task(document, page_num, "structured", page_mode=page_mode)
        unit.metadata["checkpoint_path"] = str(structured_dir / f"page_{page_num:04d}.json")
        tasks.append(task)
        units[task.task_id] = unit

    if not tasks:
        phase_validation("structured", "no pages require processing")
        return 0, 0

    phase_validation("structured", f"queued {len(tasks)} page task(s) with layout disabled")
    cif_lock = threading.Lock()
    ocr_setup_lock = threading.Lock()
    ocr_call_gate = threading.BoundedSemaphore(max(1, settings.ocr.max_parallel_requests))
    primary_provider: OCRProvider | None = None
    fallback_provider: OCRProvider | None = None
    ocr_loaded = False
    fallback_loaded = False

    def ensure_fallback_provider() -> OCRProvider | None:
        nonlocal fallback_provider, fallback_loaded
        with ocr_setup_lock:
            if fallback_provider is None:
                fallback_provider = build_fallback_ocr_provider()
            if fallback_provider is None:
                return None
            if primary_provider is not None and getattr(fallback_provider, "name", None) == getattr(primary_provider, "name", None):
                return _LockedOCRProvider(primary_provider, ocr_call_gate)
            if not fallback_loaded:
                model_status("ocr", f"loading fallback provider {getattr(fallback_provider, 'name', fallback_provider.__class__.__name__)}")
                with gpu_scheduler.claim("ocr"):
                    fallback_provider.load()
                fallback_loaded = True
                model_status("ocr", "fallback provider loaded")
        return _LockedOCRProvider(fallback_provider, ocr_call_gate)

    def ensure_ocr_providers() -> tuple[OCRProvider, OCRProvider | None]:
        nonlocal primary_provider, ocr_loaded
        with ocr_setup_lock:
            if primary_provider is None:
                primary_provider = build_primary_ocr_provider()
            if not ocr_loaded:
                model_status("ocr", f"loading primary provider {getattr(primary_provider, 'name', primary_provider.__class__.__name__)}")
                with gpu_scheduler.claim("ocr"):
                    primary_provider.load()
                ocr_loaded = True
                model_status("ocr", "primary provider loaded")
        return _LockedOCRProvider(primary_provider, ocr_call_gate), _LazyFallbackOCRProvider(ensure_fallback_provider)

    def resume_task(task: ExtractionTask, unit: PageUnit, checkpoint: DAGNodeState) -> bool:
        del task
        expected_path = Path(str(unit.metadata["checkpoint_path"]))
        if not _checkpoint_path_matches(checkpoint, expected_path):
            return False
        blocks = _load_structured_page_blocks(expected_path)
        with cif_lock:
            _add_blocks_to_cif(cif, blocks)
        return True

    def run_task(task: ExtractionTask, unit: PageUnit) -> TaskRunResult:
        del task
        page_num = int(unit.metadata["page"])
        page_mode = str(unit.metadata["page_mode"])
        page_started_at = start_timer()
        page_stage_start(page_num, "structured", f"layout=disabled mode={page_mode}")
        progress_bar("structured-render", page_num, profile.page_count, f"page {page_num} render")
        zoom = settings.rendering.zoom if page_mode == PDFContentType.DIGITAL.value else settings.rendering.scanned_pdf_zoom
        image_bytes = render_pdf_page_to_bytes(source_path, page_num, zoom=zoom)
        write_development_checkpoint(
            document,
            "render",
            page=page_num,
            summary={"page_mode": page_mode, "zoom": zoom, "bytes": len(image_bytes)},
            artifacts={f"page_{page_num:04d}.png": image_bytes},
        )
        native_blocks = get_pdf_native_blocks(source_path, page_num, zoom=zoom)
        progress_bar("structured", page_num, profile.page_count, f"page {page_num} structured")
        structured_path = structured_dir / f"page_{page_num:04d}.json"
        if native_blocks:
            page_blocks = _native_page_blocks(
                document,
                page_num,
                native_blocks,
                _empty_layout_result(page_num),
                page_image_bytes=image_bytes,
                artifact_root=settings.paths.artifact_path,
                project_root=settings.paths.project_root,
            )
            with cif_lock:
                _write_layout_disabled_page(
                    cif=cif,
                    document=document,
                    page_num=page_num,
                    page_blocks=page_blocks,
                    structured_dir=structured_dir,
                    page_mode=PDFContentType.DIGITAL.value,
                )
        else:
            primary, fallback = ensure_ocr_providers()
            ocr_started_at = start_timer()
            page_stage_start(page_num, "OCR", "full page")
            progress_bar("structured-ocr", page_num, profile.page_count, f"page {page_num} OCR")
            result = _extract_with_fallback(primary, fallback, image_bytes, "page")
            page_stage_complete(page_num, "OCR", ocr_started_at, f"blocks={len(result.typed_content.get('blocks', []))}")
            write_development_checkpoint(
                document,
                "ocr",
                page=page_num,
                summary={
                    "region_type": "page",
                    "provider": result.provider,
                    "block_count": len(result.typed_content.get("blocks", [])),
                    "latency_ms": getattr(result, "latency_ms", None),
                },
            )
            page_blocks = _ocr_result_to_page_blocks(document, page_num, result)
            with cif_lock:
                _write_layout_disabled_page(
                    cif=cif,
                    document=document,
                    page_num=page_num,
                    page_blocks=page_blocks,
                    structured_dir=structured_dir,
                    page_mode=page_mode,
                    page_layout=True,
                )
        progress_bar("structured", page_num, profile.page_count, f"page {page_num} structured")
        page_stage_complete(page_num, "structured", page_started_at, f"blocks={len(page_blocks)}")
        return TaskRunResult(path=str(structured_path), metadata={"page_mode": page_mode, "layout_enabled": False})

    executor = DAGExecutor()
    result = executor.run(document=document, tasks=tasks, units=units, runner=run_task, resume=resume_task)

    if ocr_loaded and primary_provider is not None:
        model_status("ocr", "offloading primary provider")
        with gpu_scheduler.claim("ocr"):
            primary_provider.offload()
        if fallback_loaded and fallback_provider is not None and fallback_provider is not primary_provider:
            model_status("ocr", "offloading fallback provider")
            with gpu_scheduler.claim("ocr"):
                fallback_provider.offload()

    structured_done = [task_id for task_id in result.completed_task_ids + result.skipped_task_ids if ":structured:" in task_id]
    structured_failed = [task_id for task_id in result.failed_task_ids + result.blocked_task_ids if ":structured:" in task_id]
    return len(structured_done), len(structured_failed)



def _run_layout_enabled_pdf_graph(
    *,
    source_path: Path,
    profile,
    document: str,
    settings,
    structured_dir: Path,
    layout_dir: Path,
    cif: CanonicalIntermediateFormat,
    layout_provider_name: str | None = None,
) -> tuple[int, int]:
    page_modes = profile.metadata.get("page_modes", [])
    tasks: list[ExtractionTask] = []
    units: dict[str, PageUnit] = {}
    for page_num in range(1, profile.page_count + 1):
        page_mode = page_modes[page_num - 1] if page_num - 1 < len(page_modes) else profile.pdf_content_type.value
        layout_task, layout_unit = _page_task(document, page_num, "layout", page_mode=page_mode, resource="layout")
        structured_task, structured_unit = _page_task(document, page_num, "structured", page_mode=page_mode, dependencies=[layout_task.task_id])
        layout_unit.metadata["checkpoint_path"] = str(layout_dir / f"page_{page_num:04d}.json")
        structured_unit.metadata["checkpoint_path"] = str(structured_dir / f"page_{page_num:04d}.json")
        tasks.extend([layout_task, structured_task])
        units[layout_task.task_id] = layout_unit
        units[structured_task.task_id] = structured_unit

    if not tasks:
        phase_validation("structured", "no pages require processing")
        return 0, 0

    phase_validation("structured", f"queued {len(tasks)} DAG task(s) for {profile.page_count} page(s)")
    state_lock = threading.Lock()
    cif_lock = threading.Lock()
    page_state: dict[int, dict[str, object]] = {}

    layout_setup_lock = threading.Lock()
    layout_call_lock = threading.Lock()
    layout_provider = None
    layout_loaded = False

    ocr_setup_lock = threading.Lock()
    ocr_call_gate = threading.BoundedSemaphore(max(1, settings.ocr.max_parallel_requests))
    primary_provider: OCRProvider | None = None
    fallback_provider: OCRProvider | None = None
    ocr_loaded = False
    fallback_loaded = False

    formula_setup_lock = threading.Lock()
    formula_call_lock = threading.Lock()
    formula_extractor: FormulaExtractor | None = None
    formula_loaded = False
    formula_disabled = False

    def ensure_layout_provider():
        nonlocal layout_provider, layout_loaded
        with layout_setup_lock:
            if layout_provider is None:
                layout_provider = build_layout_provider(layout_provider_name)
            if not layout_loaded:
                model_status("layout", f"loading provider {getattr(layout_provider, 'name', layout_provider.__class__.__name__)}")
                with gpu_scheduler.claim("layout"):
                    layout_provider.load()
                layout_loaded = True
                model_status("layout", "provider loaded")
        return layout_provider

    def ensure_fallback_provider() -> OCRProvider | None:
        nonlocal fallback_provider, fallback_loaded
        with ocr_setup_lock:
            if fallback_provider is None:
                fallback_provider = build_fallback_ocr_provider()
            if fallback_provider is None:
                return None
            if primary_provider is not None and getattr(fallback_provider, "name", None) == getattr(primary_provider, "name", None):
                return _LockedOCRProvider(primary_provider, ocr_call_gate)
            if not fallback_loaded:
                model_status("ocr", f"loading fallback provider {getattr(fallback_provider, 'name', fallback_provider.__class__.__name__)}")
                with gpu_scheduler.claim("ocr"):
                    fallback_provider.load()
                fallback_loaded = True
                model_status("ocr", "fallback provider loaded")
        return _LockedOCRProvider(fallback_provider, ocr_call_gate)

    def ensure_ocr_providers() -> tuple[OCRProvider, OCRProvider | None]:
        nonlocal primary_provider, ocr_loaded
        with ocr_setup_lock:
            if primary_provider is None:
                primary_provider = build_primary_ocr_provider()
            if not ocr_loaded:
                model_status("ocr", f"loading primary provider {getattr(primary_provider, 'name', primary_provider.__class__.__name__)}")
                with gpu_scheduler.claim("ocr"):
                    primary_provider.load()
                ocr_loaded = True
                model_status("ocr", "primary provider loaded")
        return _LockedOCRProvider(primary_provider, ocr_call_gate), _LazyFallbackOCRProvider(ensure_fallback_provider)

    def ensure_formula_extractor() -> FormulaExtractor | None:
        nonlocal formula_extractor, formula_loaded, formula_disabled
        if not settings.formula.enabled or getattr(settings.formula, "defer", False) or formula_disabled:
            return None
        with formula_setup_lock:
            if formula_disabled:
                return None
            if formula_extractor is None:
                formula_extractor = build_formula_extractor()
            if not formula_loaded and formula_extractor is not None:
                try:
                    model_status("formula", f"loading extractor {formula_extractor.__class__.__name__}")
                    formula_extractor.load()
                except Exception:
                    if not settings.formula.fallback_to_ocr:
                        raise
                    formula_disabled = True
                    formula_extractor = None
                    return None
                formula_loaded = True
                model_status("formula", "extractor loaded")
        if formula_extractor is None:
            return None
        return _LockedFormulaExtractor(formula_extractor, formula_call_lock)

    def resume_task(task: ExtractionTask, unit: PageUnit, checkpoint: DAGNodeState) -> bool:
        page_num = int(unit.metadata["page"])
        page_mode = str(unit.metadata["page_mode"])
        expected_path = Path(str(unit.metadata["checkpoint_path"]))
        if not _checkpoint_path_matches(checkpoint, expected_path):
            return False
        if task.task_type == "layout":
            payload = json.loads(expected_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                return False
            layout_result = _layout_result_from_payload(payload)
            zoom = settings.rendering.zoom if page_mode == PDFContentType.DIGITAL.value else settings.rendering.scanned_pdf_zoom
            image_bytes = render_pdf_page_to_bytes(source_path, page_num, zoom=zoom)
            native_blocks = get_pdf_native_blocks(source_path, page_num, zoom=zoom)
            with state_lock:
                page_state[page_num] = {
                    "image_bytes": image_bytes,
                    "layout": layout_result,
                    "native_blocks": native_blocks,
                    "page_mode": page_mode,
                }
            return True
        if task.task_type == "structured":
            blocks = _load_structured_page_blocks(expected_path)
            with cif_lock:
                _add_blocks_to_cif(cif, blocks)
            return True
        return False

    def run_task(task: ExtractionTask, unit: PageUnit) -> TaskRunResult:
        page_num = int(unit.metadata["page"])
        page_mode = str(unit.metadata["page_mode"])
        if task.task_type == "layout":
            page_started_at = start_timer()
            page_stage_start(page_num, "layout", f"mode={page_mode}")
            progress_bar("layout", page_num, profile.page_count, f"page {page_num} layout")
            zoom = settings.rendering.zoom if page_mode == PDFContentType.DIGITAL.value else settings.rendering.scanned_pdf_zoom
            image_bytes = render_pdf_page_to_bytes(source_path, page_num, zoom=zoom)
            write_development_checkpoint(
                document,
                "render",
                page=page_num,
                summary={"page_mode": page_mode, "zoom": zoom, "bytes": len(image_bytes)},
                artifacts={f"page_{page_num:04d}.png": image_bytes},
            )
            provider = ensure_layout_provider()
            with layout_call_lock:
                layout_result = provider.detect(image_bytes)
            layout_result.page_id = f"page-{page_num:04d}"
            layout_path = layout_dir / f"page_{page_num:04d}.json"
            _write_layout_page(layout_result, layout_path)
            write_development_checkpoint(
                document,
                "layout",
                page=page_num,
                summary={
                    "output": str(layout_path),
                    "provider": getattr(provider, "name", provider.__class__.__name__),
                    "region_count": len(layout_result.regions),
                    "region_types": _layout_region_counts(layout_result),
                    "layout_confidence": layout_result.layout_confidence,
                },
                artifacts=_layout_checkpoint_artifacts(layout_path, image_bytes, layout_result, getattr(provider, "name", provider.__class__.__name__)),
            )
            set_status("layout", document, page_num, "done", str(layout_path))
            native_blocks = get_pdf_native_blocks(source_path, page_num, zoom=zoom)
            with state_lock:
                page_state[page_num] = {
                    "image_bytes": image_bytes,
                    "layout": layout_result,
                    "native_blocks": native_blocks,
                    "page_mode": page_mode,
                }
            page_stage_complete(page_num, "layout", page_started_at, f"regions={len(layout_result.regions)}")
            return TaskRunResult(path=str(layout_path), metadata={"page_mode": page_mode})

        page_started_at = start_timer()
        page_stage_start(page_num, "structured", f"mode={page_mode}")
        with state_lock:
            state = dict(page_state[page_num])
        image_bytes = state["image_bytes"]
        layout_result = state["layout"]
        native_blocks = state["native_blocks"]
        page_blocks: list[StructuredBlock]
        if native_blocks:
            page_blocks = _native_page_blocks(
                document,
                page_num,
                native_blocks,
                layout_result,
                page_image_bytes=image_bytes,
                artifact_root=settings.paths.artifact_path,
                project_root=settings.paths.project_root,
            )
            matched_region_ids = _matched_layout_region_ids(native_blocks, layout_result)
            regions = [region for region in layout_result.regions if region.region_id not in matched_region_ids]
            if page_mode == PDFContentType.DIGITAL.value:
                skipped_count = len(regions)
                regions = _filter_native_pdf_residual_regions(settings, regions)
                skipped_count -= len(regions)
                if skipped_count:
                    phase_validation("ocr", f"page {page_num}: skipped {skipped_count} residual native-text region(s)")
        else:
            page_blocks = []
            regions = list(layout_result.regions)

        if regions:
            formula_regions = [region for region in regions if region.region_type == "formula"]
            ocr_regions = [region for region in regions if region.region_type != "formula"]
            ocr_blocks: list[StructuredBlock] = []
            formula_blocks: list[StructuredBlock] = []
            if ocr_regions:
                primary, fallback = ensure_ocr_providers()
                ocr_started_at = start_timer()
                use_page_ocr = not native_blocks and _should_use_page_level_ocr(settings, ocr_regions)
                if use_page_ocr:
                    page_stage_start(page_num, "OCR", f"page fallback regions={len(ocr_regions)}")
                    progress_bar("ocr", page_num, profile.page_count, f"page {page_num} page-level OCR fallback")
                    page_result = _extract_with_fallback(primary, fallback, image_bytes, "page")
                    ocr_blocks = _ocr_result_to_page_blocks(document, page_num, page_result)
                else:
                    page_stage_start(page_num, "OCR", f"regions={len(ocr_regions)}")
                    progress_bar("ocr", page_num, profile.page_count, f"page {page_num} region OCR")
                    ocr_blocks = _ocr_page_blocks(
                        document,
                        page_num,
                        image_bytes,
                        layout_result,
                        primary,
                        fallback,
                        regions=ocr_regions,
                        artifact_root=settings.paths.artifact_path,
                        project_root=settings.paths.project_root,
                    )
                page_stage_complete(page_num, "OCR", ocr_started_at, f"blocks={len(ocr_blocks)}")
                write_development_checkpoint(
                    document,
                    "ocr",
                    page=page_num,
                    summary={
                        "region_count": len(ocr_regions),
                        "block_count": len([block for block in ocr_blocks if block.provenance.source_stage == "structured_extract"]),
                        "block_types": _block_type_counts(ocr_blocks),
                    },
                )
            if formula_regions:
                formula_extractor = ensure_formula_extractor()
                progress_bar("formula", page_num, profile.page_count, f"page {page_num} formula regions")
                formula_started_at = start_timer()
                page_stage_start(page_num, "Formula", f"regions={len(formula_regions)}")
                formula_blocks = _formula_page_blocks(
                    document,
                    page_num,
                    image_bytes,
                    formula_regions,
                    formula_extractor,
                )
                page_stage_complete(page_num, "Formula", formula_started_at, f"blocks={len(formula_blocks)}")
                write_development_checkpoint(
                    document,
                    "formula",
                    page=page_num,
                    summary={
                        "region_count": len(formula_regions),
                        "formula_blocks": len([block for block in formula_blocks if block.block_type == "FormulaBlock"]),
                    },
                )
            page_blocks = sorted(page_blocks + ocr_blocks + formula_blocks, key=lambda block: (block.reading_order, block.block_id))

        structured_path = structured_dir / f"page_{page_num:04d}.json"
        output_page_mode = PDFContentType.DIGITAL.value if native_blocks else PDFContentType.SCANNED.value
        with cif_lock:
            _write_structured_page(document, page_num, page_blocks, {"page_mode": output_page_mode}, structured_path)
            for block in page_blocks:
                cif.add_block(block)
            for artifact in _artifacts_from_blocks(page_blocks):
                cif.add_artifact(artifact)
            set_status("structured", document, page_num, "done", str(structured_path))
            write_development_checkpoint(
                document,
                "structured",
                page=page_num,
                summary={
                    "output": str(structured_path),
                    "page_mode": output_page_mode,
                    "block_count": len(page_blocks),
                    "block_types": _block_type_counts(page_blocks),
                },
                artifacts={structured_path.name: structured_path},
            )
        page_stage_complete(page_num, "structured", page_started_at, f"blocks={len(page_blocks)}")
        return TaskRunResult(path=str(structured_path), metadata={"page_mode": output_page_mode})

    executor = DAGExecutor()
    result = executor.run(document=document, tasks=tasks, units=units, runner=run_task, resume=resume_task)

    if layout_loaded and layout_provider is not None:
        model_status("layout", "offloading provider")
        with gpu_scheduler.claim("layout"):
            layout_provider.offload()
    if ocr_loaded and primary_provider is not None:
        model_status("ocr", "offloading primary provider")
        with gpu_scheduler.claim("ocr"):
            primary_provider.offload()
        if fallback_loaded and fallback_provider is not None and fallback_provider is not primary_provider:
            model_status("ocr", "offloading fallback provider")
            with gpu_scheduler.claim("ocr"):
                fallback_provider.offload()
    if formula_loaded and formula_extractor is not None:
        model_status("formula", "offloading extractor")
        formula_extractor.offload()

    structured_done = [task_id for task_id in result.completed_task_ids + result.skipped_task_ids if ":structured:" in task_id]
    structured_failed = [task_id for task_id in result.failed_task_ids + result.blocked_task_ids if ":structured:" in task_id]
    return len(structured_done), len(structured_failed)



def _run_layout_detection_graph(*, source_path: Path, profile, document: str, settings, layout_dir: Path, layout_provider_name: str | None = None) -> tuple[int, int, dict[int, LayoutResult]]:
    page_modes = profile.metadata.get("page_modes", [])
    tasks: list[ExtractionTask] = []
    units: dict[str, PageUnit] = {}
    for page_num in range(1, profile.page_count + 1):
        if not should_process("layout", document, page_num):
            continue
        page_mode = page_modes[page_num - 1] if page_num - 1 < len(page_modes) else profile.pdf_content_type.value
        task, unit = _page_task(document, page_num, "layout", page_mode=page_mode, resource="layout")
        tasks.append(task)
        units[task.task_id] = unit

    if not tasks:
        return 0, 0, {}

    results: dict[int, LayoutResult] = {}
    results_lock = threading.Lock()
    provider_lock = threading.Lock()
    setup_lock = threading.Lock()
    provider = None
    provider_loaded = False

    def ensure_provider():
        nonlocal provider, provider_loaded
        with setup_lock:
            if provider is None:
                provider = build_layout_provider(layout_provider_name)
            if not provider_loaded:
                with gpu_scheduler.claim("layout"):
                    provider.load()
                provider_loaded = True
        return provider

    def run_task(task: ExtractionTask, unit: PageUnit) -> TaskRunResult:
        del task
        page_num = int(unit.metadata["page"])
        page_mode = str(unit.metadata["page_mode"])
        zoom = settings.rendering.zoom if page_mode == PDFContentType.DIGITAL.value else settings.rendering.scanned_pdf_zoom
        image_bytes = render_pdf_page_to_bytes(source_path, page_num, zoom=zoom)
        write_development_checkpoint(
            document,
            "render",
            page=page_num,
            summary={"page_mode": page_mode, "zoom": zoom, "bytes": len(image_bytes)},
            artifacts={f"page_{page_num:04d}.png": image_bytes},
        )
        layout_provider = ensure_provider()
        with provider_lock:
            layout_result = layout_provider.detect(image_bytes)
        layout_result.page_id = f"page-{page_num:04d}"
        out_path = layout_dir / f"page_{page_num:04d}.json"
        _write_layout_page(layout_result, out_path)
        write_development_checkpoint(
            document,
            "layout",
            page=page_num,
            summary={
                "output": str(out_path),
                "provider": getattr(layout_provider, "name", layout_provider.__class__.__name__),
                "region_count": len(layout_result.regions),
                "region_types": _layout_region_counts(layout_result),
                "layout_confidence": layout_result.layout_confidence,
            },
            artifacts=_layout_checkpoint_artifacts(out_path, image_bytes, layout_result, getattr(layout_provider, "name", layout_provider.__class__.__name__)),
        )
        set_status("layout", document, page_num, "done", str(out_path))
        with results_lock:
            results[page_num] = layout_result
        return TaskRunResult(path=str(out_path), metadata={"page_mode": page_mode})

    executor = DAGExecutor()
    result = executor.run(document=document, tasks=tasks, units=units, runner=run_task)

    if provider_loaded and provider is not None:
        with gpu_scheduler.claim("layout"):
            provider.offload()

    return len(result.completed_task_ids), len(result.failed_task_ids) + len(result.blocked_task_ids), results


def extract_structured_document(
    source_path: Path,
    structured_output_dir: Path | None = None,
    layout_output_dir: Path | None = None,
    layout_provider_name: str | None = None,
) -> tuple[int, int, CanonicalIntermediateFormat]:
    profile = classify_document(source_path)
    document = profile.doc_id
    phase_start("structured", f"{source_path}")
    phase_validation("intake", f"document={document} type={profile.source_kind.value} pages={profile.page_count}")
    settings = get_settings()
    structured_dir = structured_output_dir or settings.paths.structured_dir_for(document)
    layout_dir = layout_output_dir or settings.paths.layout_dir_for(document)
    structured_dir.mkdir(parents=True, exist_ok=True)
    layout_dir.mkdir(parents=True, exist_ok=True)
    init_db()
    write_development_checkpoint(document, "intake", summary=_profile_summary(profile))

    if profile.source_kind == SourceKind.HTML:
        cif = ingest_html_to_cif(source_path)
        document_path = structured_dir / "document.json"
        _write_json(document_path, asdict(cif))
        _document_checkpoint(document, "structured", cif, document_path)
        set_status("structured", document, 1, "done", str(document_path))
        phase_complete("structured", f"1 done, 0 failed, blocks={len(cif.blocks)}")
        return 1, 0, cif

    if profile.source_kind in {SourceKind.TEXT, SourceKind.MARKDOWN}:
        cif = ingest_text_to_cif(source_path)
        document_path = structured_dir / "document.json"
        _write_json(document_path, asdict(cif))
        _document_checkpoint(document, "structured", cif, document_path)
        set_status("structured", document, 1, "done", str(document_path))
        phase_complete("structured", f"1 done, 0 failed, blocks={len(cif.blocks)}")
        return 1, 0, cif

    if profile.source_kind in {SourceKind.CSV, SourceKind.TSV}:
        cif = ingest_tabular_to_cif(source_path)
        document_path = structured_dir / "document.json"
        _write_json(document_path, asdict(cif))
        _document_checkpoint(document, "structured", cif, document_path)
        set_status("structured", document, 1, "done", str(document_path))
        phase_complete("structured", f"1 done, 0 failed, blocks={len(cif.blocks)}")
        return 1, 0, cif

    if profile.source_kind == SourceKind.DOCX:
        cif = ingest_docx_to_cif(source_path)
        document_path = structured_dir / "document.json"
        _write_json(document_path, asdict(cif))
        _document_checkpoint(document, "structured", cif, document_path)
        set_status("structured", document, 1, "done", str(document_path))
        phase_complete("structured", f"1 done, 0 failed, blocks={len(cif.blocks)}")
        return 1, 0, cif

    if profile.source_kind == SourceKind.ODT:
        cif = ingest_odt_to_cif(source_path)
        document_path = structured_dir / "document.json"
        _write_json(document_path, asdict(cif))
        _document_checkpoint(document, "structured", cif, document_path)
        set_status("structured", document, 1, "done", str(document_path))
        phase_complete("structured", f"1 done, 0 failed, blocks={len(cif.blocks)}")
        return 1, 0, cif

    if profile.source_kind == SourceKind.XLSX:
        cif = ingest_xlsx_to_cif(source_path)
        document_path = structured_dir / "document.json"
        _write_json(document_path, asdict(cif))
        _document_checkpoint(document, "structured", cif, document_path)
        set_status("structured", document, 1, "done", str(document_path))
        phase_complete("structured", f"1 done, 0 failed, blocks={len(cif.blocks)}")
        return 1, 0, cif

    if profile.source_kind == SourceKind.EPUB:
        cif = ingest_epub_to_cif(source_path)
        document_path = structured_dir / "document.json"
        _write_json(document_path, asdict(cif))
        _document_checkpoint(document, "structured", cif, document_path)
        set_status("structured", document, 1, "done", str(document_path))
        phase_complete("structured", f"1 done, 0 failed, blocks={len(cif.blocks)}")
        return 1, 0, cif

    if profile.source_kind == SourceKind.IMAGE:
        primary_provider = build_primary_ocr_provider()
        fallback_provider: OCRProvider | None = None
        fallback_loaded = False
        ocr_call_gate = threading.BoundedSemaphore(max(1, settings.ocr.max_parallel_requests))

        def ensure_image_fallback_provider() -> OCRProvider | None:
            nonlocal fallback_provider, fallback_loaded
            if fallback_provider is None:
                fallback_provider = build_fallback_ocr_provider()
            if fallback_provider is None:
                return None
            if getattr(fallback_provider, "name", None) == getattr(primary_provider, "name", None):
                return _LockedOCRProvider(primary_provider, ocr_call_gate)
            if not fallback_loaded:
                model_status("ocr", f"loading fallback provider {getattr(fallback_provider, 'name', fallback_provider.__class__.__name__)}")
                with gpu_scheduler.claim("ocr"):
                    fallback_provider.load()
                fallback_loaded = True
                model_status("ocr", "fallback provider loaded")
            return _LockedOCRProvider(fallback_provider, ocr_call_gate)

        model_status("ocr", f"loading primary provider {getattr(primary_provider, 'name', primary_provider.__class__.__name__)}")
        with gpu_scheduler.claim("ocr"):
            primary_provider.load()
        model_status("ocr", "primary provider loaded")
        try:
            image_bytes = source_path.read_bytes()
            result = _extract_with_fallback(
                _LockedOCRProvider(primary_provider, ocr_call_gate),
                _LazyFallbackOCRProvider(ensure_image_fallback_provider),
                image_bytes,
                "page",
            )
            write_development_checkpoint(
                document,
                "ocr",
                page=1,
                summary={
                    "region_type": "page",
                    "provider": result.provider,
                    "block_count": len(result.typed_content.get("blocks", [])),
                    "latency_ms": getattr(result, "latency_ms", None),
                },
                artifacts={source_path.name: image_bytes},
            )
            blocks = _ocr_result_to_page_blocks(document, 1, result)
            cif = CanonicalIntermediateFormat(
                doc_id=document,
                blocks=blocks,
                metadata={"source_kind": profile.source_kind.value, "mime_type": profile.mime_type, "source_path": str(profile.source_path)},
            )
            page_path = structured_dir / "page_0001.json"
            _write_page_layout_page(document, 1, blocks, {"page_mode": profile.source_kind.value, "layout_enabled": False}, page_path)
            write_development_checkpoint(
                document,
                "structured",
                page=1,
                summary={"output": str(page_path), "block_count": len(blocks), "block_types": _block_type_counts(blocks)},
                artifacts={page_path.name: page_path},
            )
            document_path = structured_dir / "document.json"
            _write_json(document_path, asdict(cif))
            _document_checkpoint(document, "structured", cif, document_path)
            set_status("structured", document, 1, "done", str(page_path))
            phase_complete("structured", f"1 done, 0 failed, blocks={len(cif.blocks)}")
            return 1, 0, cif
        finally:
            with gpu_scheduler.claim("ocr"):
                primary_provider.offload()
            if fallback_loaded and fallback_provider is not None and fallback_provider is not primary_provider:
                with gpu_scheduler.claim("ocr"):
                    fallback_provider.offload()

    cif = CanonicalIntermediateFormat(
        doc_id=document,
        metadata={
            "source_kind": profile.source_kind.value,
            "pdf_content_type": profile.pdf_content_type.value,
            "source_path": str(profile.source_path),
            "layout_enabled": settings.layout.enabled,
        },
    )

    if not settings.layout.enabled:
        done, failed = _run_layout_disabled_pdf_graph(
            source_path=source_path,
            profile=profile,
            document=document,
            settings=settings,
            structured_dir=structured_dir,
            cif=cif,
        )
        document_path = structured_dir / "document.json"
        _write_json(document_path, asdict(cif))
        _document_checkpoint(document, "structured", cif, document_path)
        if failed:
            phase_error("structured", f"{done} done, {failed} failed")
        else:
            phase_complete("structured", f"{done} done, {failed} failed, blocks={len(cif.blocks)}")
        return done, failed, cif

    done, failed = _run_layout_enabled_pdf_graph(
        source_path=source_path,
        profile=profile,
        document=document,
        settings=settings,
        structured_dir=structured_dir,
        layout_dir=layout_dir,
        cif=cif,
        layout_provider_name=layout_provider_name,
    )
    document_path = structured_dir / "document.json"
    _write_json(document_path, asdict(cif))
    _document_checkpoint(document, "structured", cif, document_path)
    if failed:
        phase_error("structured", f"{done} done, {failed} failed")
    else:
        phase_complete("structured", f"{done} done, {failed} failed, blocks={len(cif.blocks)}")
    return done, failed, cif


def detect_layout_document(
    source_path: Path,
    layout_output_dir: Path | None = None,
    layout_provider_name: str | None = None,
) -> tuple[int, int, dict[int, LayoutResult]]:
    profile = classify_document(source_path)
    phase_start("layout", f"{source_path}")
    phase_validation("layout", f"document={profile.doc_id} type={profile.source_kind.value} pages={profile.page_count}")
    if profile.source_kind != SourceKind.PDF:
        raise ValueError("Layout detection currently supports PDF sources only")

    document = profile.doc_id
    settings = get_settings()
    layout_dir = layout_output_dir or settings.paths.layout_dir_for(document)
    layout_dir.mkdir(parents=True, exist_ok=True)
    init_db()

    done, failed, results = _run_layout_detection_graph(
        source_path=source_path,
        profile=profile,
        document=document,
        settings=settings,
        layout_dir=layout_dir,
        layout_provider_name=layout_provider_name,
    )
    if failed:
        phase_error("layout", f"{done} done, {failed} failed")
    else:
        phase_complete("layout", f"{done} done, {failed} failed")
    return done, failed, results


__all__ = ["detect_layout_document", "extract_structured_document"]
