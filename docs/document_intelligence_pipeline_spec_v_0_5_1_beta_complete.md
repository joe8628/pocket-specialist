# Document Intelligence Pipeline — Architecture Specification (Beta)

**Version:** 0.5.1-beta  
**Status:** Corrected pre-production architecture baseline  
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

This is an architectural contract, not a feature inventory. The design intentionally avoids common OCR and agentic ingestion failures: model-centric coupling, markdown-as-structure, unmanaged GPU residency, happy-path-only pipeline diagrams, and vague operational boundaries.

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

These principles keep model behavior behind stable contracts. Providers may change, prompts may change, and rendering formats may change, but CIF, provenance, lifecycle rules, and retrieval contracts remain the system's durable boundaries.

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

This separation is deliberate. OCR, layout understanding, formula extraction, chunking, retrieval, and rendering are different responsibilities with different failure modes. Collapsing them into a single "extract text" phase would make retries, validation, and downstream agent use much harder to reason about.

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

This prevents rendering or model output from becoming the source of structural truth. Layout defines regions and reading order; OCR extracts content within those scoped regions.

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

Prompt ownership lives inside each provider implementation. Calling code supplies typed units and region intent, never prompt strings, which prevents provider assumptions from leaking upward into orchestration, validation, chunking, or retrieval.

## 6.1 Default OCR Providers

| Provider | Runtime | Primary Use Case |
|---|---|---|
| GLM-OCR | Ollama / GGUF | Fast local OCR, edge deployments, lower VRAM usage |
| DeepSeek-OCR | Ollama / GGUF | Higher-accuracy extraction, complex layouts, multilingual documents |

Both providers implement the shared `OCRProvider` protocol and emit the same validated structured contracts.

This is a real provider abstraction: different model runtimes may produce different raw responses, but the pipeline only accepts normalized, validated contracts.

The default deployment strategy is:

- GLM-OCR as the primary provider
- DeepSeek-OCR as fallback/escalation provider
- provider routing configured via `pipeline.toml`

All OCR inference is local-first via Ollama.

## 6.2 OCRProvider Interface

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

## 6.3 OCRResult

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

## 6.4 Extraction Modes

Two extraction strategies are supported.

| Mode | Description |
|---|---|
| REGION_GUIDED | Layout detector determines regions ahead of OCR |
| PAGE_STRUCTURED | OCR model reconstructs page structure directly |

`PAGE_STRUCTURED` is a degraded fallback mode and should not be the primary extraction path.

The primary path remains region-guided because OCR is not one problem. Prose OCR, table extraction, code extraction, figure grounding, key-value extraction, and layout reconstruction require different routing and validation semantics.

---

# 7. Output Validation & Repair Layer

All model outputs pass through validation before entering CIF.

This layer is mandatory.

The pipeline assumes model outputs can be malformed, incomplete, or structurally inconsistent. Validation and repair make failure semantics explicit instead of treating ideal model output as an architectural assumption.

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

Mathematical OCR is treated as a specialized subsystem rather than a harder case of generic text extraction. This preserves symbolic fidelity and keeps formula dependencies out of the main OCR runtime.

## 8.1 Default Formula Backend

The default formula extraction backend is UniMERNet.

| Model | Approx Size | Use Case |
|---|---|---|
| unimernet_base | ~1.3 GB | Highest fidelity scientific extraction |
| unimernet_small | ~773 MB | Balanced accuracy/performance |
| unimernet_tiny | ~441 MB | Speed-sensitive deployments |

The formula extraction service runs in an isolated environment with PyTorch and Transformers dependencies separated from the main ingestion runtime.

This isolation protects the main ingestion stack from framework drift and heavyweight dependency coupling. UniMERNet can be upgraded, replaced, or disabled without forcing the OCR, layout, chunking, or retrieval layers to inherit its runtime constraints.

## 8.2 FormulaExtractor Interface

```python
class FormulaExtractor(Protocol):
    def load(self) -> None: ...
    def extract(self, image_bytes: bytes) -> FormulaResult: ...
    def extract_batch(self, crops: list[bytes]) -> list[FormulaResult]: ...
    def offload(self) -> None: ...
```

## 8.3 FormulaResult

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

## 8.4 Formula Routing

Formula regions are identified via:

1. layout detection
2. inline heuristics
3. OCR symbolic density heuristics

Inline equations may trigger re-extraction after OCR.

## 8.5 Formula Fallback

If formula extraction fails:

```json
{
  "type": "FormulaFallback",
  "raw_formula_text": "..."
}
```

This preserves semantic provenance.

