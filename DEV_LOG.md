# Development Log

This log records the implementation process and engineering decisions behind the current DAG OCR refactor line. It is intentionally more detailed than commit messages: each entry captures context, tradeoffs, validation, bugs found, and follow-up implications.

## Branch Order and Commit Map

Current implementation line:

1. `main`
   - `1ca148a` - `refactor(stage4): correct equations one crop at a time`
2. `DAG-OCR-phase-A`
   - `dffa67f` - `Refactor pipeline toward Phase A foundation`
   - `ba51574` - `Rename legacy pipeline modules to spec-aligned subsystems`
3. `DAG-OCR-phase-B`
   - `363f4d4` - `Refactor package layout for Phase B`
4. `feat/checkpoints`
   - `267425d` - `Add DAG checkpoint observations`
5. `DAG-OCR-phase-c-unimernet`
   - Starts from `feat/checkpoints` at `267425d`.
   - `916d0f8` - `Implement Phase C formula system`
   - `4e5d07c` - `Fix Phase B/C GPU serialization`
   - `1af873f` - `Refactor GPU scheduler to file lock`
   - `1588e34` - `Add OCR retry escalation policy`

## Branch Lineage

- `main` baseline: legacy stage-oriented OCR/correction pipeline.
- `DAG-OCR-phase-A`: Phase A foundation work from the document-intelligence beta spec.
- `DAG-OCR-phase-B`: Phase B layout/OCR and package-structure migration.
- `feat/checkpoints`: checkpoint observation work. The requested branch name `feat:checkpoints` was not used because `:` is invalid in Git ref names.
- `DAG-OCR-phase-c-unimernet`: Phase C formula-system work created from `feat/checkpoints`.

## dffa67f - Refactor pipeline toward Phase A foundation

This commit started the transition from the old Markdown-first, stage-specific OCR pipeline toward the v0.5.0 beta document-intelligence concept. The goal was not to rewrite every feature, but to establish the foundation layer required by the new spec while preserving enough compatibility to keep the existing pipeline runnable.

Key decisions:

- Introduced typed foundation modules under `pipeline/foundation/` for config, CIF primitives, GPU scheduling, task units, validation, and OCR provider abstraction.
- Kept legacy stage modules in place temporarily because immediately deleting them would have made the repo harder to validate. The foundation layer was added beside the existing implementation first.
- Added `CanonicalIntermediateFormat`, `StructuredBlock`, `SourceCoords`, `ProcessingArtifact`, and `ProvenanceRecord` as the internal representation direction. This made Markdown an export target rather than the authoritative processing state.
- Added an `OutputValidator` to centralize JSON parsing/repair rather than letting every provider or stage handle malformed model output differently.
- Added a `GPUScheduler` abstraction to serialize GPU-heavy work. The original architecture implicitly assumed stage order would prevent GPU contention; the new DAG-oriented concept needs explicit ownership.
- Updated checkpointing to become document-scoped and page-scoped so different documents can resume independently.

Important implementation nuance:

- The compatibility CLI still exposed old command names, but the help text and README were shifted toward the new terminology. This was deliberate: changing the user-facing command surface too early would have broken existing workflow tests before the new DAG runner existed.
- `requirements.txt` was cleaned to reflect actual runtime and foundation dependencies, including OCR/provider support and test tooling.
- The initial spec was committed into `docs/` as implementation context, not as an external-only planning document.

Nuance and fixes during development:

- The first branch creation attempt used `DAG-OCR-phase-B` as the base because the requested lowercase branch name did not exist locally and the available Phase B branch was uppercase. After correction, the branch was deleted and recreated from `feat/checkpoints`; the existing `DEV_LOG.md` work was preserved and restored.
- The first formula routing pass handled explicit layout `formula` regions but missed inline formulas in ordinary text. A follow-up audit against the Phase C checklist found that gap, so deterministic inline candidates are now emitted as separate `FormulaBlock`s with provenance lineage to their source block.
- The initial fallback path emitted an empty formula fallback when UniMERNet was unavailable. That was tightened so the crop is sent through OCR and converted into a symbolic `FormulaBlock` when the recognized text looks formula-like; otherwise it remains an explicit `FormulaFallback`.
- UniMERNet loading is deferred until scanned pages actually contain formula regions. This keeps scanned OCR paths from failing just because the formula service is not running for documents without formulas.
- The service import boundary is intentionally strict: UniMERNet imports happen only inside the service runtime, not in the main ingestion process. That preserves the spec requirement for dependency isolation and avoids making ordinary extraction dependent on UniMERNet installation.
- Tests use fake HTTP sessions and fake OCR/formula providers so the contract, routing, and fallback behavior are validated without loading UniMERNet or requiring network/model availability.

Validation performed:

- Added `tests/test_phase_a_foundation.py` to exercise typed settings, CIF conversion, output validation, and GPU scheduler basics.
- Used the project `.venv` for test execution as requested.

Known limitations left by the commit:

- The file layout still carried old stage names such as equations/correction/assemble.
- The checkpoint model was still stage-centric even though it had become document/page scoped.
- Phase B layout routing, scanned/digital PDF branching, and HTML ingestion were not yet implemented.

## ba51574 - Rename legacy pipeline modules to spec-aligned subsystems

This commit was a cleanup step focused on terminology. The old names encoded implementation stages rather than spec concepts, so they made the codebase look more legacy than it actually was.

Key decisions:

- Renamed `pipeline/equations.py` to `pipeline/enrichment.py` because the module was broader than equation extraction; it combined layout classes, crops, and formula-related enrichment.
- Renamed `pipeline/correction.py` to `pipeline/export.py` because the module primarily normalized and exported page content after extraction rather than owning the core correction concept.
- Renamed `pipeline/assemble.py` to `pipeline/serialization.py` to make Markdown/document assembly an output serialization concern.
- Updated CLI imports and compatibility package exports so behavior stayed stable while terminology improved.

Important implementation nuance:

- This was intentionally structural but shallow. It did not introduce wrapper modules or duplicate APIs; imports were moved to the renamed modules.
- The change was kept small to reduce risk before the larger `src/pocket_specialist` migration.

Validation performed:

- Import updates were checked after the rename.
- Existing CLI command semantics were preserved.

Known limitations left by the commit:

- The repo still had a root-level `cli.py`, root-level `config.py`, and `pipeline/` package.
- The layout still did not fully match the revised v0.5.1 reference structure.

## 363f4d4 - Refactor package layout for Phase B

This commit performed the large structural migration from the legacy `pipeline/` package into the upgraded spec-aligned `src/pocket_specialist/` layout. It also incorporated the revised v0.5.1 beta spec and the first Phase B implementation path.

Key decisions:

- Removed unnecessary root wrappers. The root `cli.py`, root `config.py`, and old `pipeline/` package were retired instead of kept as compatibility facades.
- Moved active implementation into explicit subsystems:
  - `core/` for config, CIF, models, GPU scheduling, task contracts, and validation.
  - `handlers/` for intake/classification.
  - `layout/` for layout provider contracts and routing helpers.
  - `ocr/` for OCR provider contracts and implementations.
  - `phases/` for Phase B structured extraction orchestration.
  - `storage/` for checkpoint persistence.
  - `serializers/` for Markdown/final output serialization.
  - `compat/` for old-stage behavior that is still needed but no longer authoritative.
- Added empty subsystem packages for future formula, retrieval, and utility work so the tree matches the upgraded reference without pretending those systems are implemented.
- Changed the console entry point to `pocket_specialist.cli:main` instead of pointing directly at the Typer object. This keeps installation behavior conventional without adding a root wrapper.
- Kept compatibility commands in the CLI but moved their imports to direct subsystem paths rather than importing through a package-root facade.
- Made `src/pocket_specialist/__init__.py` minimal to avoid eager provider imports and avoid becoming another wrapper layer.

Phase B implementation work included:

- Document classification for HTML, text, Markdown, scanned PDFs, digital PDFs, and hybrid PDFs.
- Native text extraction for digital PDF pages.
- Render-to-bytes support for PDF pages so layout and OCR can work without forcing all intermediate images through public CLI commands.
- Layout provider abstraction and default provider routing.
- OCR provider routing with primary/fallback providers.
- Structured extraction output for HTML and PDF inputs.
- Page-level layout JSON and structured JSON artifacts.

Bugs and cleanup found during development:

- Old tests referenced stale modules and path hacks. Those tests were removed when they no longer represented the architecture being tested.
- `pytest` was present in `requirements.txt` but missing from `.venv`; it was installed into `.venv` before running the full test suite.
- Several import scans were run to confirm there were no remaining active imports from `pipeline`, root `config`, or package-root facades.
- The CLI smoke test verified Typer still exposed the expected commands after moving the CLI into the package.

Validation performed:

- `PYTHONPATH=src python3 -m compileall src tests`
- `PYTHONPATH=src ./.venv/bin/python -m pocket_specialist.cli --help`
- `PYTHONPATH=src ./.venv/bin/python -m pytest -q`
- Final result before commit: `12 passed`.

Repository hygiene decisions:

- `.gitignore` was updated to ignore generated `data/` output and `RAG-corpus/*.pdf` documents.
- The user explicitly required that documents in `RAG-corpus` never be committed. Status checks confirmed the PDF corpus file remained ignored and untracked.
- The revised v0.5.1 spec was committed because it became the authoritative implementation reference for this branch.

Known limitations left by the commit:

- The checkpoint store still mirrored old stage concepts and did not yet model DAG node observations.
- Formula extraction and retrieval subsystems were scaffolded but not implemented.
- Region-guided OCR routing existed as a baseline, not as a production-hardened DAG runner.

## Phase C formula-system commit - UniMERNet service and symbolic routing

This commit was developed on `DAG-OCR-phase-c-unimernet`. The branch was recreated from `feat/checkpoints` after an initial incorrect branch base from `DAG-OCR-phase-B`; the corrected branch head starts at checkpoint commit `267425d`. The implementation target was the v0.5.1 Phase C formula system: UniMERNet microservice, formula routing, inline equation handling, and symbolic fallback.

