"""Phase B structured extraction orchestration."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from pocket_specialist.storage.checkpoint import init_db, set_status, should_process

from pocket_specialist.core.cif import CanonicalIntermediateFormat, ProvenanceRecord, SourceCoords, StructuredBlock
from pocket_specialist.core.config import get_settings
from pocket_specialist.handlers.intake import (
    PDFContentType,
    SourceKind,
    classify_document,
    get_pdf_native_blocks,
    ingest_html_to_cif,
    ingest_text_to_cif,
    render_pdf_page_to_bytes,
)
from pocket_specialist.layout.providers import LayoutRegion, LayoutResult, build_layout_provider, crop_region_image
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


def _parse_key_value_pairs(text: str) -> dict[str, str]:
    pairs: dict[str, str] = {}
    for line in [part.strip() for part in text.splitlines() if part.strip()]:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        pairs[key.strip()] = value.strip()
    return pairs


def _native_contract(region_type: str, text: str) -> dict[str, object]:
    if region_type == "heading":
        return {"type": "TextBlock", "text": text, "heading_level": 1, "language": None}
    if region_type == "list":
        return {"type": "ListBlock", "items": [text]}
    if region_type == "code":
        return {"type": "CodeBlock", "language": None, "code": text}
    if region_type == "table":
        return {"type": "TableBlock", "headers": [], "rows": [], "caption": None, "text": text}
    if region_type == "key_value":
        return {"type": "KeyValueBlock", "pairs": _parse_key_value_pairs(text)}
    if region_type == "figure":
        return {"type": "FigureBlock", "caption": None, "alt_text": "", "embedded_text": [text] if text else None}
    if region_type == "formula":
        return {"type": "FormulaBlock", "raw_formula_text": text, "provider": "ocr-fallback", "inline": False}
    return {"type": "TextBlock", "text": text, "heading_level": None, "language": None}


def _ocr_contract(region_type: str, texts: list[str]) -> dict[str, object]:
    merged_text = "\n".join(texts)
    if region_type == "heading":
        return {"type": "TextBlock", "text": merged_text, "heading_level": 1, "language": None}
    if region_type == "list":
        return {"type": "ListBlock", "items": texts}
    if region_type == "code":
        return {"type": "CodeBlock", "language": None, "code": merged_text}
    if region_type == "table":
        return {"type": "TableBlock", "headers": [], "rows": [], "caption": None, "text": merged_text}
    if region_type == "key_value":
        return {"type": "KeyValueBlock", "pairs": _parse_key_value_pairs(merged_text)}
    if region_type == "figure":
        return {"type": "FigureBlock", "caption": None, "alt_text": merged_text, "embedded_text": texts or None}
    return {"type": "TextBlock", "text": merged_text, "heading_level": None, "language": None}


def _native_page_blocks(document: str, page_num: int, native_blocks, layout: LayoutResult) -> list[StructuredBlock]:
    blocks: list[StructuredBlock] = []
    for idx, native in enumerate(native_blocks, 1):
        region = _match_region(native.bbox, layout)
        region_type = region.region_type if region else "text"
        block_type = _layout_region_to_block_type(region_type)
        block = StructuredBlock(
            block_id=f"{document}-page-{page_num:04d}-native-{idx:04d}",
            doc_id=document,
            block_type=block_type,
            content=_native_contract(region_type, native.text),
            section_path=[],
            reading_order=region.reading_order if region else idx,
            page=page_num,
            source_coords=SourceCoords(page=page_num, bbox=native.bbox),
            provenance=ProvenanceRecord(
                source_stage="structured_extract",
                provider="fitz-native-text",
                metadata={"region_type": region_type, "layout_region_id": region.region_id if region else None},
            ),
        )
        blocks.append(block)
    return sorted(blocks, key=lambda block: block.reading_order)


def _extract_with_fallback(primary: OCRProvider, fallback: OCRProvider | None, crop_bytes: bytes, region_type: str):
    try:
        return primary.extract(crop_bytes, region_type)
    except Exception:
        if fallback is None:
            raise
        return fallback.extract(crop_bytes, region_type)


def _ocr_page_blocks(document: str, page_num: int, image_bytes: bytes, layout: LayoutResult, primary_provider: OCRProvider, fallback_provider: OCRProvider | None) -> list[StructuredBlock]:
    blocks: list[StructuredBlock] = []
    block_index = 0
    for region in sorted(layout.regions, key=lambda item: item.reading_order):
        block_type = _layout_region_to_block_type(region.region_type)
        if region.region_type == "formula":
            block_index += 1
            blocks.append(
                StructuredBlock(
                    block_id=f"{document}-page-{page_num:04d}-formula-{block_index:04d}",
                    doc_id=document,
                    block_type=block_type,
                    content={"type": "FormulaBlock", "raw_formula_text": "", "provider": "ocr-fallback", "inline": False},
                    section_path=[],
                    reading_order=region.reading_order,
                    page=page_num,
                    source_coords=SourceCoords(page=page_num, bbox=region.bbox),
                    provenance=ProvenanceRecord(
                        source_stage="structured_extract",
                        provider="layout-routing",
                        confidence=region.confidence,
                        metadata={"layout_region_id": region.region_id, "region_type": region.region_type},
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
        blocks.append(
            StructuredBlock(
                block_id=f"{document}-page-{page_num:04d}-ocr-{block_index:04d}",
                doc_id=document,
                block_type=block_type,
                content=_ocr_contract(region.region_type, texts),
                section_path=[],
                reading_order=region.reading_order,
                page=page_num,
                source_coords=SourceCoords(page=page_num, bbox=region.bbox),
                provenance=ProvenanceRecord(
                    source_stage="structured_extract",
                    provider=result.provider,
                    confidence=result.confidence,
                    metadata={
                        "region_type": region.region_type,
                        "layout_region_id": region.region_id,
                        "ocr_mode": result.extraction_metadata.get("mode"),
                    },
                ),
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

    layout_provider = build_layout_provider()
    layout_provider.load()
    page_layouts: dict[int, LayoutResult] = {}
    page_images: dict[int, bytes] = {}
    native_pages: set[int] = set()
    cif = CanonicalIntermediateFormat(
        doc_id=document,
        metadata={
            "source_kind": profile.source_kind.value,
            "pdf_content_type": profile.pdf_content_type.value,
            "source_path": str(profile.source_path),
        },
    )

    done = failed = 0
    try:
        page_modes = profile.metadata.get("page_modes", [])
        for page_num in range(1, profile.page_count + 1):
            if not should_process("structured", document, page_num):
                continue
            page_mode = page_modes[page_num - 1] if page_num - 1 < len(page_modes) else profile.pdf_content_type.value
            zoom = settings.rendering.zoom if page_mode == PDFContentType.DIGITAL.value else settings.rendering.scanned_pdf_zoom
            image_bytes = render_pdf_page_to_bytes(source_path, page_num, zoom=zoom)
            page_images[page_num] = image_bytes
            layout_result = layout_provider.detect(image_bytes)
            layout_result.page_id = f"page-{page_num:04d}"
            page_layouts[page_num] = layout_result
            layout_path = layout_dir / f"page_{page_num:04d}.json"
            _write_layout_page(layout_result, layout_path)
            set_status("layout", document, page_num, "done", str(layout_path))
            native_blocks = get_pdf_native_blocks(source_path, page_num)
            if native_blocks:
                native_pages.add(page_num)
                page_blocks = _native_page_blocks(document, page_num, native_blocks, layout_result)
                structured_path = structured_dir / f"page_{page_num:04d}.json"
                _write_structured_page(document, page_num, page_blocks, {"page_mode": PDFContentType.DIGITAL.value}, structured_path)
                for block in page_blocks:
                    cif.add_block(block)
                set_status("structured", document, page_num, "done", str(structured_path))
                done += 1
    finally:
        layout_provider.offload()

    scanned_pages = [page_num for page_num in range(1, profile.page_count + 1) if page_num not in native_pages and should_process("structured", document, page_num)]
    if scanned_pages:
        primary_provider = build_primary_ocr_provider()
        fallback_provider = build_fallback_ocr_provider()
        primary_provider.load()
        if getattr(fallback_provider, "name", None) != getattr(primary_provider, "name", None):
            fallback_provider.load()
        try:
            for page_num in scanned_pages:
                layout_result = page_layouts[page_num]
                page_blocks = _ocr_page_blocks(document, page_num, page_images[page_num], layout_result, primary_provider, fallback_provider)
                structured_path = structured_dir / f"page_{page_num:04d}.json"
                _write_structured_page(document, page_num, page_blocks, {"page_mode": PDFContentType.SCANNED.value}, structured_path)
                for block in page_blocks:
                    cif.add_block(block)
                set_status("structured", document, page_num, "done", str(structured_path))
                done += 1
        finally:
            primary_provider.offload()
            if fallback_provider is not primary_provider:
                fallback_provider.offload()

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

    provider = build_layout_provider()
    provider.load()
    done = failed = 0
    results: dict[int, LayoutResult] = {}
    try:
        page_modes = profile.metadata.get("page_modes", [])
        for page_num in range(1, profile.page_count + 1):
            page_mode = page_modes[page_num - 1] if page_num - 1 < len(page_modes) else profile.pdf_content_type.value
            zoom = settings.rendering.zoom if page_mode == PDFContentType.DIGITAL.value else settings.rendering.scanned_pdf_zoom
            image_bytes = render_pdf_page_to_bytes(source_path, page_num, zoom=zoom)
            result = provider.detect(image_bytes)
            result.page_id = f"page-{page_num:04d}"
            results[page_num] = result
            out_path = layout_dir / f"page_{page_num:04d}.json"
            _write_layout_page(result, out_path)
            set_status("layout", document, page_num, "done", str(out_path))
            done += 1
    finally:
        provider.offload()

    return done, failed, results


__all__ = ["detect_layout_document", "extract_structured_document"]
