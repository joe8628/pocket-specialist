"""Document intake, classification, and structured-ingestion helpers."""

from __future__ import annotations

import csv
import io
import json
import zipfile
from dataclasses import dataclass, field
from enum import Enum
from html.parser import HTMLParser
from pathlib import Path
import xml.etree.ElementTree as ET

import fitz

from pocket_specialist.core.cif import CanonicalIntermediateFormat, ProvenanceRecord, SourceCoords, StructuredBlock
from pocket_specialist.core.config import get_settings


class SourceKind(str, Enum):
    PDF = "pdf"
    HTML = "html"
    TEXT = "text"
    MARKDOWN = "markdown"
    IMAGE = "image"
    CSV = "csv"
    TSV = "tsv"
    DOCX = "docx"
    ODT = "odt"
    XLSX = "xlsx"
    EPUB = "epub"


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
    def __init__(self, doc_id: str, *, source_stage: str = "html_ingest", provider: str = "stdlib-html-parser") -> None:
        super().__init__(convert_charrefs=True)
        self.doc_id = doc_id
        self.source_stage = source_stage
        self.provider = provider
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
                        source_stage=self.source_stage,
                        provider=self.provider,
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
                        source_stage=self.source_stage,
                        provider=self.provider,
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


def _source_metadata(profile: DocumentProfile) -> dict[str, object]:
    metadata: dict[str, object] = {
        "source_kind": profile.source_kind.value,
        "mime_type": profile.mime_type,
        "source_path": str(profile.source_path),
    }
    if profile.pdf_content_type != PDFContentType.NONE:
        metadata["pdf_content_type"] = profile.pdf_content_type.value
    metadata.update(profile.metadata)
    return metadata


def _text_block(doc_id: str, idx: int, text: str, *, source_stage: str, provider: str, page: int = 1, heading_level: int | None = None, metadata: dict[str, object] | None = None) -> StructuredBlock:
    return StructuredBlock(
        block_id=f"{doc_id}-{source_stage}-{idx:04d}",
        doc_id=doc_id,
        block_type="TextBlock",
        content={"type": "TextBlock", "text": text, "heading_level": heading_level, "language": None},
        section_path=[],
        reading_order=idx,
        page=page,
        source_coords=SourceCoords(page=page),
        provenance=ProvenanceRecord(source_stage=source_stage, provider=provider, metadata=metadata or {}),
    )


def _table_block(doc_id: str, idx: int, headers: list[str], rows: list[list[str]], *, source_stage: str, provider: str, page: int = 1, metadata: dict[str, object] | None = None) -> StructuredBlock:
    return StructuredBlock(
        block_id=f"{doc_id}-{source_stage}-{idx:04d}",
        doc_id=doc_id,
        block_type="TableBlock",
        content={"type": "TableBlock", "headers": headers, "rows": rows, "caption": None, "text": _tabular_text(headers, rows)},
        section_path=[],
        reading_order=idx,
        page=page,
        source_coords=SourceCoords(page=page),
        provenance=ProvenanceRecord(source_stage=source_stage, provider=provider, metadata=metadata or {}),
    )


def _tabular_text(headers: list[str], rows: list[list[str]]) -> str:
    parts: list[str] = []
    if headers:
        parts.append(" | ".join(headers))
    parts.extend(" | ".join(row) for row in rows)
    return "\n".join(parts)


def _iter_zip_xml_text(source_path: Path, member: str, tags: set[str]) -> list[str]:
    with zipfile.ZipFile(source_path) as archive:
        root = ET.fromstring(archive.read(member))
    texts: list[str] = []
    for elem in root.iter():
        if elem.tag.rsplit('}', 1)[-1] in tags:
            text = "".join(elem.itertext()).strip()
            if text:
                texts.append(text)
    return texts


def _column_index(cell_ref: str) -> int:
    letters = "".join(ch for ch in cell_ref if ch.isalpha()).upper()
    index = 0
    for letter in letters:
        index = (index * 26) + (ord(letter) - 64)
    return max(index - 1, 0)


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
    if suffix == ".csv":
        return "text/csv"
    if suffix == ".tsv":
        return "text/tab-separated-values"
    if suffix in {".png"}:
        return "image/png"
    if suffix in {".jpg", ".jpeg"}:
        return "image/jpeg"
    if suffix in {".tif", ".tiff"}:
        return "image/tiff"
    if suffix == ".docx":
        return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    if suffix == ".odt":
        return "application/vnd.oasis.opendocument.text"
    if suffix == ".xlsx":
        return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    if suffix == ".epub":
        return "application/epub+zip"
    if b"<html" in stripped[:256]:
        return "text/html"
    return "application/octet-stream"


