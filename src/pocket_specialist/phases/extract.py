"""Phase B structured extraction orchestration."""

from __future__ import annotations

import json
import re
import threading
from dataclasses import asdict
from pathlib import Path

from pocket_specialist.storage.checkpoint import init_db, set_status, should_process

from pocket_specialist.core.cif import CanonicalIntermediateFormat, ProcessingArtifact, ProvenanceRecord, SourceCoords, StructuredBlock
from pocket_specialist.core.config import get_settings
from pocket_specialist.core.dag import DAGExecutor, TaskRunResult
from pocket_specialist.core.gpu import gpu_scheduler
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

_OCR_REGION_TYPES = {"text", "heading", "table", "code", "key_value", "list", "footer", "header", "figure"}


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


def _match_region(bbox: tuple[int, int, int, int], layout: LayoutResult) -> LayoutRegion | None:
    cx = (bbox[0] + bbox[2]) / 2
    cy = (bbox[1] + bbox[3]) / 2
    best: LayoutRegion | None = None
    best_area = None
    for region in layout.regions:
        x0, y0, x1, y1 = region.bbox
        if x0 <= cx <= x1 and y0 <= cy <= y1:
            area = (x1 - x0) * (y1 - y0)
            if best_area is None or area < best_area:
                best = region
                best_area = area
    return best


def _matched_layout_region_ids(native_blocks, layout: LayoutResult) -> set[str]:
    matched: set[str] = set()
    for native in native_blocks:
        region = _match_region(native.bbox, layout)
        if region is not None:
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
    if region_type == "formula":
        return _formula_fallback_contract(text, inline=False)
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
        region_type = region.region_type if region else "text"
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
        metadata = {"region_type": region_type, "layout_region_id": region.region_id if region else None}
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


def _extract_formula_symbolic_fallback(primary: OCRProvider, fallback: OCRProvider | None, crop_bytes: bytes) -> str:
    try:
        result = _extract_with_fallback(primary, fallback, crop_bytes, "formula")
    except Exception:
        return ""
    return "\n".join(str(block["raw_text"]).strip() for block in result.typed_content["blocks"] if str(block["raw_text"]).strip())


