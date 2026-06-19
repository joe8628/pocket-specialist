# GLOSSARY.md — Canonical Terms

> One canonical name per concept. When you're about to name a new thing, check
> here first; add the term here the moment it's coined. Naming drift across
> sessions ("chunk" vs "segment" vs "passage") quietly fractures a codebase.
> If a term needs more than one line, give it a `wiki/CON-XXXX.md` concept page
> and link it from its row here.

| Term | Canonical meaning | Not to be confused with |
|------|-------------------|-------------------------|
| CIF | **Canonical Intermediate Format** — the typed, provenance-bearing schema that is the only state passed between stages (DEC-0001, `core/cif.py`). | Crystallographic Information File; a markdown render of the document. |
| Block | A typed layout/semantic element in CIF (`StructuredBlock`) with source coordinates and provenance, produced during extraction. | a Chunk (post-serialization); a layout Region; a Unit. |
| Region | A layout-detector output (`LayoutRegion`): bbox + class + confidence + reading order, before content extraction. | a Block (extraction output); a `RegionUnit` (the work item carrying a region's crop). |
| Unit | A `ProcessingUnit` work item handed to a DAG task (`PageUnit`, `RegionUnit`, `FormulaUnit`, `TextSectionUnit`, `RowBatchUnit`). | a Region (a detection) or a Block (CIF output) — a `RegionUnit` wraps a Region for processing. |
| Chunk | The retrieval unit produced by serializing Blocks for embedding/indexing (Phase D, [[CON-0001]]). | a Block (pre-chunking); a Region; a Unit. Do not call it a "segment" or "passage". |
| Formula | **Canonical** name for mathematical content: layout `formula` region, `FormulaBlock`, `FormulaFallback`, `[formula]` config. | "equation" — the **legacy** spelling (`BlockType.EQUATION`/`EQUATION_FAILED`, compat `equations` command/dir). Layout maps `equation→formula`; use **formula** in new code. |
| doc_id | **Canonical** document identifier carried on CIF, Units, and checkpoints. | `doc_slug` / `document_slug` / `slug` / `<doc-slug>` — path-name spellings of the same id. Use **doc_id** in code; reserve `*_slug` for filesystem path segments. |
| Provider | The **in-process** client adapter behind a protocol (`LayoutProvider`, `OCRProvider`), selected by a `build_*` factory. | the model itself; a **Service** (the HTTP server); a **Runtime** (the model wrapper inside a service). For the formula stage the same role is named **Extractor** (`FormulaExtractor`). |
| Service | An **out-of-process** HTTP server hosting a heavy provider (`layout/service.py`, `layout/paddle_service.py`, `formula/service.py`), CON-0002. | a Provider — e.g. `SuryaLayoutServiceProvider` is the repo-side Provider *client* that calls the Surya Service. |
| Runtime | The model-lifecycle wrapper **inside** a Service (`SuryaLayoutRuntime`, `UniMERNetRuntime`) owning load/detect/`stop`. | a Provider (repo-side client) or a Service (the HTTP process). |
| Phase | A roadmap milestone (A–F) from the v0.5.1 spec; coarse delivery boundary. | a Stage; a DAG Node/Task. |
| Stage | The **legacy** pipeline axis: compat steps (render, ocr, equations, correct, assemble) and the stage tables / `source_stage`, mirrored into DAG node state for compatibility (DEC-0010). | a DAG **Node/Task**; a **Phase**. |
| Node / Task | The DAG execution identity. A **Task** is a schedulable `ExtractionTask`; a **Node** is its checkpoint identity `(document, page, node)` where `node == task.task_id` (`core/dag.py`, `dag_node_state`). | a legacy **Stage**; a **Phase**. |
| Structured path | The active `extract-structured` flow targeting CIF. | the legacy `compat/` render→ocr→correct→assemble markdown flow. |
| Offload | The provider/protocol teardown method `offload()` — drop the model and free its GPU claim. | `release_memory()` (only `torch.cuda.empty_cache()`); `stop`/`close`/`shutdown` (tearing down a backend process or vLLM container inside a Runtime). |
| Fallback | **Overloaded — always qualify.** (1) *OCR fallback provider* (`fallback_provider`, the secondary OCR model); (2) *page-level OCR fallback* (`page_fallback_region_threshold` — whole-page instead of per-region); (3) *`FormulaFallback`* (CIF payload when formula extraction fails); (4) *`ocr-fallback`* (a `FormulaBlock.provider` label, RUL-0003). | a bare "fallback" — never write it unqualified. |
| bbox | **Canonical** pixel box as a 4-tuple `(x0, y0, x1, y1)` in rendered-image space (RUL-0005). | a raw provider `box` dict; the compat `BoundingBox` dataclass; a **Polygon**. |
| Polygon | An ordered point list (≥3 points) — a richer region outline than a bbox, preferred for overlap matching when present (RUL-0005). | a bbox — **not interchangeable**; a Polygon is not "just another bbox". |
| Dev checkpoint | A `_dev_checkpoints/*.md` debug render written during ingestion; temporary, slated for removal before release. | a real checkpoint (the SQLite `dag_node_state` / stage tables used for resume). |

---

## Naming conventions

- Pipeline stages are verbs (render, classify, layout, ocr, formula, extract).
- Data shapes are nouns in PascalCase; CIF types live in `core/cif.py` / `core/models.py`.
- Config keys are snake_case, namespaced by subsystem (`[layout]`, `[ocr]`, `[formula]`, `[storage]`, `[batch]`).
- Environment overrides are `PIPELINE_<SECTION>_<KEY>` (e.g. `PIPELINE_LAYOUT_PROVIDER`).
- Providers are named `<model-or-vendor>-<role>` (e.g. `pp-doclayout-v3`, `glm-ocr`, `deepseek-ocr`).
- Prefer **formula** over "equation" in new code; `equation` survives only in the legacy `BlockType` enum and the compat `equations` flow.
- Use **doc_id** for the identifier; only filesystem path segments may spell it `<doc-slug>`.
- Never write a bare "fallback" — name which of the four (OCR provider / page-level OCR / `FormulaFallback` / `ocr-fallback` label).
- **Provider** = in-process client, **Service** = HTTP server, **Runtime** = model wrapper inside the service; don't use them interchangeably.
- **offload** is the contract teardown; `release_memory` only empties the CUDA cache; `stop`/`close` tear down a backend process — keep them distinct.
