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