Fallbacks degrade fidelity without erasing lineage. Even when symbolic extraction fails, downstream systems can distinguish OCR-derived formula text from canonical formula output.

---

# 9. VRAM Lifecycle Management

GPU ownership is deterministic.

No model may remain resident between stages.

GPU residency is an explicit lifecycle concern, not an incidental runtime side effect. Load/offload ownership, cleanup guarantees, and stage isolation are centralized so memory pressure, fragmentation, and failure cleanup paths are observable and testable.

## 9.1 Core Rule

> Models must be explicitly loaded before processing and explicitly offloaded immediately after processing.

## 9.2 GPU Scheduler

GPU execution is serialized.

```python
class GPUScheduler:
    async def acquire(self, resource_type: str): ...
```

Only one GPU-heavy task category may execute simultaneously.

This favors bounded peak memory and predictable recovery over opportunistic concurrency. Concurrency can still exist around CPU-bound graph work, but GPU-heavy stages have a single ownership boundary.

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

Implementations should enforce offload in cleanup paths, including exceptions. A typical stage should pair acquisition with deterministic release, for example via `try`/`finally`, so failed batches do not leave models resident.

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

All document types converge to CIF before chunking. This makes chunking format-agnostic, retrieval uniform, rendering deterministic, and storage stable across source formats and provider changes.

## 11.1 Core Guarantees

- deterministic structure
- typed semantics
- provenance retention
- rendering independence
- storage portability

CIF captures the abstraction level needed by downstream agents: typed semantic blocks, reading order, section hierarchy, source coordinates, and provenance. It is intentionally richer than raw chunks and less presentation-specific than markdown.

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

This keeps CIF suitable for persistence, indexing, validation, and migration. Heavy artifacts remain addressable through storage references rather than inflating the canonical representation.

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

Chunking is a retrieval serialization layer, not the document's canonical structure. It consumes CIF and emits task-specific views for embedding, agent use, and display without rewriting the source representation.

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

This separation preserves machine-consumable structure alongside embedding-friendly text. Agents can retrieve semantic context without losing the typed payloads, neighboring structure, and provenance required for tool use.

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

## 14.1 Default Embedding Provider

The default embedding provider is:

- `nomic-embed-text` via Ollama

Embedding providers remain replaceable behind a provider abstraction.

## 14.2 Storage Layers

| Layer | Technology | Purpose |
|---|---|---|
| Vector Store | ChromaDB | Semantic retrieval |
| Metadata Store | SQLite/Postgres | CIF persistence |
| Artifact Store | Filesystem/Object store | Images/rendered pages |
| State Store | SQLite | Checkpointing |
| Observation Store | SQLite/Postgres | DAG node state and append-only processing events |

## 14.3 DAG Checkpoint Observation Layer

The checkpoint system records progress at DAG-node granularity, not only legacy stage granularity.

Required tables:

- `dag_node_state`: current state for `(document, page, node)` with status, attempts, artifact path, timestamps, metadata, and error.
- `dag_observations`: append-only event log for node starts, completions, failures, retries, and compatibility stage updates.
- Metadata must reuse the existing task/unit/provenance shape: `doc_id`, `unit_id`, `task_id`, `task_type`, `source_stage`, plus optional artifact metadata such as `artifact_uri`.

Resume behavior:

- skip nodes with `status = done`
- retry nodes with `status = failed` until `MAX_RETRIES`
- treat missing nodes as processable
- compute resume position from the first processable page for a node
- preserve old stage checkpoints by mirroring stage status changes into DAG node state

This enables a document to resume from the last completed page/node in a graph-shaped workflow while retaining an auditable processing history.

Lifecycle state is part of the architecture. The checkpoint layer gives the DAG failure semantics: retries are scoped to nodes, completed work is reusable, and recovery can happen without replaying unrelated extraction, chunking, or indexing work.

## 14.4 Large-Scale Considerations

For larger deployments:

- SQLite may be replaced with Postgres
- artifacts may move to S3-compatible storage
- retrieval service may become independently deployable

---

# 15. Observability & Telemetry

Observability is mandatory.

The pipeline is expected to run under imperfect providers, mixed document quality, and constrained hardware. Metrics, traces, and structured logs are therefore part of the contract, not optional debugging aids.

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

# 17. Runtime Configuration

