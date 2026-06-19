# SPEC.md — Source of Truth for Intent

> The **what** and **why**. When code and this document disagree, this document
> wins (or this document is wrong and must be updated first, explicitly).
> Update via a normal edit; significant changes should also add a `wiki/DEC-XXXX.md` entry.
>
> **Read by section, not whole.** Jump to the heading you need:
> Problem · Purpose · Consumers · Functional Requirements · Non-Functional
> Requirements · Success Criteria · Non-Goals · Open Questions.
>
> Architecture baseline: `docs/document_intelligence_pipeline_spec_v_0_5_1_beta_complete.md`
> (v0.5.1-beta). This SPEC is that contract reconciled with the **current
> implementation**. Each requirement carries a status:
> **[done]** implemented · **[partial]** partially implemented · **[todo]** not yet built.

**Last reviewed:** 2026-06-19

---

## Problem

Heterogeneous technical documents (digital and scanned PDF, HTML, DOCX/ODT,
XLSX, CSV/TSV, EPUB, images, TXT/Markdown) lock their content inside layout,
tables, figures, and mathematical formulas. Downstream uses — retrieval, agents,
a personal queryable expert — need typed, provenance-bearing structure, not
flattened text. The common workarounds fail in known, named ways: OCR-to-markdown
loses coordinates and structure ("markdown-as-structure"); single-model
"extract text" couples the system to one model's quirks ("model-centric
coupling"); naive pipelines leave GPU models resident and OOM on consumer
hardware ("unmanaged GPU residency"); happy-path pipelines have no failure
semantics. Done by hand, re-deriving clean structure per document is slow and
unrepeatable.

## Purpose & Outcome

Point the system at a local corpus and get, per document, a typed **Canonical
Intermediate Format (CIF)** record: ordered, classified blocks (headings,
paragraphs, tables, figures, formulas, code, key-values) each carrying source
coordinates and provenance, produced resumably and within one constrained local
GPU (reference: RTX 2080 Ti, 11 GB). CIF is the durable substrate; the eventual
product is a **"pocket specialist"** — retrieval-ready chunks and grounded,
citation-bearing answers over your own library. Providers, prompts, and render
formats may change; CIF, provenance, GPU lifecycle, and the retrieval contract
are the durable boundaries.

## Consumers / Interfaces

- **CLI** (`pocket_specialist.cli`, console script `pocket-specialist`) — primary
  entry point. Active structured path: `extract-structured`,
  `extract-structured-surya`, `extract-structured-pp-doclayout-v3`,
  `extract-structured-corpus`, `layout`, `layout-surya`,
  `layout-pp-doclayout-v3`, `compare-layout-page`, `status`, `reset`,
  `serve-surya-layout`, `serve-paddleocr-layout`, `rename-corpus`. Legacy compat:
  `render`, `ocr`, `equations`, `correct`, `assemble`, `run`, `run-all`.
- **Library import** (`pocket_specialist.*`) — phases, providers, CIF types.
- **Isolated HTTP services** (CON-0002, DEC-0005) — Surya v2 layout, PaddleOCR
  layout, UniMERNet formula; reached over `PIPELINE_*_BASE_URL`. Uniform
  `/load`, `/detect`|`/extract`, `/offload`, `/health` contract.
- **Downstream (intended, [todo])** — a retrieval/agent layer consuming CIF +
  chunks. Not yet a consumer; it is the reason the substrate exists.

---

## Functional Requirements

> Numbered so decisions and code can reference them. Each maps to a section of the
> v0.5.1 architecture spec and to the code that implements it.

- **FR-1 — Intake & classification (spec §18). [done]** Classify and ingest PDF
  (digital/scanned/hybrid), HTML, TXT/Markdown, CSV/TSV, DOCX/ODT, XLSX, EPUB,
  and images (PNG/JPG/TIFF). MIME-based detection, not extension-only.
  *Code:* `handlers/intake.py` (`SourceKind`, `classify_document`,
  `detect_mime_type`, native ingest helpers).

- **FR-2 — Layout detection subsystem (spec §5). [done]** First-class layout
  layer behind a `LayoutProvider` protocol producing regions with bbox,
  confidence, reading order, and (extension) polygons. Default in-process
  provider is PP-DocLayoutV3 via Transformers; Surya v2 and PaddleOCR run as
  isolated services. Layout owns region classification, reading order, and
  OCR/table/formula routing scope.
  *Code:* `layout/providers.py`, `layout/service.py`, `layout/paddle_service.py`,
  `layout/compare.py`. *See:* DEC-0006, DEC-0005, RUL-0005, DEC-0013.