Implementation decisions:

- Added a formula provider contract under `src/pocket_specialist/formula/` with `FormulaResult`, `FormulaExtractor`, and a `UniMERNetFormulaExtractor` HTTP client.
- Added an isolated UniMERNet microservice module with `/health`, `/load`, `/offload`, `/extract`, and `/extract_batch` JSON endpoints. The service keeps UniMERNet imports out of the main ingestion process.
- Added `pocket-specialist-formula-service` as a console entry point for running the service process.
- Wired scanned-PDF formula layout regions into Phase B structured extraction so formula crops become typed `FormulaBlock` payloads when the service succeeds.
- Added deterministic inline formula routing for native/OCR text using math delimiters and symbolic-density heuristics. Inline formulas are emitted as separate `FormulaBlock` records with lineage back to the source text block.
- Added a symbolic fallback path: when UniMERNet extraction fails, formula crops can be OCRed and converted into `FormulaBlock` content if the text looks symbolic, otherwise the pipeline emits `FormulaFallback`.
- Preserved graceful degradation: if formula extraction fails and `fallback_to_ocr` is enabled, the page can still complete with fallback provenance metadata instead of failing the whole page.
- Added formula timeout configuration through `PIPELINE_FORMULA_TIMEOUT_SECONDS` and `[formula].timeout_seconds`.

Validation performed:

- `PYTHONPATH=src python3 -m compileall src tests`
- `PYTHONPATH=src ./.venv/bin/python -m pytest tests/test_phase_c_formula.py -q`
- `PYTHONPATH=src ./.venv/bin/python -m pytest -q`
- Current result: `20 passed`.

Follow-up fix after spec audit:

- A later audit against sections 4 through 19 of the v0.5.1 spec found that digital and hybrid PDF pages were still incorrectly treated as fully complete as soon as native text was present. That meant uncovered layout regions such as image-only formulas on otherwise digital pages never reached OCR or UniMERNet.
- The structured extraction path was adjusted so native text blocks only satisfy the layout regions they actually cover. Unmatched layout regions are now routed through the OCR/formula path and merged back into the page result. This keeps digital/hybrid pages aligned with the spec rule that complex regions can still trigger OCR even when native text exists.
- The first implementation pass exposed an edge case in cleanup: the orchestration code allowed `fallback_provider` to be `None` during extraction, but the final offload logic still assumed an object existed. That produced an `AttributeError` in the new orchestration test. The offload/load guards were tightened so `None` fallback providers are handled consistently.
- Test coverage was extended beyond unit routing helpers. The new integration-style test patches document classification, layout detection, native block extraction, and provider factories to prove that a digital page with native text plus an uncovered formula region now emits both the native `TextBlock` and the routed `FormulaBlock`.

Validation performed for the follow-up fix:

- `python3 -m compileall src tests`
- `PYTHONPATH=src ./.venv/bin/python -m pytest tests/test_phase_c_formula.py -q`
- `PYTHONPATH=src ./.venv/bin/python -m pytest -q`
- Follow-up result: `22 passed`.

Missing detail now documented after a later spec audit:

- Section 9 GPU serialization was still not actually enforced in the active Phase B/C extraction path even though `GPUScheduler` existed. The code was releasing GPU memory after provider use, but the live layout, OCR, and formula paths were not consistently claiming the scheduler at model load/inference/offload boundaries.
- The corrective follow-up wired scheduler claims into the Phase B/C orchestration path and the concrete GPU-heavy providers/runtime: layout detection, OCR inference, UniMERNet service client requests, and the isolated UniMERNet runtime. This keeps CPU work outside the lock while serializing the GPU-heavy sections the spec actually cares about.
- Regression coverage was extended to assert scheduler claims on the active extraction path and provider-level inference boundaries, in addition to the existing digital-page formula routing coverage.
- Validation for this GPU-serialization follow-up used the project virtualenv test run: `.venv/bin/python3 -m pytest tests/test_phase_c_formula.py tests/test_phase_b_foundation.py tests/test_phase_a_foundation.py`, with `22 passed`.

Cross-process scheduler refactor after the enforcement fix:

- The earlier enforcement fix made the active Phase B/C paths call `gpu_scheduler.claim()` consistently, but the scheduler implementation itself still used only in-process `threading.Lock` and `asyncio.Lock` state. That meant separate local processes such as the main extractor and the UniMERNet service could still overlap on GPU use.
- The scheduler was refactored to keep the existing `claim()` and `acquire()` API while replacing the internal lock primitive with an OS-backed `fcntl.flock` lock on a shared file. The default path is `/tmp/pocket-specialist-gpu.lock`, with `PIPELINE_GPU_LOCK_PATH` available for override.
- The async path now acquires and releases the same file lock through `asyncio.to_thread(...)` so event-loop callers preserve the old API without blocking the loop. Timeout behavior remains driven by `runtime.gpu_serialization_timeout_s`, implemented as non-blocking lock attempts plus short sleep polling until the deadline expires.
- Test coverage in `tests/test_phase_a_foundation.py` was extended to verify successful repeated sync claims, timeout while another holder owns the shared lock file, and async acquisition through the file-lock path.
- Validation for the scheduler-primitive refactor used `.venv/bin/python3 -m pytest tests/test_phase_a_foundation.py` and `.venv/bin/python3 -m pytest tests/test_phase_a_foundation.py tests/test_phase_b_foundation.py tests/test_phase_c_formula.py`, with `24 passed`.

