# Document Intelligence Pipeline — Architecture Specification (Beta)

**Version:** 0.5.0-beta  
**Status:** Pre-production architecture baseline  
**Date:** May 2026

---

# 1. Overview

This system is a local-first, agent-oriented document intelligence pipeline for heterogeneous document ingestion, structured extraction, semantic indexing, and retrieval.

The pipeline ingests:

- PDF
- HTML
- DOCX
- XLSX
- CSV/TSV
- EPUB
- Images
- TXT / Markdown

and produces:

- typed semantic blocks
- retrieval-ready chunks
- structured metadata
- provenance-aware embeddings
- machine-consumable outputs for agents/tools

The architecture is explicitly designed around:

- typed intermediate representations
- deterministic phase boundaries
- explicit GPU lifecycle management
- provider abstraction
- structured outputs (never markdown intermediates)
- resumable processing
- operational isolation

Markdown exists only as a rendering/export layer.

---

# 2. Core Architectural Principles

| Principle | Description |
|---|---|
| Typed intermediates | All extraction outputs are structured machine-readable objects |
| OCR is not parsing | OCR providers extract content, not final semantic interpretation |
| Rendering is not storage | Markdown is generated on demand and never used as intermediate state |
| Explicit GPU ownership | Models must be loaded and offloaded deterministically |
| Layout is first-class | Layout detection is a dedicated subsystem |
| Extraction is graph-shaped | Internal execution is a DAG, not a purely linear flow |
| Provider isolation | OCR, layout, formula extraction, and embedding providers are independently swappable |
| Operational resilience | Pipeline must degrade gracefully under subsystem failures |
| Provenance preservation | Every extracted artifact retains source coordinates and lineage |

---

# 3. High-Level System Architecture

```text
                    ┌────────────────────┐
                    │ Intake / Queue API │
                    └─────────┬──────────┘
                              │
                              ▼
                 ┌─────────────────────────┐
                 │ Phase 1 — Classification│
                 └─────────┬───────────────┘
                           │
                           ▼
                 ┌─────────────────────────┐
                 │ Phase 2 — Preprocessing │
                 │ + Layout Detection      │
                 └─────────┬───────────────┘
                           │
          ┌────────────────┼─────────────────┐
          ▼                ▼                 ▼
   Structured Path     OCR Path         Formula Path
          │                │                 │
          │                ▼                 ▼
          │       OCR Provider Layer   Formula Extractor
          │         (Ollama VLMs)        (UniMERNet)
          │                │                 │
          └────────────────┴─────────────────┘
                           │
                           ▼
            Canonical Intermediate Format
                           │
                           ▼
               Structure & Enrichment
                           │
                           ▼
                   Chunk Serialization
                           │
                           ▼
                    Embedding / Indexing
                           │
                           ▼
                    Retrieval Interfaces
```

---

# 4. Extraction Graph Model

The pipeline is internally modeled as a dependency graph rather than a strictly linear workflow.

## 4.1 ExtractionTask

```python
@dataclass
class ExtractionTask:
    task_id: str
    task_type: str
    dependencies: list[str]
    input_refs: list[str]
    resource_requirements: dict
    retry_count: int = 0
```

## 4.2 Why DAG Execution

This enables:

- independent retries
- partial recomputation
- task-level observability
- resource scheduling
- layout reuse
- caching
- concurrent CPU execution
- serialized GPU execution

---

# 5. Layout Detection Layer

Layout detection is a first-class subsystem.

## 5.1 Responsibilities

The layout layer owns:

- region classification
- bounding boxes
- reading order
- formula routing
- table routing
- OCR scoping
- page segmentation

OCR providers do not perform authoritative layout reconstruction except in degraded fallback modes.

## 5.2 LayoutProvider Interface

```python
class LayoutProvider(Protocol):
    def load(self) -> None: ...
    def detect(self, image_bytes: bytes) -> LayoutResult: ...
    def offload(self) -> None: ...
```

## 5.3 LayoutResult