```toml
[pipeline]
version = "0.5.1-beta"

[ocr]
provider = "glm-ocr"
fallback_provider = "deepseek-ocr"
ollama_base_url = "http://localhost:11434"
timeout_seconds = 60
batch_size = 4

[layout]
provider = "pp-doclayout-v3"
enabled = true

[formula]
enabled = true
base_url = "http://localhost:8001"
model_size = "base"
fallback_to_ocr = true

[embedding]
provider = "nomic-embed-text"
ollama_base_url = "http://localhost:11434"

[gpu]
serialized_execution = true
max_gpu_workers = 1

[chunking]
max_tokens = 512
overlap_tokens = 64

[storage]
sqlite_path = "./data/pipeline.db"
artifact_path = "./data/artifacts"
chroma_path = "./data/chroma"
```

---

# 18. Document Handler Matrix

| Format | Primary Path | OCR Triggered When | Notes |
|---|---|---|---|
| PDF (digital) | Structured | Complex regions/images | Text extraction first |
| PDF (scanned) | OCR | Always | Render at 300 DPI |
| HTML | Structured | Embedded images | DOM-first extraction |
| CSV/TSV | Structured | Never | Schema inference |
| TXT/Markdown | Structured | Never | Direct semantic parsing |
| DOCX/ODT | Structured | Embedded images | XML extraction |
| XLSX | Structured | Never | Sheet-aware TableBlocks |
| PNG/JPG/TIFF | OCR | Always | Full image OCR |
| EPUB | Structured | Embedded images | HTML chapter extraction |

---

# 19. Retrieval Interfaces

```python
def search_chunks(...): ...
def get_table(...): ...
def get_document_map(...): ...
def get_page(...): ...
def get_key_values(...): ...
def render_markdown(...): ...
def list_documents(...): ...
```

Retrieval interfaces are designed for structured agent/tool consumption.

The retrieval surface is optimized for machine consumers, not only human chat answers. It preserves document maps, provenance, section hierarchy, typed structures, and renderable views so agents can inspect, cite, and operate on source-grounded data.

---

# 20. Reference Project Structure

```text
src/
└── pocket_specialist/
    ├── cli.py
    ├── core/
    │   ├── cif.py
    │   ├── config.py
    │   ├── gpu.py
    │   ├── models.py
    │   ├── tasks.py
    │   └── validation.py
    ├── handlers/
    │   └── intake.py
    ├── layout/
    │   └── providers.py
    ├── ocr/
    │   └── providers.py
    ├── phases/
    │   └── extract.py
    ├── storage/
    │   └── checkpoint.py
    ├── serializers/
    │   └── markdown.py
    ├── compat/
    ├── formula/
    ├── retrieval/
    └── utils/

services/
└── formula_extractor/

scripts/
└── ingest.py
```

---

# 21. Hardware Reference Notes

Reference deployment target:

| Hardware | Notes |
|---|---|
| RTX 2080 Ti (11 GB) | Fully supported |
| GLM-OCR Q4 | ~1.5 GB VRAM |
| DeepSeek-OCR Q4 | ~2.5 GB VRAM |
| UniMERNet base | ~1.5 GB VRAM |
| nomic-embed-text | ~0.3 GB VRAM |

Because GPU-heavy stages are serialized, peak VRAM is bounded to a single active model plus runtime overhead.

The reference hardware assumptions reinforce the lifecycle design: the system should remain viable on constrained local GPUs by controlling residency rather than assuming every model can stay loaded.

---

# 22. Implementation Roadmap

## Phase A — Foundation

- project scaffold
- config system
- OCR provider abstraction
- validation layer
- GPU scheduler
- typed CIF primitives

Foundation work establishes the durable contracts first: provider boundaries, validation, GPU ownership, and CIF. This prevents later phases from becoming coupled to temporary model behavior or presentation formats.

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

Hardening focuses on the remaining high-level risks: operational rigor, concurrency semantics, validation guarantees, and scale behavior. These are the natural next problems once the core abstraction boundaries are in place.

## Phase F — Scaling

- distributed ingestion
- remote artifact storage
- graph retrieval
- multi-document reasoning

---

# 23. Open Questions

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

# 24. Final Notes

This version formalizes several previously implicit architectural assumptions:

- layout detection is now a first-class subsystem
- extraction execution is graph-oriented
- OCR output validation is mandatory
- GPU ownership is centrally coordinated
- CIF avoids binary inflation
- structured outputs are machine-contract driven
- fallback semantics preserve provenance
- chunk serialization is strategy-based
- concrete OCR providers are explicitly defined
- operational configuration is standardized

The resulting architecture has operational gravity: it describes how the system should behave under resource limits, provider failures, dependency drift, partial retries, and future migration pressure. The remaining open issues are therefore mostly hardening and scaling concerns rather than missing architectural foundations.

This specification should be treated as the baseline for implementation planning and prototype stabilization.