def classify_document(source_path: Path) -> DocumentProfile:
    path = source_path.resolve()
    mime = detect_mime_type(path)
    suffix = path.suffix.lower()
    doc_id = get_settings().paths.document_slug(path)

    if mime == "text/html" or suffix in {".html", ".htm"}:
        return DocumentProfile(doc_id=doc_id, source_path=path, source_kind=SourceKind.HTML, mime_type="text/html", page_count=1, text_extractable=True)
    if mime == "text/plain" or suffix in {".txt", ".log"}:
        return DocumentProfile(doc_id=doc_id, source_path=path, source_kind=SourceKind.TEXT, mime_type="text/plain", page_count=1, text_extractable=True)
    if mime == "text/markdown" or suffix in {".md", ".markdown"}:
        return DocumentProfile(doc_id=doc_id, source_path=path, source_kind=SourceKind.MARKDOWN, mime_type="text/markdown", page_count=1, text_extractable=True)
    if mime == "text/csv" or suffix == ".csv":
        return DocumentProfile(doc_id=doc_id, source_path=path, source_kind=SourceKind.CSV, mime_type="text/csv", page_count=1, text_extractable=True)
    if mime == "text/tab-separated-values" or suffix == ".tsv":
        return DocumentProfile(doc_id=doc_id, source_path=path, source_kind=SourceKind.TSV, mime_type="text/tab-separated-values", page_count=1, text_extractable=True)
    if mime.startswith("image/") or suffix in {".png", ".jpg", ".jpeg", ".tif", ".tiff"}:
        return DocumentProfile(doc_id=doc_id, source_path=path, source_kind=SourceKind.IMAGE, mime_type=mime, page_count=1, text_extractable=False, metadata={"image_format": suffix.lstrip('.')})
    if mime == "application/vnd.openxmlformats-officedocument.wordprocessingml.document" or suffix == ".docx":
        return DocumentProfile(doc_id=doc_id, source_path=path, source_kind=SourceKind.DOCX, mime_type=mime, page_count=1, text_extractable=True)
    if mime == "application/vnd.oasis.opendocument.text" or suffix == ".odt":
        return DocumentProfile(doc_id=doc_id, source_path=path, source_kind=SourceKind.ODT, mime_type=mime, page_count=1, text_extractable=True)
    if mime == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" or suffix == ".xlsx":
        return DocumentProfile(doc_id=doc_id, source_path=path, source_kind=SourceKind.XLSX, mime_type=mime, page_count=1, text_extractable=True)
    if mime == "application/epub+zip" or suffix == ".epub":
        return DocumentProfile(doc_id=doc_id, source_path=path, source_kind=SourceKind.EPUB, mime_type=mime, page_count=1, text_extractable=True)

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
            native_blocks.append(NativeTextBlock(text=clean, bbox=(int(x0), int(y0), int(x1), int(y1)), block_no=idx))
        return native_blocks


def ingest_html_to_cif(source_path: Path) -> CanonicalIntermediateFormat:
    profile = classify_document(source_path)
    parser = _HTMLStructuredParser(profile.doc_id)
    parser.feed(source_path.read_text(encoding="utf-8"))
    return CanonicalIntermediateFormat(doc_id=profile.doc_id, blocks=parser.blocks, metadata=_source_metadata(profile))


def ingest_text_to_cif(source_path: Path) -> CanonicalIntermediateFormat:
    profile = classify_document(source_path)
    text = source_path.read_text(encoding="utf-8")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    blocks = [
        _text_block(profile.doc_id, idx, line, source_stage="text_ingest", provider="native-text-reader", metadata={"mime_type": profile.mime_type})
        for idx, line in enumerate(lines, 1)
    ]
    return CanonicalIntermediateFormat(doc_id=profile.doc_id, blocks=blocks, metadata=_source_metadata(profile))


def ingest_tabular_to_cif(source_path: Path) -> CanonicalIntermediateFormat:
    profile = classify_document(source_path)
    delimiter = "," if profile.source_kind == SourceKind.CSV else "	"
    with source_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle, delimiter=delimiter)
        rows = [[cell.strip() for cell in row] for row in reader]
    headers = rows[0] if rows else []
    body = rows[1:] if len(rows) > 1 else []
    block = _table_block(profile.doc_id, 1, headers, body, source_stage="tabular_ingest", provider="stdlib-csv", metadata={"delimiter": delimiter})
    return CanonicalIntermediateFormat(doc_id=profile.doc_id, blocks=[block], metadata=_source_metadata(profile))


def ingest_docx_to_cif(source_path: Path) -> CanonicalIntermediateFormat:
    profile = classify_document(source_path)
    paragraphs = _iter_zip_xml_text(source_path, "word/document.xml", {"p"})
    blocks = [
        _text_block(profile.doc_id, idx, paragraph, source_stage="docx_ingest", provider="zip-xml-reader")
        for idx, paragraph in enumerate(paragraphs, 1)
    ]
    return CanonicalIntermediateFormat(doc_id=profile.doc_id, blocks=blocks, metadata=_source_metadata(profile))