```python
@dataclass
class LayoutRegion:
    region_id: str
    region_type: str
    bbox: tuple[int, int, int, int]
    confidence: float
    reading_order: int

@dataclass
class LayoutResult:
    page_id: str
    regions: list[LayoutRegion]
    layout_confidence: float | None
```

## 5.4 Supported Region Types

- text
- heading
- table
- formula
- code
- figure
- key_value
- list
- footer
- header

---

# 6. OCR Provider Abstraction

OCR providers are interchangeable implementations behind a shared protocol.

The pipeline never constructs provider prompts directly.

## 6.1 OCRProvider Interface

```python
class OCRProvider(Protocol):
    def load(self) -> None: ...

    def extract(
        self,
        image_bytes: bytes,
        region_type: str,
    ) -> OCRResult: ...

    def extract_batch(
        self,
        units: list[RegionUnit],
    ) -> list[OCRResult]: ...

    def offload(self) -> None: ...

    def health_check(self) -> bool: ...
```

## 6.2 OCRResult

```python
@dataclass
class OCRResult:
    region_type: str
    typed_content: dict

    provider: str

    confidence: float | None
    structure_confidence: float | None

    bounding_boxes: list | None

    raw_response: str
    latency_ms: int

    extraction_metadata: dict
```

## 6.3 Extraction Modes

Two extraction strategies are supported.

| Mode | Description |
|---|---|
| REGION_GUIDED | Layout detector determines regions ahead of OCR |
| PAGE_STRUCTURED | OCR model reconstructs page structure directly |

`PAGE_STRUCTURED` is a degraded fallback mode and should not be the primary extraction path.

---

# 7. Output Validation & Repair Layer

All model outputs pass through validation before entering CIF.

This layer is mandatory.

## 7.1 Responsibilities

- strict JSON parsing
- malformed JSON repair
- schema validation
- provider normalization
- retry logic
- fallback escalation
- extraction provenance tracking

## 7.2 Validation Flow

```text
Model Output
    │
    ├── strict parse
    ├── repair attempt
    ├── schema validation
    ├── retry if invalid
    └── fallback provider if exhausted
```

## 7.3 Retry Policy

| Failure Type | Action |
|---|---|
| malformed JSON | repair + retry once |
| schema mismatch | retry with constrained prompt |
| timeout | retry with smaller batch |
| CUDA OOM | reduce batch size |
| provider unavailable | route to fallback provider |

---

# 8. Formula Extraction Layer

Formula extraction is isolated from general OCR.

## 8.1 FormulaExtractor Interface

```python
class FormulaExtractor(Protocol):
    def load(self) -> None: ...
    def extract(self, image_bytes: bytes) -> FormulaResult: ...
    def extract_batch(self, crops: list[bytes]) -> list[FormulaResult]: ...
    def offload(self) -> None: ...
```

## 8.2 FormulaResult

```python
@dataclass
class FormulaResult:
    latex: str
    mathml: str | None

    provider: str

    confidence: float | None

    is_inline: bool

    raw_response: str
    latency_ms: int
```

## 8.3 Formula Routing

Formula regions are identified via:

1. layout detection
2. inline heuristics
3. OCR symbolic density heuristics

Inline equations may trigger re-extraction after OCR.

## 8.4 Formula Fallback

If formula extraction fails:

```json
{
  "type": "FormulaFallback",
  "raw_formula_text": "..."
}
```

This preserves semantic provenance.

---

# 9. VRAM Lifecycle Management

GPU ownership is deterministic.

No model may remain resident between stages.

## 9.1 Core Rule

> Models must be explicitly loaded before processing and explicitly offloaded immediately after processing.

## 9.2 GPU Scheduler

GPU execution is serialized.

```python
class GPUScheduler:
    async def acquire(self, resource_type: str): ...
```

Only one GPU-heavy task category may execute simultaneously.

## 9.3 Lifecycle Sequence

```text
Structured Parse
    ↓
Load OCR
    ↓
OCR Batch
    ↓
Offload OCR
    ↓
Load Formula Model
    ↓
Formula Batch
    ↓
Offload Formula Model
    ↓
Load Embedder
    ↓
Embedding Batch
    ↓
Offload Embedder
```