def _ocr_page_blocks(
    document: str,
    page_num: int,
    image_bytes: bytes,
    layout: LayoutResult,
    primary_provider: OCRProvider,
    fallback_provider: OCRProvider | None,
    formula_extractor: FormulaExtractor | None = None,
    regions: list[LayoutRegion] | None = None,
    *,
    artifact_root: Path | None = None,
    project_root: Path | None = None,
) -> list[StructuredBlock]:
    blocks: list[StructuredBlock] = []
    block_index = 0
    settings = get_settings()
    for region in sorted(regions or layout.regions, key=lambda item: item.reading_order):
        block_type = _layout_region_to_block_type(region.region_type)
        if region.region_type == "formula":
            block_index += 1
            crop_bytes = crop_region_image(image_bytes, region.bbox)
            formula_result: FormulaResult | None = None
            formula_error: str | None = None
            if settings.formula.enabled and formula_extractor is not None:
                try:
                    formula_result = formula_extractor.extract(crop_bytes)
                except Exception as exc:
                    formula_error = str(exc)
                    if not settings.formula.fallback_to_ocr:
                        raise

            raw_formula_text = "" if formula_result is not None else _extract_formula_symbolic_fallback(primary_provider, fallback_provider, crop_bytes)
            content = _formula_contract(formula_result) if formula_result is not None else _formula_fallback_contract(raw_formula_text, inline=False)
            blocks.append(
                StructuredBlock(
                    block_id=f"{document}-page-{page_num:04d}-formula-{block_index:04d}",
                    doc_id=document,
                    block_type=block_type,
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
            continue
        if region.region_type not in _OCR_REGION_TYPES:
            continue

        crop_bytes = crop_region_image(image_bytes, region.bbox)
        result = _extract_with_fallback(primary_provider, fallback_provider, crop_bytes, region.region_type)
        texts = [str(block["raw_text"]).strip() for block in result.typed_content["blocks"] if str(block["raw_text"]).strip()]
        if not texts and region.region_type != "figure":
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
    if candidate in _REGION_TO_BLOCK_TYPE or candidate == "formula":
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
    def __init__(self, provider: OCRProvider | None, lock: threading.Lock) -> None:
        self._provider = provider
        self._lock = lock

    def extract(self, image_bytes: bytes, region_type: str):
        if self._provider is None:
            raise RuntimeError("OCR provider is not available")
        with self._lock:
            return self._provider.extract(image_bytes, region_type)


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
        if not should_process("structured", document, page_num):
            continue
        page_mode = page_modes[page_num - 1] if page_num - 1 < len(page_modes) else profile.pdf_content_type.value
        task, unit = _page_task(document, page_num, "structured", page_mode=page_mode)
        tasks.append(task)
        units[task.task_id] = unit

    if not tasks:
        return 0, 0

    cif_lock = threading.Lock()
    ocr_setup_lock = threading.Lock()
    ocr_call_lock = threading.Lock()
    primary_provider: OCRProvider | None = None
    fallback_provider: OCRProvider | None = None
    ocr_loaded = False

    def ensure_ocr_providers() -> tuple[OCRProvider, OCRProvider | None]:
        nonlocal primary_provider, fallback_provider, ocr_loaded
        with ocr_setup_lock:
            if primary_provider is None:
                primary_provider = build_primary_ocr_provider()
                fallback_provider = build_fallback_ocr_provider()
            if not ocr_loaded:
                with gpu_scheduler.claim("ocr"):
                    primary_provider.load()
                if fallback_provider is not None and getattr(fallback_provider, "name", None) != getattr(primary_provider, "name", None):
                    with gpu_scheduler.claim("ocr"):
                        fallback_provider.load()
                ocr_loaded = True
        primary = _LockedOCRProvider(primary_provider, ocr_call_lock)
        fallback = None
        if fallback_provider is not None:
            fallback = _LockedOCRProvider(fallback_provider, ocr_call_lock)
        return primary, fallback

    def run_task(task: ExtractionTask, unit: PageUnit) -> TaskRunResult:
        del task
        page_num = int(unit.metadata["page"])
        page_mode = str(unit.metadata["page_mode"])
        zoom = settings.rendering.zoom if page_mode == PDFContentType.DIGITAL.value else settings.rendering.scanned_pdf_zoom
        image_bytes = render_pdf_page_to_bytes(source_path, page_num, zoom=zoom)
        native_blocks = get_pdf_native_blocks(source_path, page_num)
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
            result = _extract_with_fallback(primary, fallback, image_bytes, "page")
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
        return TaskRunResult(path=str(structured_path), metadata={"page_mode": page_mode, "layout_enabled": False})

    executor = DAGExecutor()
    result = executor.run(document=document, tasks=tasks, units=units, runner=run_task)

    if ocr_loaded and primary_provider is not None:
        with gpu_scheduler.claim("ocr"):
            primary_provider.offload()
        if fallback_provider is not None and fallback_provider is not primary_provider:
            with gpu_scheduler.claim("ocr"):
                fallback_provider.offload()

    return len(result.completed_task_ids), len(result.failed_task_ids) + len(result.blocked_task_ids)



def _run_layout_enabled_pdf_graph(
    *,
    source_path: Path,
    profile,
    document: str,
    settings,
    structured_dir: Path,
    layout_dir: Path,
    cif: CanonicalIntermediateFormat,
) -> tuple[int, int]:
    page_modes = profile.metadata.get("page_modes", [])
    tasks: list[ExtractionTask] = []
    units: dict[str, PageUnit] = {}
    for page_num in range(1, profile.page_count + 1):
        if not should_process("structured", document, page_num):
            continue
        page_mode = page_modes[page_num - 1] if page_num - 1 < len(page_modes) else profile.pdf_content_type.value
        layout_task, layout_unit = _page_task(document, page_num, "layout", page_mode=page_mode, resource="layout")
        structured_task, structured_unit = _page_task(document, page_num, "structured", page_mode=page_mode, dependencies=[layout_task.task_id])
        tasks.extend([layout_task, structured_task])
        units[layout_task.task_id] = layout_unit
        units[structured_task.task_id] = structured_unit

    if not tasks:
        return 0, 0

    state_lock = threading.Lock()
    cif_lock = threading.Lock()
    page_state: dict[int, dict[str, object]] = {}

    layout_setup_lock = threading.Lock()
    layout_call_lock = threading.Lock()
    layout_provider = None
    layout_loaded = False

    ocr_setup_lock = threading.Lock()
    ocr_call_lock = threading.Lock()
    primary_provider: OCRProvider | None = None
    fallback_provider: OCRProvider | None = None
    ocr_loaded = False

    formula_setup_lock = threading.Lock()
    formula_call_lock = threading.Lock()
    formula_extractor: FormulaExtractor | None = None
    formula_loaded = False
    formula_disabled = False

    def ensure_layout_provider():
        nonlocal layout_provider, layout_loaded
        with layout_setup_lock:
            if layout_provider is None:
                layout_provider = build_layout_provider()
            if not layout_loaded:
                with gpu_scheduler.claim("layout"):
                    layout_provider.load()
                layout_loaded = True
        return layout_provider

    def ensure_ocr_providers() -> tuple[OCRProvider, OCRProvider | None]:
        nonlocal primary_provider, fallback_provider, ocr_loaded
        with ocr_setup_lock:
            if primary_provider is None:
                primary_provider = build_primary_ocr_provider()
                fallback_provider = build_fallback_ocr_provider()
            if not ocr_loaded:
                with gpu_scheduler.claim("ocr"):
                    primary_provider.load()
                if fallback_provider is not None and getattr(fallback_provider, "name", None) != getattr(primary_provider, "name", None):
                    with gpu_scheduler.claim("ocr"):
                        fallback_provider.load()
                ocr_loaded = True
        primary = _LockedOCRProvider(primary_provider, ocr_call_lock)
        fallback = None
        if fallback_provider is not None:
            fallback = _LockedOCRProvider(fallback_provider, ocr_call_lock)
        return primary, fallback

    def ensure_formula_extractor() -> FormulaExtractor | None:
        nonlocal formula_extractor, formula_loaded, formula_disabled
        if not settings.formula.enabled or formula_disabled:
            return None
        with formula_setup_lock:
            if formula_disabled:
                return None
            if formula_extractor is None:
                formula_extractor = build_formula_extractor()
            if not formula_loaded and formula_extractor is not None:
                try:
                    formula_extractor.load()
                except Exception:
                    if not settings.formula.fallback_to_ocr:
                        raise
                    formula_disabled = True
                    formula_extractor = None
                    return None
                formula_loaded = True
        if formula_extractor is None:
            return None
        return _LockedFormulaExtractor(formula_extractor, formula_call_lock)

    def run_task(task: ExtractionTask, unit: PageUnit) -> TaskRunResult:
        page_num = int(unit.metadata["page"])
        page_mode = str(unit.metadata["page_mode"])
        if task.task_type == "layout":
            zoom = settings.rendering.zoom if page_mode == PDFContentType.DIGITAL.value else settings.rendering.scanned_pdf_zoom
            image_bytes = render_pdf_page_to_bytes(source_path, page_num, zoom=zoom)
            provider = ensure_layout_provider()
            with layout_call_lock:
                layout_result = provider.detect(image_bytes)
            layout_result.page_id = f"page-{page_num:04d}"
            layout_path = layout_dir / f"page_{page_num:04d}.json"
            _write_layout_page(layout_result, layout_path)
            set_status("layout", document, page_num, "done", str(layout_path))
            native_blocks = get_pdf_native_blocks(source_path, page_num)
            with state_lock:
                page_state[page_num] = {
                    "image_bytes": image_bytes,
                    "layout": layout_result,
                    "native_blocks": native_blocks,
                    "page_mode": page_mode,
                }
            return TaskRunResult(path=str(layout_path), metadata={"page_mode": page_mode})

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
        else:
            page_blocks = []
            regions = list(layout_result.regions)

        if regions:
            primary, fallback = ensure_ocr_providers()
            formula = ensure_formula_extractor() if any(region.region_type == "formula" for region in regions) else None
            ocr_blocks = _ocr_page_blocks(
                document,
                page_num,
                image_bytes,
                layout_result,
                primary,
                fallback,
                formula,
                regions=regions,
                artifact_root=settings.paths.artifact_path,
                project_root=settings.paths.project_root,
            )
            page_blocks = sorted(page_blocks + ocr_blocks, key=lambda block: (block.reading_order, block.block_id))

        structured_path = structured_dir / f"page_{page_num:04d}.json"
        output_page_mode = PDFContentType.DIGITAL.value if native_blocks else PDFContentType.SCANNED.value
        with cif_lock:
            _write_structured_page(document, page_num, page_blocks, {"page_mode": output_page_mode}, structured_path)
            for block in page_blocks:
                cif.add_block(block)
            for artifact in _artifacts_from_blocks(page_blocks):
                cif.add_artifact(artifact)
            set_status("structured", document, page_num, "done", str(structured_path))
        return TaskRunResult(path=str(structured_path), metadata={"page_mode": output_page_mode})

    executor = DAGExecutor()
    result = executor.run(document=document, tasks=tasks, units=units, runner=run_task)

    if layout_loaded and layout_provider is not None:
        with gpu_scheduler.claim("layout"):
            layout_provider.offload()
    if ocr_loaded and primary_provider is not None:
        with gpu_scheduler.claim("ocr"):
            primary_provider.offload()
        if fallback_provider is not None and fallback_provider is not primary_provider:
            with gpu_scheduler.claim("ocr"):
                fallback_provider.offload()
    if formula_loaded and formula_extractor is not None:
        formula_extractor.offload()

    return len(result.completed_task_ids), len(result.failed_task_ids) + len(result.blocked_task_ids)



def _run_layout_detection_graph(*, source_path: Path, profile, document: str, settings, layout_dir: Path) -> tuple[int, int, dict[int, LayoutResult]]:
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
                provider = build_layout_provider()
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
        layout_provider = ensure_provider()
        with provider_lock:
            layout_result = layout_provider.detect(image_bytes)
        layout_result.page_id = f"page-{page_num:04d}"
        out_path = layout_dir / f"page_{page_num:04d}.json"
        _write_layout_page(layout_result, out_path)
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
) -> tuple[int, int, CanonicalIntermediateFormat]:
    profile = classify_document(source_path)
    document = profile.doc_id
    settings = get_settings()
    structured_dir = structured_output_dir or settings.paths.structured_dir_for(document)
    layout_dir = layout_output_dir or settings.paths.layout_dir_for(document)
    structured_dir.mkdir(parents=True, exist_ok=True)
    layout_dir.mkdir(parents=True, exist_ok=True)
    init_db()

    if profile.source_kind == SourceKind.HTML:
        cif = ingest_html_to_cif(source_path)
        _write_json(structured_dir / "document.json", asdict(cif))
        set_status("structured", document, 1, "done", str(structured_dir / "document.json"))
        return 1, 0, cif

    if profile.source_kind in {SourceKind.TEXT, SourceKind.MARKDOWN}:
        cif = ingest_text_to_cif(source_path)
        _write_json(structured_dir / "document.json", asdict(cif))
        set_status("structured", document, 1, "done", str(structured_dir / "document.json"))
        return 1, 0, cif

    if profile.source_kind in {SourceKind.CSV, SourceKind.TSV}:
        cif = ingest_tabular_to_cif(source_path)
        _write_json(structured_dir / "document.json", asdict(cif))
        set_status("structured", document, 1, "done", str(structured_dir / "document.json"))
        return 1, 0, cif

    if profile.source_kind == SourceKind.DOCX:
        cif = ingest_docx_to_cif(source_path)
        _write_json(structured_dir / "document.json", asdict(cif))
        set_status("structured", document, 1, "done", str(structured_dir / "document.json"))
        return 1, 0, cif

    if profile.source_kind == SourceKind.ODT:
        cif = ingest_odt_to_cif(source_path)
        _write_json(structured_dir / "document.json", asdict(cif))
        set_status("structured", document, 1, "done", str(structured_dir / "document.json"))
        return 1, 0, cif

    if profile.source_kind == SourceKind.XLSX:
        cif = ingest_xlsx_to_cif(source_path)
        _write_json(structured_dir / "document.json", asdict(cif))
        set_status("structured", document, 1, "done", str(structured_dir / "document.json"))
        return 1, 0, cif

    if profile.source_kind == SourceKind.EPUB:
        cif = ingest_epub_to_cif(source_path)
        _write_json(structured_dir / "document.json", asdict(cif))
        set_status("structured", document, 1, "done", str(structured_dir / "document.json"))
        return 1, 0, cif

    if profile.source_kind == SourceKind.IMAGE:
        primary_provider = build_primary_ocr_provider()
        fallback_provider = build_fallback_ocr_provider()
        with gpu_scheduler.claim("ocr"):
            primary_provider.load()
        if fallback_provider is not None and getattr(fallback_provider, "name", None) != getattr(primary_provider, "name", None):
            with gpu_scheduler.claim("ocr"):
                fallback_provider.load()
        try:
            result = _extract_with_fallback(primary_provider, fallback_provider, source_path.read_bytes(), "page")
            blocks = _ocr_result_to_page_blocks(document, 1, result)
            cif = CanonicalIntermediateFormat(
                doc_id=document,
                blocks=blocks,
                metadata={"source_kind": profile.source_kind.value, "mime_type": profile.mime_type, "source_path": str(profile.source_path)},
            )
            page_path = structured_dir / "page_0001.json"
            _write_page_layout_page(document, 1, blocks, {"page_mode": profile.source_kind.value, "layout_enabled": False}, page_path)
            _write_json(structured_dir / "document.json", asdict(cif))
            set_status("structured", document, 1, "done", str(page_path))
            return 1, 0, cif
        finally:
            with gpu_scheduler.claim("ocr"):
                primary_provider.offload()
            if fallback_provider is not None and fallback_provider is not primary_provider:
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
        _write_json(structured_dir / "document.json", asdict(cif))
        return done, failed, cif

    done, failed = _run_layout_enabled_pdf_graph(
        source_path=source_path,
        profile=profile,
        document=document,
        settings=settings,
        structured_dir=structured_dir,
        layout_dir=layout_dir,
        cif=cif,
    )
    _write_json(structured_dir / "document.json", asdict(cif))
    return done, failed, cif


def detect_layout_document(
    source_path: Path,
    layout_output_dir: Path | None = None,
) -> tuple[int, int, dict[int, LayoutResult]]:
    profile = classify_document(source_path)
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
    )
    return done, failed, results


__all__ = ["detect_layout_document", "extract_structured_document"]
