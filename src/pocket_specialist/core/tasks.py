"""Typed processing and execution units used by the extraction DAG."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class ExtractionTask:
    task_id: str
    task_type: str
    dependencies: list[str] = field(default_factory=list)
    input_refs: list[str] = field(default_factory=list)
    resource_requirements: dict[str, object] = field(default_factory=dict)
    retry_count: int = 0


@dataclass(slots=True)
class ProcessingUnit:
    unit_id: str
    doc_id: str
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(slots=True)
class PageUnit(ProcessingUnit):
    image_bytes: bytes = b""


@dataclass(slots=True)
class RegionUnit(ProcessingUnit):
    region_type: str = "text"
    bbox: tuple[int, int, int, int] = (0, 0, 0, 0)
    image_bytes: bytes = b""


@dataclass(slots=True)
class FormulaUnit(ProcessingUnit):
    image_bytes: bytes = b""


@dataclass(slots=True)
class TextSectionUnit(ProcessingUnit):
    text: str = ""


@dataclass(slots=True)
class RowBatchUnit(ProcessingUnit):
    rows: list[dict[str, object]] = field(default_factory=list)