Section 7.3 retry and escalation follow-up:

- Output validation already existed, but the active OCR path still stopped short of the section 7.3 policy: malformed JSON could be repaired once, and provider fallback existed at the extraction layer, but there was no constrained-prompt retry for structured OCR failures and no batch-size reduction strategy for timeout or OOM conditions.
- `OllamaOCRProvider.extract()` was extended to retry once with a stricter JSON-only prompt when the first response fails structured validation. The successful retry records retry metadata so downstream provenance can distinguish the escalated path from the first-pass success case.
- `SuryaOCRProvider.extract_batch()` was expanded from a simple per-unit loop into batched execution with recursive recovery. When a batched OCR call fails due to timeout or OOM-style errors, the batch is split into smaller chunks until the work succeeds or reaches a single unit that still fails. This covers both the smaller-batch timeout recovery and OOM-driven batch reduction missing from section 7.3.
- The extraction orchestration API did not need to change for this follow-up; the missing behavior belonged in the OCR provider layer, which is where structured-output retries and batch-size recovery are now enforced.
- Added `tests/test_ocr_retry_policy.py` to cover constrained-prompt retry after malformed JSON, timeout-triggered batch splitting, and OOM-triggered batch splitting.
- Validation for this follow-up used `.venv/bin/python3 -m pytest tests/test_ocr_retry_policy.py` and `.venv/bin/python3 -m pytest tests/test_phase_a_foundation.py tests/test_phase_b_foundation.py tests/test_phase_c_formula.py tests/test_ocr_retry_policy.py`, with `27 passed`.

Section 18 document-handler matrix follow-up:

- The intake layer still only recognized PDF, HTML, text, and Markdown, which left the pre-Phase-D handler matrix incomplete even though the spec expected broader document coverage before retrieval work. Unsupported inputs were still rejected up front in `handlers/intake.py`, so the missing formats never reached any fallback path.
- `SourceKind` and `classify_document()` were expanded to cover full-image OCR inputs (`PNG`, `JPG`, `TIFF`), tabular text formats (`CSV`, `TSV`), office text containers (`DOCX`, `ODT`), spreadsheets (`XLSX`), and `EPUB`.
- Lightweight ingestion helpers were added for the non-PDF formats without introducing new third-party dependencies: CSV/TSV via the stdlib `csv` reader, DOCX/ODT/XLSX/EPUB via ZIP/XML parsing, and EPUB chapter content through the existing HTML block parser.
- The active Phase B extraction entrypoint was extended so these new source kinds are accepted end-to-end. Textual/tabular/archive formats now emit CIF directly, while image formats route through the existing full-page OCR provider path and convert the OCR result into `StructuredBlock`s without forcing a PDF wrapper.
- `tests/test_phase_b_foundation.py` was broadened to cover classification and ingestion for CSV, DOCX, XLSX, EPUB, and image inputs, plus the full-image OCR extraction branch.
- Validation for the handler-matrix follow-up used `.venv/bin/python3 -m pytest tests/test_phase_b_foundation.py` and `.venv/bin/python3 -m pytest tests/test_phase_a_foundation.py tests/test_phase_b_foundation.py tests/test_phase_c_formula.py tests/test_ocr_retry_policy.py`, with `30 passed`.

## Current WIP on feat/checkpoints - DAG checkpoint observation layer

This work is not committed yet. It started after verifying that local `DAG-OCR-phase-B` matched `origin/DAG-OCR-phase-B` at `363f4d4`. A new branch, `feat/checkpoints`, was created from that verified remote head.

Feature proposal:

- Keep SQLite as the state store for local-first operation.
- Preserve existing per-document/per-page stage tables for compatibility.
- Add a DAG observation layer so resume decisions are based on graph node progress, not only linear stage progress.
- Use two tables:
  - `dag_node_state` stores the latest state for `(document, page, node)`.
  - `dag_observations` stores append-only events for node starts, completions, failures, retries, and compatibility stage updates.

Design decisions:

- Existing `set_status(stage, document, page, status, path)` now mirrors stage updates into `dag_node_state`. This avoids forcing all compatibility code to be rewritten immediately.
- New node-level helpers were added for DAG-native code: `record_node_start`, `record_node_done`, `record_node_failed`, `get_node_state`, `should_process_node`, `get_resume_state`, and `get_node_summary`.
- Resume behavior skips `done`, retries `failed` until `MAX_RETRIES`, treats missing state as processable, and computes the next page from the first processable page for a node.
- `pocket-specialist status` now reports both existing stage summaries and DAG node summaries.
- The README and v0.5.1 spec were updated to describe the observation layer as part of the architecture.