- **FR-3 — OCR provider abstraction (spec §6). [done]** Interchangeable OCR
  providers behind a shared protocol; orchestration never builds prompts (prompt
  ownership lives in the provider). `glm-ocr` primary, `deepseek-ocr` fallback
  (both Ollama-backed), plus `surya`. REGION_GUIDED is the primary mode;
  PAGE_STRUCTURED is a degraded fallback only.
  *Code:* `ocr/providers.py` (`OCR_PROVIDERS`, `build_primary/fallback_ocr_provider`).
  *See:* CON-0003, DEC-0012.

- **FR-4 — Output validation & repair + retry policy (spec §7). [done]** All
  model output passes through validation before entering CIF: strict parse →
  repair → schema validation → constrained-prompt retry → fallback provider.
  Batch-split on timeout/OOM down to a single unit; repair allowed only on the
  retry attempt so first-pass telemetry stays meaningful.
  *Code:* `core/validation.py`, `ocr/providers.py`, `tests/test_ocr_retry_policy.py`.
  *Caveat:* validation checks JSON **shape**, not semantic usefulness — see the
  `glm-ocr` placeholder-echo gap under Success Criteria / DEC-0012.

- **FR-5 — Formula extraction (spec §8). [done]** Mathematical OCR isolated from
  general OCR. UniMERNet runs as an isolated HTTP service; layout `formula`
  regions are crop-only routing hints. Routing via layout detection + inline
  heuristics + OCR symbolic-density. On failure, OCR symbolic recovery
  (`ocr-fallback`) or an explicit `FormulaFallback` preserving lineage.
  *Code:* `formula/providers.py`, `formula/service.py`, `formula/symbolic.py`.
  *See:* DEC-0007, RUL-0003, DEC-0011.

- **FR-6 — GPU lifecycle management (spec §9). [done]** Deterministic load/offload;
  no model resident between stages. GPU-heavy categories serialized through a
  scheduler that uses a cross-process OS file lock so the extractor and isolated
  services cannot overlap. Offload enforced in cleanup paths (incl. exceptions);
  Surya vLLM container torn down on offload.
  *Code:* `core/gpu.py`, `layout/service.py` (offload). *See:* DEC-0004, DEC-0008.

- **FR-7 — DAG execution (spec §4). [done]** Extraction is graph-shaped: a shared
  `DAGExecutor` schedules `ExtractionTask`s with dependency edges, per-resource
  semaphores (CPU concurrent / GPU serialized), retries, checkpoint-aware
  skipping, and artifact-validated resume hooks.
  *Code:* `core/dag.py`, `core/tasks.py`, `phases/extract.py`. *See:* DEC-0009.

- **FR-8 — Canonical Intermediate Format (spec §10–11). [done]** All sources
  converge to CIF before any downstream use. Typed `StructuredBlock`
  (block_type, content, section_path, reading_order, page, source_coords,
  provenance) and a `ProcessingUnit` hierarchy. Figure binaries are stored
  externally and referenced by `artifact_uri`; CIF never inlines binaries.
  *Code:* `core/cif.py`, `core/models.py`.
  *Nuance:* spec §11.3 names block types (`TextBlock`, `TableBlock`, …); these
  are carried as free-string `block_type` values on `StructuredBlock`. The
  `BlockType` enum in `models.py` is a separate legacy set (text/heading/equation/
  …) used by the compat adapter, not the structured-extraction contract. Worth
  unifying — see Open Questions.

- **FR-9 — Structured output contracts (spec §12 routing table). [done]** Each
  unit type routes to a typed payload: `TextBlock`, `TableBlock` (object-normalized
  rows; CSV adds `schema`, XLSX adds `sheet_name`), `CodeBlock`, `KeyValueBlock`,
  `FigureBlock` (external artifact), `FormulaBlock`, and `PageLayout` for full
  scanned pages.
  *Code:* `phases/extract.py`, `handlers/intake.py` (`_table_block`).
  *See:* CON-0003, RUL-0004 (native text only satisfies covered regions).

