"""Canonical Intermediate Format (CIF) primitives."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class SourceCoords:
    page: int | None = None
    bbox: tuple[int, int, int, int] | None = None
    polygons: list[list[tuple[int, int]]] | None = None


@dataclass(slots=True)
class ProvenanceRecord:
    source_stage: str
    provider: str | None = None
    confidence: float | None = None
    lineage: list[str] = field(default_factory=list)
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(slots=True)
class ProcessingArtifact:
    artifact_id: str
    artifact_type: str
    uri: str
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(slots=True)
class StructuredBlock:
    block_id: str
    doc_id: str
    block_type: str
    content: dict[str, object] = field(default_factory=dict)
    section_path: list[str] = field(default_factory=list)
    reading_order: int = 0
    page: int | None = None
    source_coords: SourceCoords | None = None
    provenance: ProvenanceRecord = field(default_factory=lambda: ProvenanceRecord(source_stage="unknown"))


@dataclass(slots=True)
class CanonicalIntermediateFormat:
    doc_id: str
    blocks: list[StructuredBlock] = field(default_factory=list)
    artifacts: list[ProcessingArtifact] = field(default_factory=list)
    metadata: dict[str, object] = field(default_factory=dict)

    def add_block(self, block: StructuredBlock) -> None:
        self.blocks.append(block)

    def add_artifact(self, artifact: ProcessingArtifact) -> None:
        self.artifacts.append(artifact)
