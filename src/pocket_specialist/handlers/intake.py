"""Document intake, classification, and structured-ingestion helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from html.parser import HTMLParser
from pathlib import Path

import fitz

from pocket_specialist.core.cif import CanonicalIntermediateFormat, ProvenanceRecord, SourceCoords, StructuredBlock
from pocket_specialist.core.config import get_settings


class SourceKind(str, Enum):
    PDF = "pdf"
    HTML = "html"
    TEXT = "text"
    MARKDOWN = "markdown"


class PDFContentType(str, Enum):
    DIGITAL = "digital"
    SCANNED = "scanned"
    HYBRID = "hybrid"
    NONE = "none"


@dataclass(slots=True)
class NativeTextBlock:
    text: str
    bbox: tuple[int, int, int, int]
    block_no: int


@dataclass(slots=True)
class DocumentProfile:
    doc_id: str
    source_path: Path
    source_kind: SourceKind
    mime_type: str
    pdf_content_type: PDFContentType = PDFContentType.NONE
    page_count: int = 0
    text_extractable: bool = False
    metadata: dict[str, object] = field(default_factory=dict)


class _HTMLStructuredParser(HTMLParser):
    def __init__(self, doc_id: str) -> None:
        super().__init__(convert_charrefs=True)
        self.doc_id = doc_id
        self.blocks: list[StructuredBlock] = []
        self._tag_stack: list[str] = []
        self._text_parts: list[str] = []
        self._section_path: list[str] = []
        self._table_depth = 0
        self._reading_order = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._tag_stack.append(tag)
        attr_map = {key: value for key, value in attrs}
        if tag == "table":
            self._table_depth += 1
        if tag == "img":
            alt_text = (attr_map.get("alt") or "").strip()
            src = attr_map.get("src") or ""
            self._reading_order += 1
            self.blocks.append(
                StructuredBlock(
                    block_id=f"{self.doc_id}-html-{self._reading_order:04d}",
                    doc_id=self.doc_id,
                    block_type="FigureBlock",
                    content={
                        "type": "FigureBlock",
                        "caption": None,
                        "alt_text": alt_text,
                        "embedded_text": None,
                        "artifact_uri": src or None,
                    },
                    section_path=list(self._section_path),
                    reading_order=self._reading_order,
                    page=1,
                    source_coords=SourceCoords(page=1),
                    provenance=ProvenanceRecord(
                        source_stage="html_ingest",
                        provider="stdlib-html-parser",
                        metadata={"tag": tag, "src": src},
                    ),
                )
            )

    def handle_endtag(self, tag: str) -> None:
        block_tags = {"p", "pre", "li", "table", "h1", "h2", "h3", "h4", "h5", "h6"}
        text = " ".join(part.strip() for part in self._text_parts if part.strip()).strip()
        if tag in block_tags and text:
            self._reading_order += 1
            block_type, content, section_path = self._coerce_block(tag, text)
            self.blocks.append(
                StructuredBlock(
                    block_id=f"{self.doc_id}-html-{self._reading_order:04d}",
                    doc_id=self.doc_id,
                    block_type=block_type,
                    content=content,
                    section_path=section_path,
                    reading_order=self._reading_order,
                    page=1,
                    source_coords=SourceCoords(page=1),
                    provenance=ProvenanceRecord(
                        source_stage="html_ingest",
                        provider="stdlib-html-parser",
                        metadata={"tag": tag},
                    ),
                )
            )
            if tag.startswith("h") and len(tag) == 2 and tag[1].isdigit():
                level = int(tag[1])
                self._section_path = self._section_path[: max(level - 1, 0)]
                self._section_path.append(text)
        if tag == "table":
            self._table_depth = max(self._table_depth - 1, 0)
        if self._tag_stack and self._tag_stack[-1] == tag:
            self._tag_stack.pop()
        if tag in block_tags:
            self._text_parts = []

    def handle_data(self, data: str) -> None:
        if data.strip():
            self._text_parts.append(data)

    def _coerce_block(self, tag: str, text: str) -> tuple[str, dict[str, object], list[str]]:
        if tag.startswith("h") and len(tag) == 2 and tag[1].isdigit():
            return "TextBlock", {"type": "TextBlock", "text": text, "heading_level": int(tag[1]), "language": None}, list(self._section_path)
        if tag == "li":
            return "ListBlock", {"type": "ListBlock", "items": [text]}, list(self._section_path)
        if tag == "pre":
            return "CodeBlock", {"type": "CodeBlock", "language": None, "code": text}, list(self._section_path)
        if tag == "table" or self._table_depth:
            return "TableBlock", {"type": "TableBlock", "headers": [], "rows": [], "caption": None, "text": text}, list(self._section_path)
        return "TextBlock", {"type": "TextBlock", "text": text, "heading_level": None, "language": None}, list(self._section_path)


def detect_mime_type(source_path: Path) -> str:
    sample = source_path.read_bytes()[:1024]
    suffix = source_path.suffix.lower()
    if sample.startswith(b"%PDF"):
        return "application/pdf"
    stripped = sample.lstrip().lower()
    if stripped.startswith(b"<!doctype html") or stripped.startswith(b"<html"):
        return "text/html"
    if suffix in {".md", ".markdown"}:
        return "text/markdown"
    if suffix in {".txt", ".log"}:
        return "text/plain"
    if b"<html" in stripped[:256]:
        return "text/html"
    return "application/octet-stream"


def classify_document(source_path: Path) -> DocumentProfile:
    path = source_path.resolve()
    mime = detect_mime_type(path)
    suffix = path.suffix.lower()
    doc_id = get_settings().paths.document_slug(path)

    if mime == "text/html" or suffix in {".html", ".htm"}:
        return DocumentProfile(
            doc_id=doc_id,
            source_path=path,
            source_kind=SourceKind.HTML,
            mime_type="text/html",
            page_count=1,
            text_extractable=True,
        )

    if mime == "text/plain" or suffix in {".txt", ".log"}:
        return DocumentProfile(
            doc_id=doc_id,
            source_path=path,
            source_kind=SourceKind.TEXT,
            mime_type="text/plain",
            page_count=1,
            text_extractable=True,
        )

    if mime == "text/markdown" or suffix in {".md", ".markdown"}:
        return DocumentProfile(
            doc_id=doc_id,
            source_path=path,
            source_kind=SourceKind.MARKDOWN,
            mime_type="text/markdown",
            page_count=1,
            text_extractable=True,
        )

    if mime != "application/pdf" and suffix != ".pdf":
        raise ValueError(f"Unsupported source type or MIME mismatch: {path}")

    with fitz.open(str(path)) as doc:
        page_count = len(doc)
        extractable_pages = 0
        page_modes: list[str] = []
        for page in doc:
            text = page.get_text("text").strip()
            if len(text) >= 32:
                extractable_pages += 1
                page_modes.append(PDFContentType.DIGITAL.value)
            else:
                page_modes.append(PDFContentType.SCANNED.value)

    if extractable_pages == 0:
        pdf_type = PDFContentType.SCANNED
    elif extractable_pages == page_count:
        pdf_type = PDFContentType.DIGITAL
    else:
        pdf_type = PDFContentType.HYBRID

    return DocumentProfile(
        doc_id=doc_id,
        source_path=path,
        source_kind=SourceKind.PDF,
        mime_type="application/pdf",
        pdf_content_type=pdf_type,
        page_count=page_count,
        text_extractable=extractable_pages > 0,
        metadata={"page_modes": page_modes, "extractable_pages": extractable_pages},
    )


def render_pdf_page_to_bytes(pdf_path: Path, page_num: int, zoom: float | None = None) -> bytes:
    zoom_factor = zoom or get_settings().rendering.zoom
    with fitz.open(str(pdf_path)) as doc:
        page = doc[page_num - 1]
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom_factor, zoom_factor))
        return pix.tobytes("png")


def get_pdf_native_blocks(pdf_path: Path, page_num: int) -> list[NativeTextBlock]:
    with fitz.open(str(pdf_path)) as doc:
        page = doc[page_num - 1]
        native_blocks: list[NativeTextBlock] = []
        for idx, block in enumerate(page.get_text("blocks")):
            x0, y0, x1, y1, text, *_ = block
            clean = text.strip()
            if not clean:
                continue
            native_blocks.append(
                NativeTextBlock(
                    text=clean,
                    bbox=(int(x0), int(y0), int(x1), int(y1)),
                    block_no=idx,
                )
            )
        return native_blocks


def ingest_html_to_cif(source_path: Path) -> CanonicalIntermediateFormat:
    profile = classify_document(source_path)
    parser = _HTMLStructuredParser(profile.doc_id)
    parser.feed(source_path.read_text(encoding="utf-8"))
    return CanonicalIntermediateFormat(
        doc_id=profile.doc_id,
        blocks=parser.blocks,
        metadata={
            "source_kind": profile.source_kind.value,
            "mime_type": profile.mime_type,
            "source_path": str(profile.source_path),
        },
    )


def ingest_text_to_cif(source_path: Path) -> CanonicalIntermediateFormat:
    profile = classify_document(source_path)
    text = source_path.read_text(encoding="utf-8")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    blocks: list[StructuredBlock] = []
    for idx, line in enumerate(lines, 1):
        blocks.append(
            StructuredBlock(
                block_id=f"{profile.doc_id}-text-{idx:04d}",
                doc_id=profile.doc_id,
                block_type="TextBlock",
                content={"type": "TextBlock", "text": line, "heading_level": None, "language": None},
                section_path=[],
                reading_order=idx,
                page=1,
                source_coords=SourceCoords(page=1),
                provenance=ProvenanceRecord(
                    source_stage="text_ingest",
                    provider="native-text-reader",
                    metadata={"mime_type": profile.mime_type},
                ),
            )
        )
    return CanonicalIntermediateFormat(
        doc_id=profile.doc_id,
        blocks=blocks,
        metadata={
            "source_kind": profile.source_kind.value,
            "mime_type": profile.mime_type,
            "source_path": str(profile.source_path),
        },
    )


__all__ = [
    "DocumentProfile",
    "NativeTextBlock",
    "PDFContentType",
    "SourceKind",
    "classify_document",
    "detect_mime_type",
    "get_pdf_native_blocks",
    "ingest_html_to_cif",
    "ingest_text_to_cif",
    "render_pdf_page_to_bytes",
]