- **FR-10 — Checkpoint, resume & observation layer (spec §14.3). [done]** SQLite
  state store with legacy per-stage tables mirrored into `dag_node_state`, plus an
  append-only `dag_observations` event log. Resume skips `done`, retries `failed`
  to `MAX_RETRIES`, treats missing as processable. `status` reports both views.
  *Code:* `storage/checkpoint.py`, `tests/test_checkpoint_observations.py`.
  *See:* DEC-0010. *Known bug:* a no-op rerun can rewrite aggregate
  `structured/document.json` with zero blocks (see Success Criteria).

- **FR-11 — Gated corpus batch (operational). [done]** Run structured extraction
  over `RAG-corpus` (or `--corpus-dir`), off by default; enable per run with
  `--enabled`, `PIPELINE_STRUCTURED_CORPUS_ENABLED=1`, or
  `[batch].structured_corpus_enabled`.
  *Code:* `cli.py` (`extract-structured-corpus`), `core/config.py` (`BatchSettings`).

- **FR-12 — Chunk serialization (spec §13). [todo / Phase D].** A `ChunkSerializer`
  emitting embedding/agent/display views of CIF with separated
  `content`/`content_structured`/`metadata`. *Code:* only `serializers/markdown.py`
  exists (export/compat render, DEC-0003); no chunker. This is the first half of
  the unbuilt "specialist."

- **FR-13 — Embedding & indexing (spec §14.1, §14.2). [todo / Phase D].** Default
  `nomic-embed-text` via Ollama into a ChromaDB vector store.
  *Code:* `EmbeddingSettings` and `[storage].chroma_path` exist in config, but
  **no embedder or Chroma client is wired** anywhere in `src/`.

- **FR-14 — Retrieval interfaces (spec §19). [todo / Phase D].** `search_chunks`,
  `get_table`, `get_document_map`, `get_page`, `get_key_values`,
  `render_markdown`, `list_documents`. *Code:* `retrieval/` is an empty package.

- **FR-15 — Observability & telemetry (spec §15). [todo / Phase E].** Metrics
  (OCR/formula latency, pages/sec, malformed-JSON/retry counts, GPU memory,
  fallback rate), OpenTelemetry traces with per-document trace IDs, structured
  JSON logs. *Code:* only the temporary dev instrumentation (`core/progress.py`,
  `[START]/[MODEL]/…` prints, `_dev_checkpoints/`) exists — that is **debt to
  remove**, not this requirement.

- **FR-16 — Security & sandboxing (spec §16). [partial / Phase E].** Required:
  sandboxed parsing, size/zip-bomb limits, disabled embedded-script execution,
  HTML SSRF network restrictions, timeout enforcement, oversized-image caps, MIME
  validation. *Code present:* MIME detection (`handlers/intake.py`) and a render
  DPI cap (`rendering.max_dpi`). *Missing:* sandboxing, decompression/size limits,
  SSRF restrictions, execution disabling.

## Non-Functional Requirements

- **Accuracy / quality (spec §2, §7).** Extraction preserves reading order and
  never silently drops a layout region (RUL-0004); table/figure/formula/code
  regions are typed, not flattened. *Status:* enforced structurally, but there is
  **no quantitative eval corpus yet** (Phase E), and shape-validation ≠ semantic
  validation (DEC-0012).
- **Latency (spec §15).** Per-page and per-region elapsed timing is observable
  from the run log. *Status:* no hard p95 target until a benchmark corpus and
  telemetry exist (Phase E). Reference: warm Surya page-6 layout ~4.4s; UniMERNet/
  vLLM cold starts can exceed client timeouts (~294s observed) — a real
  operational caveat.
- **Cost / VRAM budget (spec §21).** Must stay within one RTX 2080 Ti (11 GB).
  Because GPU-heavy stages are serialized (DEC-0004/0008), peak VRAM is bounded to
  one active model plus overhead (GLM-OCR ~1.5 GB, DeepSeek-OCR ~2.5 GB, UniMERNet
  base ~1.5 GB, nomic-embed ~0.3 GB).
- **Reproducibility (spec §2).** Same input + pinned providers ⇒ same CIF; runs
  are resumable and document-scoped. Requires pinned deps (e.g. `transformers`
  recent enough for PP-DocLayoutV3 — DEC-0006).
- **Provenance (spec §2, §11).** Every CIF artifact carries source coordinates and
  lineage to document/page/region (DEC-0001); fallbacks preserve lineage (RUL-0003).