## 9.4 OOM Recovery

```text
CUDA OOM
    ↓
empty_cache()
    ↓
halve batch size
    ↓
retry once
    ↓
degraded queue
```

---

# 10. Processing Units

Processing units are strongly typed.

## 10.1 Base Unit

```python
@dataclass
class ProcessingUnit:
    unit_id: str
    doc_id: str
    metadata: dict
```

## 10.2 Specialized Units

```python
class PageUnit(ProcessingUnit):
    image_bytes: bytes

class RegionUnit(ProcessingUnit):
    region_type: str
    bbox: tuple[int, int, int, int]
    image_bytes: bytes

class FormulaUnit(ProcessingUnit):
    image_bytes: bytes

class TextSectionUnit(ProcessingUnit):
    text: str

class RowBatchUnit(ProcessingUnit):
    rows: list[dict]
```

---

# 11. Canonical Intermediate Format (CIF)

All extracted content converges into CIF.

CIF is the authoritative internal representation.

## 11.1 Core Guarantees

- deterministic structure
- typed semantics
- provenance retention
- rendering independence
- storage portability

## 11.2 StructuredBlock

```python
@dataclass
class StructuredBlock:
    block_id: str
    doc_id: str

    block_type: str

    content: dict

    section_path: list[str]
    reading_order: int

    page: int | None

    source_coords: dict | None

    provenance: dict
```

## 11.3 Block Types

| Block Type | Purpose |
|---|---|
| TextBlock | Paragraphs/headings |
| TableBlock | Structured tables |
| FormulaBlock | Mathematical expressions |
| CodeBlock | Source code |
| FigureBlock | Figures/charts/diagrams |
| KeyValueBlock | Form extraction |
| ListBlock | Ordered/unordered lists |
| MetaBlock | Document metadata |

## 11.4 FigureBlock

Figure artifacts are externally stored.

```json
{
  "artifact_uri": "artifacts/fig_001.png",
  "caption": "...",
  "ocr_text": "..."
}
```

Binary payloads are never embedded inside CIF.

---

# 12. Phase 3 — Structured Output Contracts

## 12.1 Routing Table

| Unit Type | Routing Target | region_type | Structured Output Contract | Notes |
|---|---|---|---|---|
| Scanned text region | OCR Provider | `"text"` | `{ "type": "TextBlock", "text": str, "heading_level": int \| null, "language": str \| null }` | Paragraphs/headings |
| Table crop | OCR Provider | `"table"` | `{ "type": "TableBlock", "headers": list[str], "rows": list[dict[str, Any]], "caption": str \| null }` | Object-normalized rows |
| Code region | OCR Provider | `"code"` | `{ "type": "CodeBlock", "language": str \| null, "code": str }` | Preserve whitespace |
| Form/KV region | OCR Provider | `"key_value"` | `{ "type": "KeyValueBlock", "pairs": dict[str, str] }` | Preserve original keys |
| Figure region | OCR Provider | `"figure"` | `{ "type": "FigureBlock", "caption": str \| null, "alt_text": str, "embedded_text": list[str] \| null }` | Optional OCR grounding |
| Formula crop | FormulaExtractor | N/A | `{ "type": "FormulaBlock", "latex": str, "mathml": str \| null, "inline": bool, "provider": "unimernet" \| "ocr-fallback" }` | Canonical symbolic form |
| Full scanned page | OCR Provider | `"full_page"` | `{ "type": "PageLayout", "blocks": list[StructuredBlockPayload], "reading_order": list[str] }` | Layout-aware extraction |
| Structured text section | Native parser | N/A | `{ "type": "TextBlock", "text": str, "heading_level": int \| null }` | No OCR |
| CSV row batch | Native parser | N/A | `{ "type": "TableBlock", "headers": list[str], "rows": list[dict[str, Any]], "schema": dict }` | Schema included |
| XLSX sheet | Native parser | N/A | `{ "type": "TableBlock", "sheet_name": str, "headers": list[str], "rows": list[dict[str, Any]] }` | Workbook provenance |