Schema/contract correction:

- Initial checkpoint metadata used a new key, `checkpoint_stage`. That was removed because it duplicated existing concepts and created a new metadata convention unnecessarily.
- Observation metadata now follows existing task/unit/provenance naming:
  - `doc_id`
  - `unit_id`
  - `task_id`
  - `task_type`
  - `source_stage`
  - optional artifact metadata such as `artifact_uri`
- Metadata type hints were tightened to `dict[str, object]` to match the existing CIF/task contract style. Loose `Any` usage was removed from checkpoint code.
- Direct `record_node_done(..., path=...)` now adds `artifact_uri` consistently, matching mirrored stage checkpoint behavior.

Bug found during testing:

- `should_process_node()` initially used `MAX_RETRIES` as a default argument. That captured the value at function definition time, so patched or runtime retry settings were not respected during tests.
- The function was changed to accept `max_retries: int | None = None` and read `MAX_RETRIES` at call time when no explicit limit is provided.

Validation performed:

- Added `tests/test_checkpoint_observations.py` for stage-to-DAG mirroring, resume-state selection, retry exhaustion, and metadata contract checks.
- Ran compile and CLI smoke tests after changes:
  - `PYTHONPATH=src python3 -m compileall src tests`
  - `PYTHONPATH=src ./.venv/bin/python -m pocket_specialist.cli --help`
- Ran full test suite through `.venv`:
  - `PYTHONPATH=src ./.venv/bin/python -m pytest -q`
  - Current result: `15 passed`.

Current status:

- Modified files: `README.md`, the v0.5.1 spec, `src/pocket_specialist/cli.py`, and `src/pocket_specialist/storage/checkpoint.py`.
- New file: `tests/test_checkpoint_observations.py`.
- `DEV_LOG.md` is being added as this development history record.
- Ignored/generated files remain untracked, including `RAG-corpus/*.pdf`, `.venv/`, caches, checkpoints, and local `data/`.

## Smoke Test Audit After 267425d - Phase B PDF Readiness

This section documents the PDF readiness smoke-test work performed after the checkpoint observation commit. No source-code change was made during this audit; the goal was to determine whether the Phase B infrastructure can run a PDF completely enough to produce layout and structured outputs.

Scope of the audit:

- Only Phase B PDF execution was evaluated.
- Later spec features such as retrieval, formula extraction service hardening, chunk serialization, and production DAG scheduling were not treated as blockers unless they prevented a Phase B PDF run.
- The checked path was `pocket_specialist.cli extract-structured`, which calls `extract_structured_document()` and should produce layout JSON, per-page structured JSON, aggregate `document.json`, and checkpoint records.

Validation commands and checks:

- Verified the repo was clean on `feat/checkpoints` before running smoke tests.
- Confirmed installed runtime dependencies in `.venv` included `surya-ocr`, `PyMuPDF`, `Pillow`, `torch`, `typer`, and `requests`.
- Confirmed the Surya layout and OCR imports used by the code are importable:
  - `surya.foundation.FoundationPredictor`
  - `surya.layout.LayoutPredictor`
  - `surya.detection.DetectionPredictor`
  - `surya.recognition.RecognitionPredictor`
- Confirmed the configured default providers in `pipeline.toml`:
  - layout provider: `pp-doclayout-v3`
  - OCR primary: `glm-ocr`
  - OCR fallback: `deepseek-ocr`

Digital PDF smoke test:

- Created a temporary one-page digital PDF with native text using PyMuPDF.
- Ran `extract-structured` with isolated temporary output, checkpoint, and SQLite paths.
- Result: passed.
- Produced:
  - `layout/page_0001.json`
  - `structured/page_0001.json`
  - `structured/document.json`
  - `pipeline.db`
- Interpretation: the digital PDF path is runnable for Phase B. It uses layout detection plus native text extraction, so it does not depend on OCR model availability for text content.

Scanned PDF smoke test with default OCR config:

- Created a temporary one-page scanned-style PDF by embedding a raster image containing text.
- Ran `extract-structured` using the default OCR config from `pipeline.toml`.
- Initial result: failed with `HTTPError: 404 Client Error` from Ollama `/api/generate`.
- Cause: the configured production OCR model names, `glm-ocr` and `deepseek-ocr`, were not installed at that moment. The local Ollama model list only contained `qwen2.5vl:3b`, `qwen2.5:7b`, and `qwen2.5vl:7b`.
- Decision: this was recorded as a real production-readiness gap, not something to hide by falling back to Surya. The intended production OCR providers must be present and callable.

Scanned PDF smoke test with Surya OCR override:

- Re-ran the scanned-style PDF test with:
  - `PIPELINE_OCR_PROVIDER=surya`
  - `PIPELINE_OCR_FALLBACK_PROVIDER=surya`