def ingest_odt_to_cif(source_path: Path) -> CanonicalIntermediateFormat:
    profile = classify_document(source_path)
    paragraphs = _iter_zip_xml_text(source_path, "content.xml", {"p", "h"})
    blocks = [
        _text_block(profile.doc_id, idx, paragraph, source_stage="odt_ingest", provider="zip-xml-reader")
        for idx, paragraph in enumerate(paragraphs, 1)
    ]
    return CanonicalIntermediateFormat(doc_id=profile.doc_id, blocks=blocks, metadata=_source_metadata(profile))


def ingest_xlsx_to_cif(source_path: Path) -> CanonicalIntermediateFormat:
    profile = classify_document(source_path)
    blocks: list[StructuredBlock] = []
    with zipfile.ZipFile(source_path) as archive:
        shared_strings: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            shared_root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            shared_strings = ["".join(elem.itertext()).strip() for elem in shared_root.iter() if elem.tag.rsplit('}', 1)[-1] == "si"]
        sheet_names = sorted(name for name in archive.namelist() if name.startswith("xl/worksheets/sheet") and name.endswith(".xml"))
        for idx, sheet_name in enumerate(sheet_names, 1):
            root = ET.fromstring(archive.read(sheet_name))
            parsed_rows: list[list[str]] = []
            for row in [elem for elem in root.iter() if elem.tag.rsplit('}', 1)[-1] == "row"]:
                values: dict[int, str] = {}
                max_col = -1
                for cell in [elem for elem in row if elem.tag.rsplit('}', 1)[-1] == "c"]:
                    ref = cell.attrib.get("r", "A1")
                    col = _column_index(ref)
                    cell_type = cell.attrib.get("t")
                    value_elem = next((child for child in cell if child.tag.rsplit('}', 1)[-1] == "v"), None)
                    value = "" if value_elem is None or value_elem.text is None else value_elem.text
                    if cell_type == "s" and value.isdigit() and int(value) < len(shared_strings):
                        value = shared_strings[int(value)]
                    elif cell_type == "inlineStr":
                        is_elem = next((child for child in cell if child.tag.rsplit('}', 1)[-1] == "is"), None)
                        value = "".join(is_elem.itertext()).strip() if is_elem is not None else ""
                    values[col] = value.strip()
                    max_col = max(max_col, col)
                if max_col >= 0:
                    parsed_rows.append([values.get(col, "") for col in range(max_col + 1)])
            headers = parsed_rows[0] if parsed_rows else []
            body = parsed_rows[1:] if len(parsed_rows) > 1 else []
            blocks.append(_table_block(profile.doc_id, idx, headers, body, source_stage="xlsx_ingest", provider="zip-xml-reader", metadata={"sheet_path": sheet_name}))
    return CanonicalIntermediateFormat(doc_id=profile.doc_id, blocks=blocks, metadata=_source_metadata(profile))


def ingest_epub_to_cif(source_path: Path) -> CanonicalIntermediateFormat:
    profile = classify_document(source_path)
    parser = _HTMLStructuredParser(profile.doc_id, source_stage="epub_ingest", provider="epub-html-parser")
    with zipfile.ZipFile(source_path) as archive:
        container_root = ET.fromstring(archive.read("META-INF/container.xml"))
        rootfile = next((elem.attrib.get("full-path") for elem in container_root.iter() if elem.tag.rsplit('}', 1)[-1] == "rootfile"), None)
        if rootfile is None:
            raise ValueError(f"EPUB container is missing OPF rootfile: {source_path}")
        opf_root = ET.fromstring(archive.read(rootfile))
        opf_dir = Path(rootfile).parent
        manifest: dict[str, str] = {}
        for elem in opf_root.iter():
            if elem.tag.rsplit('}', 1)[-1] == "item":
                item_id = elem.attrib.get("id")
                href = elem.attrib.get("href")
                if item_id and href:
                    manifest[item_id] = str((opf_dir / href).as_posix())
        spine_ids = [elem.attrib.get("idref") for elem in opf_root.iter() if elem.tag.rsplit('}', 1)[-1] == "itemref"]
        for item_id in spine_ids:
            href = manifest.get(item_id or "")
            if href is None:
                continue
            parser.feed(archive.read(href).decode("utf-8", errors="ignore"))
    return CanonicalIntermediateFormat(doc_id=profile.doc_id, blocks=parser.blocks, metadata=_source_metadata(profile))


__all__ = [
    "DocumentProfile",
    "NativeTextBlock",
    "PDFContentType",
    "SourceKind",
    "classify_document",
    "detect_mime_type",
    "get_pdf_native_blocks",
    "ingest_docx_to_cif",
    "ingest_epub_to_cif",
    "ingest_html_to_cif",
    "ingest_odt_to_cif",
    "ingest_tabular_to_cif",
    "ingest_text_to_cif",
    "ingest_xlsx_to_cif",
    "render_pdf_page_to_bytes",
]