---

# 13. Chunk Serialization Layer

Chunk serialization is explicit and strategy-based.

## 13.1 Serializer Interface

```python
class ChunkSerializer(Protocol):
    def serialize_for_embedding(...): ...
    def serialize_for_agent(...): ...
    def serialize_for_display(...): ...
```

## 13.2 Separation of Concerns

| Field | Purpose |
|---|---|
| content | embedding-oriented serialization |
| content_structured | structured machine payload |
| metadata | provenance/filtering |

## 13.3 Embedding Rules

| Block Type | Embedding Strategy |
|---|---|
| TextBlock | raw prose + section context |
| TableBlock | caption + header-aware row serialization |
| FormulaBlock | latex + nearby textual context |
| CodeBlock | language-tagged serialization |
| FigureBlock | caption + OCR text |

---

# 14. Storage Architecture

## 14.1 Storage Layers

| Layer | Technology | Purpose |
|---|---|---|
| Vector Store | ChromaDB | Semantic retrieval |
| Metadata Store | SQLite/Postgres | CIF persistence |
| Artifact Store | Filesystem/Object store | Images/rendered pages |
| State Store | SQLite | Checkpointing |

## 14.2 Large-Scale Considerations

For larger deployments:

- SQLite may be replaced with Postgres
- artifacts may move to S3-compatible storage
- retrieval service may become independently deployable

---

# 15. Observability & Telemetry

Observability is mandatory.

## 15.1 Metrics

The system must expose:

- OCR latency
- formula latency
- pages/sec
- malformed JSON count
- retry count
- GPU memory usage
- queue depth
- chunk generation rate
- provider fallback rate

## 15.2 Tracing

OpenTelemetry tracing is recommended.

Each document ingestion receives a trace ID.

## 15.3 Structured Logging

All phases emit structured JSON logs.

---

# 16. Security & Sandboxing

## 16.1 Required Protections

| Threat | Mitigation |
|---|---|
| Malicious PDFs | sandboxed parsing |
| Zip bombs | size limits |
| Embedded scripts | disable execution |
| HTML SSRF | network restrictions |
| Resource exhaustion | timeout enforcement |
| Oversized images | preprocessing caps |

## 16.2 File Validation

- MIME validation required
- extension checks are insufficient
- decompression limits enforced

---

# 17. Implementation Roadmap

## Phase A — Foundation

- project scaffold
- config system
- OCR provider abstraction
- validation layer
- GPU scheduler
- typed CIF primitives

## Phase B — Layout & OCR

- layout subsystem
- OCR region routing
- scanned/digital PDF support
- HTML ingestion
- structured extraction

## Phase C — Formula System

- UniMERNet microservice
- formula routing
- inline equation handling
- symbolic fallback path

## Phase D — Retrieval & Chunking

- serializer strategies
- chunk generation
- embedding/indexing
- retrieval APIs

## Phase E — Hardening

- retries
- resumability
- telemetry
- benchmarking
- concurrency controls
- evaluation corpus

## Phase F — Scaling

- distributed ingestion
- remote artifact storage
- graph retrieval
- multi-document reasoning

---

# 18. Open Questions

| Topic | Notes |
|---|---|
| Inline formula routing | still heuristic-heavy |
| Multi-page layout coherence | future graph-aware extraction |
| Cross-reference resolution | currently best-effort |
| Streaming ingestion | not yet formalized |
| Incremental updates | future delta ingestion system |
| Layout model replacement | abstraction exists but benchmarking needed |
| Retrieval ranking fusion | semantic + structural hybrid ranking |

---

# 19. Final Notes

This version formalizes several previously implicit architectural assumptions:

- layout detection is now a first-class subsystem
- extraction execution is graph-oriented
- OCR output validation is mandatory
- GPU ownership is centrally coordinated
- CIF avoids binary inflation
- structured outputs are machine-contract driven
- fallback semantics preserve provenance
- chunk serialization is strategy-based

This specification should be treated as the baseline for implementation planning and prototype stabilization.