- Result: passed.
- Produced layout and structured outputs.
- Interpretation: the scanned-PDF infrastructure path itself can run when a working OCR provider is configured. This proved that PDF rendering, layout detection, region cropping, OCR invocation, structured output writing, and checkpoint writes are connected. It did not prove production OCR model readiness because Surya is not the configured production provider.

Resume/rerun smoke test:

- Ran `extract-structured` twice against the same completed one-page digital PDF using the same SQLite checkpoint and output directories.
- First run: completed with `1 done, 0 failed` and wrote one block to `document.json`.
- Second run: reported `0 done, 0 failed`, but rewrote aggregate `structured/document.json` with zero blocks while leaving `structured/page_0001.json` in place.
- Bug found: completed pages are skipped by checkpoint state, but aggregate CIF assembly does not reload existing page outputs before rewriting `document.json`.
- Readiness impact: this is a Phase B resume blocker. Resume can avoid reprocessing pages, but a no-op rerun can corrupt the aggregate output by replacing it with an empty document. The fix should either preserve existing aggregate output on a no-op run or rebuild the aggregate CIF from existing per-page structured artifacts.

OCR model smoke test after model installation:

- After `glm-ocr:latest` and `deepseek-ocr:latest` appeared in `ollama list`, re-ran the scanned PDF smoke test using the default production OCR config.
- Result: command completed successfully.
- Ollama showed `glm-ocr:latest` resident on GPU after the run.
- The structured output recorded provider `glm-ocr`, confirming that the primary production OCR path was called.
- Bug found: the extracted text content was not meaningful. The model returned the placeholder text from the prompt contract:
  - `"text": "..."`
- Readiness impact: OCR model availability is no longer the blocker in that environment, but output contract quality is still a blocker for production scanned PDFs. The current `_contract_hint()` examples use placeholders, and the model can satisfy JSON shape requirements by echoing placeholders instead of extracting real text.

Gaps identified that prevent reliable Phase B PDF runs:

- Resume aggregation bug: rerunning a completed document can overwrite `structured/document.json` with an empty CIF because skipped pages are not reloaded from existing per-page outputs.
- Production OCR quality bug: `glm-ocr` can return placeholder content that passes the structural JSON contract but is not useful OCR output.
- Provider readiness depends on actual Ollama model installation. The code can call `glm-ocr` and `deepseek-ocr`, but a fresh environment will fail if those model tags are not pulled first.
- The installed console script `pocket-specialist` was not available on PATH during the audit; running through `PYTHONPATH=src ./.venv/bin/python -m pocket_specialist.cli ...` works. This means editable installation or PATH setup is required before using README-style CLI commands directly.

Decisions from the audit:

- Do not treat Surya override success as proof that production OCR is ready. It is useful infrastructure validation, but production config is `glm-ocr` with `deepseek-ocr` fallback.
- Treat the placeholder OCR output as a contract/prompt validation issue, not merely a model quality issue. The validator currently checks shape, not semantic extraction usefulness.
- Treat the resume aggregation behavior as a real blocker before testing long documents, because a recovery/retry flow must not destroy already generated aggregate output.
- Keep ignored smoke-test artifacts out of the repo. All smoke-test files were written under `/tmp` or ignored local data paths.

Section 12 table-contract follow-up:

- A later audit found that Phase B table routing existed only as scaffolding in the active extraction path. Both native and OCR table regions still emitted `TableBlock` payloads with empty `headers`/`rows` plus a raw `text` field, which was enough to prove routing but not enough to satisfy the section 12 structured output contract.
- The extraction layer now normalizes table content in `phases/extract.py` for both native text and OCR table regions. Native table text is parsed into headers plus object-normalized row dictionaries, and OCR table regions now preserve provider-supplied structured `headers`/`rows` when available while falling back to text-based normalization when the OCR result only provides spans.
- The OCR validator in `ocr/providers.py` was also tightened so top-level table fields such as `headers`, `rows`, and `caption` survive validation instead of being discarded when the provider response is normalized down to its `blocks` list.
- Coverage was extended in `tests/test_phase_c_formula.py` to assert that native table routing emits normalized row objects and that OCR table routing preserves a structured table payload rather than collapsing back to stub content.
- Validation in this environment was limited by missing runtime test dependencies (`pytest`, package-path setup, and `fitz` for the broader suite), so the concrete verification completed here was `python3 -m py_compile src/pocket_specialist/phases/extract.py src/pocket_specialist/ocr/providers.py tests/test_phase_c_formula.py`.

Section 11.4 figure-artifact follow-up:

- A later audit found that figure handling was only partially aligned with the CIF/storage contract. HTML image ingestion already carried `artifact_uri`, but the active PDF/layout-routed figure path only emitted `FigureBlock` content with caption/alt/OCR text and never materialized an external figure artifact or registered one in `CanonicalIntermediateFormat.artifacts`.
- The extraction layer now writes PDF figure crops as external PNG artifacts under the configured artifact root, adds `artifact_uri` to native and OCR-routed `FigureBlock` payloads, and mirrors that URI into provenance metadata for traceability.
- Aggregate CIF assembly now derives `ProcessingArtifact` entries from figure blocks during page collection so the document-level structured output includes both the `FigureBlock` and the externally stored artifact record required by section 11.4.
- Coverage was extended in `tests/test_phase_c_formula.py` at two levels: one test asserts that an OCR-routed figure block receives an `artifact_uri`, and a second integration-style test proves `extract_structured_document()` collects the corresponding figure artifact in `cif.artifacts` for a scanned PDF page.
- Validation in this environment remained dependency-limited, so the concrete verification completed here was `python3 -m py_compile src/pocket_specialist/phases/extract.py tests/test_phase_c_formula.py`.

Section 17 layout-enabled runtime-config follow-up:

- A later audit found that `layout.enabled` was loaded from configuration but not actually honored in the active PDF extraction path. The code always built and loaded a layout provider for PDF documents, which meant section 17 runtime configuration was only partially effective even when the operator explicitly disabled layout.
- The extraction path now gates PDF layout detection on `settings.layout.enabled`. When the flag is `false`, PDF extraction skips layout-provider construction entirely, preserves native-text extraction for digital pages, and falls back to full-page OCR for pages without native text instead of using region-guided layout routing.
- Document-level and per-page structured metadata now record `layout_enabled: false` on that degraded path so downstream consumers can distinguish a no-layout extraction result from the normal layout-guided flow.
- Coverage was extended in `tests/test_phase_c_formula.py` to assert that the layout provider is not called when the flag is disabled for a digital PDF, and that a scanned PDF still routes through full-page OCR in that mode.
- Validation in this environment remained dependency-limited, so the concrete verification completed here was `python3 -m py_compile src/pocket_specialist/phases/extract.py tests/test_phase_c_formula.py`.


Section 12 native-tabular and formula-provider contract follow-up:

- After the broader sections 4 through 12 audit, two smaller contract mismatches were prioritized ahead of the larger PageLayout and DAG gaps because they were narrow, locally testable, and already close to the intended section 12 shapes.
- The first issue was native tabular ingestion. PDF-routed tables had been normalized earlier, but CSV/TSV and XLSX ingestion in `handlers/intake.py` still emitted pre-normalized payloads that mixed structural fields with raw row arrays. That meant the repo could claim table support in two different shapes depending on source type, which is exactly the kind of contract drift the CIF layer is supposed to prevent.
- The implementation decision was to normalize native tabular inputs at the same intake boundary where the table blocks are created rather than trying to repair them later in extraction or chunking. This keeps `TableBlock` shape stable before any later phase reads CIF output.
- The shared `_table_block()` helper in `handlers/intake.py` was therefore tightened to emit object-normalized `rows` consistently. CSV/TSV ingestion now adds the section 12 `schema` payload, and XLSX ingestion now includes `sheet_name` while preserving the same normalized row-object shape.
- The second issue was formula fallback naming. The code had evolved a local `symbolic-fallback` provider label for heuristic OCR-based symbolic recovery, but the spec only allows `unimernet` or `ocr-fallback` for `FormulaBlock.provider` and treats non-recovered cases as `FormulaFallback`.
- The decision there was not to introduce another compatibility alias or broaden the spec locally. Instead, the fallback provider string was collapsed down to `ocr-fallback` so all OCR-derived formula recovery paths share one contract-visible provider label.
- Coverage was updated in two places because these were source-type contract fixes, not just internal helpers. `tests/test_phase_b_foundation.py` now asserts normalized row-object output plus `schema`/`sheet_name` for CSV and XLSX intake, and `tests/test_phase_c_formula.py` now asserts that symbolic formula fallback reports `provider == "ocr-fallback"`.
- Verification remained environment-limited, so the concrete check run for this change set was `python3 -m py_compile src/pocket_specialist/handlers/intake.py src/pocket_specialist/phases/extract.py tests/test_phase_b_foundation.py tests/test_phase_c_formula.py`.

Section 12 full-page PageLayout follow-up:

- The next checklist item was the larger full-page OCR contract gap. The audit had found that degraded `PAGE_STRUCTURED` routing existed operationally, but the persisted structured output still flattened that result directly into CIF `TextBlock`s. That was enough to keep downstream text consumers working, but it was not enough to satisfy section 12's explicit `PageLayout` page contract for full scanned pages.
- The key design decision was to separate the persisted per-page contract from the aggregate CIF representation instead of forcing one format to serve both roles. CIF still needs flattened `StructuredBlock` entries for downstream chunking and retrieval, but the per-page artifact for degraded full-page OCR now needs to preserve its identity as a page-level `PageLayout` result with ordered child blocks.
- Based on that decision, the extraction layer gained a dedicated page writer for `{"type": "PageLayout", "blocks": [...], "reading_order": [...]}` while leaving `document.json` as aggregate CIF. This avoided a more invasive refactor of downstream consumers while still bringing the page artifact into line with the spec.
- Full-page OCR normalization in `phases/extract.py` was also tightened while making that change. The old helper hard-coded every full-page OCR span to `TextBlock`; the new path preserves OCR `block_type` when present, maps it through the same routing helpers used elsewhere, and records that the block originated from page-structured OCR in provenance metadata.
- Another decision was to keep native no-layout PDF pages on the existing simpler structured-page writer. Only the degraded full-page OCR paths now emit top-level `PageLayout` page JSON. That keeps the change narrowly targeted at the exact contract gap identified by the audit instead of silently changing every layout-disabled page artifact in one pass.
- Image sources were updated alongside scanned PDFs because they use the same degraded full-page OCR mode in practice. Treating images and scanned PDF pages differently would have recreated the same contract divergence under a different source-kind branch.
- Test coverage was added at both entry points that exercise degraded full-page OCR. `tests/test_phase_b_foundation.py` now checks that image extraction writes `page_0001.json` as `PageLayout` with a `reading_order` list, and `tests/test_phase_c_formula.py` now checks the same thing for layout-disabled scanned PDF fallback.
- Developing that test coverage exposed a secondary risk: broad scripted replacements can easily bleed assertions into adjacent tests when similar fixture code is repeated. The digital-PDF layout-disabled test was repaired afterward to keep its original native-text expectation while the scanned-PDF test alone carries the new `PageLayout` assertions.
- Verification again used syntax checks because the current environment still lacks the broader runtime test stack. The concrete check run for this change set was `python3 -m py_compile src/pocket_specialist/phases/extract.py tests/test_phase_b_foundation.py tests/test_phase_c_formula.py`.


Section 4 DAG-executor follow-up:

- The remaining Phase A gap after the structured-output work was that the repo had task types and checkpoint-backed node observations, but no actual executor driving those abstractions. The active runtime still processed pages in handwritten sequential loops, so the graph model existed as data and observability scaffolding rather than as the orchestration mechanism described in section 4.
- The first implementation decision was to add a generic executor in `core/` rather than embedding one more page-loop helper inside `phases/extract.py`. The problem was architectural, not phase-local: retries, dependency scheduling, checkpoint-aware skipping, and resource gating need one shared implementation if they are going to be credible DAG behavior instead of another specialized code path.
- The new `DAGExecutor` was built around the existing `ExtractionTask` and `ProcessingUnit` contracts rather than replacing them. That let the change use the repo's existing checkpoint semantics directly: `record_node_start()`, `record_node_done()`, `record_node_failed()`, `get_node_state()`, and `should_process_node()` now actively participate in execution rather than only serving reporting and resume inspection.
- A deliberate decision was made to keep the executor thread-based and CPU-concurrent while still allowing explicit resource serialization. Section 4 calls for concurrent CPU execution and serialized GPU execution, so the executor now uses a thread pool for runnable tasks plus task-declared resource semaphores for categories like `layout`, `ocr`, and `formula`.
- Another key decision was not to try to parallelize provider method calls blindly. The pipeline already serializes GPU access through `gpu_scheduler`, and many provider clients are stateful. For the extraction integration, provider loading and inference calls were therefore wrapped with narrow locks around shared OCR, layout, and formula clients. That keeps the executor honest about DAG scheduling without pretending every underlying provider is safe for unrestricted concurrent calls.
- The executor was then integrated into the active PDF flows in `phases/extract.py` at three points instead of only being added as an unused primitive. Layout-disabled PDF structured extraction now runs as per-page DAG tasks; layout-guided structured extraction now models `layout -> structured` dependencies explicitly; and standalone layout detection now also runs through the same page-task executor path.
- This integration choice matters because it is what turns the graph from theory into runtime behavior. The existing extraction code still performs the same document work, but now the work is scheduled as task nodes with dependency edges, checkpoint-aware skipping, retry behavior, and resource declarations rather than as hand-authored for-loops with implicit sequencing.
- The document/image full-page OCR branch was intentionally left as a direct path for now. The main section 4 gap was on multi-page orchestration, resume, and dependency scheduling; forcing every one-page source through the executor immediately would have added more surface area without improving the specific architectural deficiency identified by the audit.
- Test coverage was added at the Phase A layer rather than only relying on indirect extraction tests. The new tests prove three distinct behaviors: a transiently failing task is retried and then unblocks its dependent task, a checkpointed completed dependency is skipped while its dependent still runs, and CPU tasks can overlap while `layout` tasks remain serialized by resource limits.
- While wiring these tests, one subtle runtime issue became clearer: it is easy to claim DAG scheduling while still effectively running a linear pipeline if every task shares one global resource gate. The direct resource-serialization test was added specifically to guard against that regression by proving CPU tasks can overlap while layout tasks do not.
- Verification in this environment again used syntax checks because the broader runtime test stack is incomplete here. The concrete check run for this change set was `python3 -m py_compile src/pocket_specialist/core/dag.py src/pocket_specialist/phases/extract.py tests/test_phase_a_foundation.py tests/test_phase_b_foundation.py tests/test_phase_c_formula.py`.
