"""Shared data models used across compatibility modules and Phase A foundations."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from pipeline.foundation.cif import ProvenanceRecord, SourceCoords, StructuredBlock


class BlockType(str, Enum):
    TEXT = "text"
    HEADING = "heading"
    EQUATION = "equation"
    EQUATION_FAILED = "equation_failed"
    FOOTNOTE = "footnote"
    FIGURE = "figure"
    CAPTION = "caption"
    TABLE = "table"
    LIST_ITEM = "list_item"
    UNKNOWN = "unknown"


@dataclass
class BoundingBox:
    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def height(self) -> float:
        return self.y1 - self.y0

    def to_dict(self) -> dict:
        return {"x0": self.x0, "y0": self.y0, "x1": self.x1, "y1": self.y1}

    @classmethod
    def from_dict(cls, d: dict) -> BoundingBox:
        return cls(d["x0"], d["y0"], d["x1"], d["y1"])

    def to_tuple(self) -> tuple[int, int, int, int]:
        return (int(self.x0), int(self.y0), int(self.x1), int(self.y1))


@dataclass
class TextBlock:
    bbox: BoundingBox
    raw_text: str
    confidence: float
    block_type: BlockType = BlockType.UNKNOWN
    latex: Optional[str] = None
    latex_confidence: Optional[float] = None

    def to_dict(self) -> dict:
        return {
            "bbox": self.bbox.to_dict(),
            "raw_text": self.raw_text,
            "confidence": self.confidence,
            "block_type": self.block_type.value,
            "latex": self.latex,
            "latex_confidence": self.latex_confidence,
        }

    def to_structured_block(
        self,
        *,
        block_id: str,
        doc_id: str,
        reading_order: int,
        page: int | None = None,
        provider: str | None = None,
        source_stage: str = "ocr",
        section_path: list[str] | None = None,
    ) -> StructuredBlock:
        content: dict[str, object] = {"text": self.raw_text}
        if self.latex is not None:
            content["latex"] = self.latex
        if self.latex_confidence is not None:
            content["latex_confidence"] = self.latex_confidence
        return StructuredBlock(
            block_id=block_id,
            doc_id=doc_id,
            block_type=self.block_type.value,
            content=content,
            section_path=section_path or [],
            reading_order=reading_order,
            page=page,
            source_coords=SourceCoords(page=page, bbox=self.bbox.to_tuple()),
            provenance=ProvenanceRecord(
                source_stage=source_stage,
                provider=provider,
                confidence=self.confidence,
                metadata={"adapter_model": "TextBlock"},
            ),
        )

    @classmethod
    def from_dict(cls, d: dict) -> TextBlock:
        return cls(
            bbox=BoundingBox.from_dict(d["bbox"]),
            raw_text=d["raw_text"],
            confidence=d["confidence"],
            block_type=BlockType(d.get("block_type", "unknown")),
            latex=d.get("latex"),
            latex_confidence=d.get("latex_confidence"),
        )


@dataclass
class PageRecord:
    page_num: int
    image_path: str
    blocks: list[TextBlock] = field(default_factory=list)
    corrected_markdown: Optional[str] = None
    ocr_status: str = "pending"
    equation_status: str = "pending"
    correction_status: str = "pending"


@dataclass
class EquationCrop:
    page_num: int
    crop_index: int
    bbox: BoundingBox
    crop_path: str
    latex: Optional[str] = None
    confidence: Optional[float] = None
    failed: bool = False


@dataclass
class OutputRecord:
    page_num: int
    heading: Optional[str]
    content: Optional[str]
    latex_blocks: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "page_num": self.page_num,
            "heading": self.heading,
            "content": self.content,
            "latex_blocks": self.latex_blocks,
        }


@dataclass
class OutputManifest:
    source_pdf: str
    total_pages: int
    pages_succeeded: int
    pages_failed: int
    records: list[OutputRecord] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "source_pdf": self.source_pdf,
            "total_pages": self.total_pages,
            "pages_succeeded": self.pages_succeeded,
            "pages_failed": self.pages_failed,
            "records": [r.to_dict() for r in self.records],
        }