- **Reliability / resilience (spec §2, §7, §9).** Degrades gracefully under
  subsystem failure: provider unavailability routes to fallback; provider import
  failure raises rather than exits (RUL-0002); failed batches never leave models
  resident.
- **Locality (spec §1).** Single-user, local-first; all inference local (Ollama,
  isolated services). No required network egress beyond local model services.

## Success Criteria (Definition of Done)

- [ ] `extract-structured <doc>` yields a valid typed CIF document for every
  supported input type, with provenance (FR-1, FR-8, FR-9).
- [ ] An interrupted run resumes from checkpoint without recomputation or
  corruption (FR-7, FR-10).
- [ ] **Open bug:** a no-op rerun must not overwrite `structured/document.json`
  with an empty CIF — aggregate must rebuild from existing per-page artifacts
  (FR-10).
- [ ] A full corpus pass completes on the reference GPU without OOM, with
  deterministic offload between subsystems (FR-6).
- [ ] **Open bug:** production OCR (`glm-ocr`) must return real extracted text,
  not echo the prompt placeholder while passing JSON-shape validation (FR-4,
  DEC-0012).
- [ ] No stage reads markdown back as state; markdown is export-only (DEC-0003).
- [ ] Dev instrumentation and `_dev_checkpoints/*.md` are removed before release
  (FR-15 / Known Debt).
- [ ] **Specialist DoD (Phase D, [todo]):** CIF → chunks → index → retrieval
  returns source-cited results over the corpus (FR-12, FR-13, FR-14).

---

## Non-Goals (Anti-Scope)

> Explicitly out of scope. Re-state the tempting ones — these creep in across sessions.

- **No model training / fine-tuning** — providers are consumed, never trained.
- **No distributed / multi-tenant / remote-storage deployment** in v1 — single
  user, local-first. (Postgres/S3/independent retrieval service are spec §14.4
  *future* options, i.e. Phase F, deliberately deferred.)
- **No markdown-as-structure** — the legacy `compat/` markdown flow is not the
  target and must not be extended; it exists for backward compatibility only
  (DEC-0003).
- **Not a general chatbot or agent framework** — it produces the substrate an
  agent consumes; it is not the agent.
- **No new UI** beyond the CLI for now.

## Known Debt (drift to pay down, not features)

- **Temporary dev instrumentation** — `core/progress.py` prints and
  `_dev_checkpoints/*.md` are remove-before-release (README + FR-15). They have
  accreted across sessions; delete, do not entrench behind a flag.
- **Legacy `compat/` pipeline** — parallel markdown render→ocr→equations→correct→
  assemble flow kept for compatibility; slated for removal once `extract-structured`
  fully supersedes it (DEC-0003).
- **Block-type duality** — the `BlockType` enum vs. spec §11.3 free-string payload
  types (FR-8) should be unified to one contract.

## Open Questions

> Unresolved spec-level questions. Resolving one usually produces a `wiki/DEC-XXXX.md` entry.
> The first group is from architecture spec §23; the rest are implementation gaps.

- [ ] **Inline formula routing** is still heuristic-heavy (spec §23, §8.4).
- [ ] **Multi-page layout coherence** — future graph-aware extraction (spec §23).
- [ ] **Cross-reference resolution** — currently best-effort (spec §23).
- [ ] **Streaming ingestion** and **incremental/delta updates** — not formalized
  (spec §23).
- [ ] **Layout model replacement** — abstraction exists; benchmarking PP-DocLayout
  vs Surya vs PaddleOCR on the target corpus is unfinished (spec §23; the
  PaddleOCR `/detect` path was still failing on the dev machine).
- [ ] **Retrieval ranking fusion** — semantic + structural hybrid ranking (spec §23).
- [ ] **Phase D design:** chunk schema and how chunks reference back to CIF blocks
  for citations; embedding model/vector store NFR targets (recall@k) — becomes a
  DEC + CON (FR-12, FR-13, FR-14).
- [ ] **Evaluation corpus + quantitative bar** (Phase E) that turns the
  qualitative accuracy/latency NFRs into pass/fail gates.
- [ ] **Cut criteria:** what condition signals the `compat/` flow and dev
  instrumentation are safe to remove?
- [ ] **Block-type unification:** adopt the spec §11.3 type set as the single
  contract, or formally map the legacy `BlockType` enum onto it?
