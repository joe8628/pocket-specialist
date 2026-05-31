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

## fix/review1 - pp-doclayout-v3 provider correction and dependency pinning

This branch started as a response to a review finding about fictional `pp-doclayout-v3` support. That review finding turned out to be wrong: the model exists in Hugging Face Transformers, but the repo had no working adapter for it and the local environment was on a Transformers version too old to recognize the architecture.

Key decisions:

- Kept the `pp-doclayout-v3` path strictly Transformers-only. No Paddle, PaddleOCR, or other parallel runtime was introduced for this provider path.
- Added a dedicated `PPDocLayoutV3LayoutProvider` in `src/pocket_specialist/layout/providers.py` and kept `SuryaLayoutProvider` as the separate implementation for the Surya path.
- Loaded the model through the documented Transformers object-detection pipeline against `PaddlePaddle/PP-DocLayoutV3_safetensors`.
- Added an explicit minimum-version guard for Transformers so old environments fail with a clear compatibility error instead of a vague auto-config crash.
- Introduced small internal helpers around Transformers loading/pipeline construction so the provider can be tested without mocking the entire third-party package import path.

Dependency correction:

- `requirements.txt` was converted from loose minimum ranges to exact pins for the versions this branch actually expects.
- The important corrective change is `transformers==5.5.4`. The previous local environment had `transformers==4.57.6`, which is within the old loose range but does not recognize `pp_doclayout_v3` at runtime.
- The rest of the runtime/test stack was pinned to the versions currently in use in the project virtualenv to make the environment reproducible.

Validation performed:

- Focused provider tests were added/updated in `tests/test_phase_c_formula.py` to cover:
  - successful `pp-doclayout-v3` provider construction and region normalization through the Transformers pipeline path
  - explicit rejection of stale Transformers versions such as `4.57.6`
- Ran `.venv/bin/python -m pytest tests/test_phase_c_formula.py -q -k "pp_doclayout_v3_provider"` with `2 passed`.
- Upgraded the local virtualenv to the pinned requirements with `.venv/bin/pip install -r requirements.txt`.
- Verified the upgraded environment was on `transformers 5.5.4`.
- Performed a real provider initialization using the branch code from `src/`, confirmed `PPDocLayoutV3LayoutProvider` loaded successfully, then offloaded cleanly.
- Performed a real `detect()` call on a blank image and confirmed the provider executed end to end and returned a `LayoutResult`.

Important nuance:

- The failure mode here was not “the model does not exist.” The actual problem was the mismatch between the codebase expectation and an overly broad Transformers version range.
- The provider implementation now matches the real model path, and the dependency pin ensures the runtime can actually execute it instead of silently selecting an unsupported Transformers release.


## fix/review1 - structured corpus CLI and README refresh

This update added a gated batch entry point for running the active structured extraction pipeline over corpus files without requiring a PDF path per command invocation.

Key decisions:

- Added `pocket-specialist extract-structured-corpus` as a separate command instead of changing `extract-structured`. The single-document command still requires an explicit source path, while the new command scans `RAG-corpus` or a caller-provided `--corpus-dir`.
- Kept the corpus subroutine gated. It requires `--enabled`, `PIPELINE_STRUCTURED_CORPUS_ENABLED=1`, or `[batch].structured_corpus_enabled = true` so accidental batch model runs do not start just because files exist in `RAG-corpus`.
- Reused `extract_structured_document()` directly rather than duplicating extraction logic. Corpus runs therefore write the same document-scoped `layout/` and `structured/` outputs as single-document structured extraction.
- Added `[batch].structured_corpus_enabled = false` to `pipeline.toml` and a matching typed `BatchSettings` config section.
- Refreshed `README.md` to remove stale Phase A/B-only language, remove claims that formula support is unimplemented, document current Phase A-C scope, and clarify the difference between active structured extraction and legacy compatibility commands.

Validation performed:

- `PYTHONPATH=src .venv/bin/python -m pocket_specialist.cli --help` confirmed the new command is exposed.
- `PYTHONPATH=src .venv/bin/python -m pocket_specialist.cli extract-structured-corpus --help` confirmed the command options and gated behavior are visible.
- `PYTHONPATH=src .venv/bin/python -m pocket_specialist.cli extract-structured-corpus` confirmed disabled mode exits cleanly with an enablement message.
- `.venv/bin/python -m py_compile src/pocket_specialist/cli.py src/pocket_specialist/core/config.py` passed.
- `PYTHONPATH=src .venv/bin/python -m pytest tests/test_phase_a_foundation.py::test_pipeline_settings_from_env_uses_project_root -q` passed.

Follow-up implications:

- The active structured corpus flow is intentionally distinct from legacy `run-all`, which still drives the compatibility render/OCR/equations/correction/assemble pipeline.
- Production-scale batch orchestration, queueing, and Phase E hardening remain out of scope for this change.


## fix/review1 - repo-root corpus path correction

This update fixed a path-resolution bug introduced by the structured corpus command work. When running the CLI with `PYTHONPATH=src`, `PipelineSettings.from_env()` derived the default project root from `Path(__file__).parents[2]`, which resolves to the package `src/` directory rather than the repository root. As a result, the default corpus path incorrectly became `src/RAG-corpus`.

Key decisions:

- Replaced the fragile parent-index root calculation with `_default_project_root()`, which walks upward from `core/config.py` until it finds the repository markers `pyproject.toml` and `src/pocket_specialist`.
- Preserved explicit `project_root=` injection for tests and callers that intentionally override the root.
- Added a regression test proving the default root is the repo root and that the default corpus directory resolves to `<repo>/RAG-corpus`.

Validation performed:

- `PYTHONPATH=src .venv/bin/python - <<'PY' ... PipelineSettings.from_env() ... PY` confirmed project root resolves to `/home/jjmr/github-repos/pocket-specialist` and corpus dir resolves to `/home/jjmr/github-repos/pocket-specialist/RAG-corpus`.
- `PYTHONPATH=src .venv/bin/python -m pocket_specialist.cli extract-structured-corpus --enabled --dry-run --limit 1` confirmed the corpus scanner finds the repo-root PDF instead of looking under `src/`.
- `PYTHONPATH=src .venv/bin/python -m pytest tests/test_phase_a_foundation.py::test_pipeline_settings_from_env_uses_project_root tests/test_phase_a_foundation.py::test_pipeline_settings_default_root_is_repo_root -q` passed with `2 passed`.


## fix/review1 - development ingestion checkpoints

This update added temporary development checkpoints for stage-level ingestion debugging. These checkpoints are intentionally filesystem breadcrumbs that should be removed before release rather than converted into a permanent disabled feature flag.

Key decisions:

- Added `src/pocket_specialist/core/dev_checkpoints.py` as the development-only writer for markdown summaries and stage-scoped debug artifacts.
- Wrote checkpoints under `checkpoints/<doc>/_dev_checkpoints/<stage>/` so they are isolated from release checkpoint state and from normal structured outputs.
- Added an `artifacts/` subfolder inside each stage directory. Page renderings, layout JSON snapshots, OCR crops, formula crops, equation crops, structured page JSON, and document CIF snapshots can be copied there for inspection.
- Wired checkpoints into the active `extract-structured` pipeline after intake, render, layout, OCR, formula, and structured stages.
- Wired the same checkpoint writer into compatibility render/OCR/equations/correction stages so legacy debugging artifacts use the same directory convention.

Validation performed:

- `.venv/bin/python -m py_compile src/pocket_specialist/core/dev_checkpoints.py src/pocket_specialist/phases/extract.py src/pocket_specialist/compat/render.py src/pocket_specialist/compat/ocr.py src/pocket_specialist/compat/enrichment.py src/pocket_specialist/compat/export.py` passed.
- `PYTHONPATH=src .venv/bin/python -m pytest tests/test_phase_a_foundation.py tests/test_phase_b_foundation.py -q` passed with `23 passed`.

Follow-up implications:

- These files are intentionally noisy and development-only. Remove `core/dev_checkpoints.py` and the call sites before release instead of adding a runtime off switch.


## fix/review1 - development phase progress and validation output

This update added temporary status output for ingestion debugging. Like the development checkpoint writer, this is intended to be removed before release rather than hidden behind a permanent runtime flag.

Key decisions:

- Added `src/pocket_specialist/core/progress.py` with simple `[START]`, `[VALIDATION]`, `[MODEL]`, `[PROGRESS]`, `[COMPLETED]`, and `[ERROR]` messages.
- Instrumented the active `extract-structured` path so intake, render, layout, OCR, formula, and structured stages report progress and terminal status.
- Instrumented compatibility render/OCR/equations/correction stages with the same status convention so all currently exposed ingestion paths provide visible progress.
- Kept the implementation dependency-free and intentionally simple so the whole layer can be removed cleanly before release.

Validation performed:

- `.venv/bin/python -m py_compile src/pocket_specialist/core/progress.py src/pocket_specialist/core/dev_checkpoints.py src/pocket_specialist/phases/extract.py src/pocket_specialist/compat/render.py src/pocket_specialist/compat/ocr.py src/pocket_specialist/compat/enrichment.py src/pocket_specialist/compat/export.py` passed.
- `PYTHONPATH=src .venv/bin/python -m pytest tests/test_phase_b_foundation.py tests/test_phase_c_formula.py -q` passed with `30 passed, 1 warning`.
- The broader Phase A-C run surfaced an existing GPU file-lock timeout test failure in `test_gpu_scheduler_claim_times_out_when_lock_is_held`; extraction-focused tests are clean.

## fix/review1 - Surya layout service isolation

This update removes the in-process Surya layout adapter as the primary Surya path and replaces it with an isolated HTTP microservice.

Key decisions:

- Replaced the main-runtime `SuryaLayoutProvider` with `SuryaLayoutServiceProvider` in `src/pocket_specialist/layout/providers.py`.
- Added `src/pocket_specialist/layout/service.py` as the isolated Surya layout server with `POST /load`, `POST /detect`, `POST /offload`, and `GET /health`.
- Added a dedicated service dependency file at `services/surya_layout_service/requirements.txt` so Surya can pin its own compatible `transformers` version independently of the main pipeline runtime.
- Added `layout.base_url` to typed config and environment loading, with `http://localhost:8002` as the default service endpoint.
- Added `pocket-specialist serve-surya-layout` so the service can be started through the repo CLI instead of remembering the module path.
- Kept `pp-doclayout-v3` fully in-process and Transformers-backed; only the Surya layout path was isolated.

Validation performed:

- `.venv/bin/python -m py_compile src/pocket_specialist/layout/providers.py src/pocket_specialist/layout/service.py src/pocket_specialist/core/config.py src/pocket_specialist/cli.py src/pocket_specialist/phases/extract.py` passed.
- `PYTHONPATH=src .venv/bin/python - <<'PY' ... build_layout_provider(...) ... PY` confirmed provider alias resolution for `pp-doclayout-v3`, `surya-layout-service`, `surya-layout`, and `surya`.

Important nuance:

- This change isolates Surya dependency compatibility from the main runtime, but it does not by itself prove the Surya layout model quality on your documents. The service wiring is in place; comparative provider evaluation still has to happen against real pages.


## fix/review1 - render DPI cap and phase validation

This update makes render resolution explicit in DPI terms and adds a configurable upper bound for rendered page images.

Key decisions:

- Kept existing zoom-based internals for PyMuPDF rendering but moved the public control surface to a proper DPI path.
- Added `rendering.max_dpi` to typed config and `pipeline.toml` so page rasterization can be capped consistently across CLI runs and pipeline stages.
- Added config helpers to convert between zoom and DPI and to clamp requested render DPI before rasterization.
- Updated `pocket-specialist render` to accept `--dpi`, while preserving `--zoom` as a lower-level override path.
- Updated render-phase validation/checkpoint metadata to record the effective DPI, not just the raw zoom factor.

Validation performed:

- `.venv/bin/python -m py_compile src/pocket_specialist/core/config.py src/pocket_specialist/handlers/intake.py src/pocket_specialist/compat/render.py src/pocket_specialist/cli.py` passed.
- Rendered page 1 of `RAG-corpus/unknown_2018_mpphys-many-particle-simulation-package.pdf` at `192` DPI and confirmed output size `1588 x 2246`, consistent with the source page dimensions.
- Re-ran render with `PIPELINE_RENDER_MAX_DPI=192` and requested `--dpi 300`; validation output confirmed the effective render DPI was clamped to `192`.

Follow-up implication:

- Render checkpoint state is keyed by document slug rather than output directory. Re-render validation on the same document may require a reset or a copied filename if you want a fresh render pass without touching existing checkpoints.


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


Section 4 DAG resume and provider hardening follow-up:

- The adversarial review found two required resume blockers in the first DAG-executor pass. The executor could mark a checkpointed dependency as skipped purely from SQLite state, but it did not rehydrate the artifact that the dependent node needed in memory. In the PDF layout graph that meant a skipped `layout` task could leave `page_state` empty and then let `structured` run into a missing-state crash.
- The second required issue was related but broader: a `done` node was trusted without checking whether the recorded artifact still existed or still matched the active output directory. That made resumes unsafe when output dirs changed, temp artifacts disappeared, or checkpoint state outlived its files.
- The fix added an optional checkpoint resume hook to `DAGExecutor`. The executor now validates recorded artifact paths before skipping a done node, calls the hook so graph-specific code can reload required state, and treats rejected or missing artifacts as stale work that must rerun. A small force-rerun path was needed because the checkpoint layer correctly says a done node should not process again; stale artifact recovery is the exception that has to override that normal answer.
- The PDF extraction graphs now populate expected checkpoint paths in each page unit and use resume hooks to reload layout JSON into `page_state` or structured page JSON back into the aggregate CIF. This preserves the useful skip behavior while making skipped nodes materially available to downstream tasks and to `document.json` assembly.
- While testing this, a counting nuance surfaced: the layout-enabled graph has both `layout` and `structured` tasks, but the public `extract_structured_document()` return value historically reports structured page completion, not total DAG nodes. The return accounting was corrected to count structured tasks only, including valid skipped structured pages as completed output pages.
- Another subtle bug appeared in the resource scheduler coverage. Unknown resources defaulted to a semaphore capacity of one, which accidentally serialized CPU tasks when they declared `resource: cpu`. The executor now gives CPU work capacity equal to the worker count while keeping GPU-style resources like layout, OCR, and formula serialized by default.
- The recommended PP-DocLayoutV3 issue was fixed locally in the adapter. The provider no longer assigns reading order from raw detection enumeration; it sorts detections by a deterministic top-to-bottom, left-to-right bbox key before assigning `reading_order`. This is still a pragmatic reading-order approximation rather than a full document-layout ordering model, but it removes the known adapter bug where provider output order leaked directly into CIF order.
- The recommended malformed-JSON issue was fixed at the OCR provider boundary. `OutputValidator` can still repair JSON when asked, but `OllamaOCRProvider` now disables repair on the first unconstrained model response. A repairable malformed response therefore fails validation, triggers the constrained retry, and only the retry attempt may use the repair path. That keeps retry telemetry and prompt tightening meaningful instead of silently accepting broken first-pass output.
- Provider import failures were changed from process termination to recoverable exceptions. Surya OCR/layout and missing Transformers for PP-DocLayout now raise `RuntimeError` with install guidance instead of calling `sys.exit(1)`. This keeps library and phase code composable under CLI, tests, and orchestrated workers; the CLI can still decide how to present the error without providers killing the process directly.
- Test work was done under the repository `.venv` after an initial mistake using system `python3`, which hid available pytest dependencies. Focused coverage was added for stale DAG artifacts, rejected resume hooks, skipped layout rehydration, PP-DocLayout reading order, provider import errors, and Ollama OCR constrained retry after repairable malformed JSON.
- Existing tests also exposed two stale assumptions caused by moving PDF extraction onto the DAG executor. GPU claim ordering is no longer a stable contract because layout offload can happen after OCR work while the graph owns provider lifecycle, so that assertion now checks required claim counts. A figure artifact assertion was also moved inside its temporary directory lifetime so it validates the actual artifact before cleanup removes it.
- Verification run with `.venv/bin/python`: `python -m py_compile src/pocket_specialist/core/dag.py src/pocket_specialist/phases/extract.py tests/test_phase_a_foundation.py tests/test_phase_c_formula.py` passed; focused pytest `tests/test_phase_a_foundation.py tests/test_phase_c_formula.py -q` passed with `34 passed`; full pytest initially reported one timing-sensitive GPU lock timeout but the failing test passed on immediate isolated rerun.

Development benchmark timing follow-up:

- Added benchmark-friendly final completion stamps to ingestion-facing CLI commands. `layout`, `extract-structured`, `extract-structured-corpus`, single-document `run`, and corpus `run-all` now report both elapsed wall-clock duration and a concrete local `finished_at` timestamp in their final output.
- Kept the formatter shared inside `cli.py` so the benchmark output shape stays consistent across single-file and batch commands while remaining easy to remove with the rest of the development-only instrumentation before release.
- Updated README development instrumentation notes to mention the final `elapsed=` and `finished_at=` fields.

Development page and region timing follow-up:

- Added explicit `[PAGE START]` and `[PAGE COMPLETE]` timing messages around layout, structured, and OCR page stages in the active PDF extraction paths.
- Added `[REGION START]` and `[REGION COMPLETE]` timing messages inside layout-guided OCR/formula region processing so a long page shows which detected region is currently consuming time.
- Updated README development instrumentation notes to document the page/region timing messages.
- Validation performed: `.venv/bin/python -m py_compile src/pocket_specialist/core/progress.py src/pocket_specialist/phases/extract.py` passed.

OCR throughput tuning follow-up:

- Added OCR speed controls to runtime configuration: `ocr.page_fallback_region_threshold`, `ocr.max_parallel_requests`, `ocr.skip_residual_text_regions_for_native_pdf`, and `formula.defer`.
- Layout-guided digital PDF extraction now skips unmatched residual native-text-like regions by default, keeping OCR focused on table, figure, formula, code, and key-value residual regions.
- Scanned/no-native pages with many OCR/formula regions can now use page-level OCR fallback instead of per-region Ollama calls when the configurable threshold is reached.
- OCR calls are now gated by `ocr.max_parallel_requests`; setting it above 1 also bypasses the cross-process GPU file lock for OCR only, while layout/formula remain serialized.
- Formula extraction can now be deferred with `formula.defer=true`, and disabled/deferred formula regions no longer trigger symbolic OCR fallback.
- Fallback OCR provider loading is now lazy in the active extraction paths: the fallback provider is built and loaded only when primary OCR extraction fails.

## fix/pp-doclayout - layout fidelity, crop-only formula routing, and PaddleOCR layout service

This branch continued from `fix/review1` to tighten the PP-DocLayout integration around what the pipeline actually needs: reliable layout regions, preserved reading order, preserved polygons, and formula regions that act as crop proposals for UniMERNet rather than as early semantic formula interpretations.

Key decisions:

- Removed the geometric re-sort from the Transformers-backed `PPDocLayoutV3LayoutProvider` and now preserve the model's native reading-order output directly.
- Expanded PP-DocLayout label normalization to cover the model's actual label set such as `abstract`, `algorithm`, `aside_text`, `content`, `doc_title`, `reference`, `reference_content`, `vision_footnote`, `chart`, `seal`, `figure_title`, `paragraph_title`, and `formula_number`.
- Switched the in-process PP-DocLayout adapter away from the generic `transformers.pipeline("object-detection")` path and onto the lower-level `AutoImageProcessor` plus `AutoModelForObjectDetection` path so `polygon_points` and model `order_seq` are preserved.
- Added `polygon` to `LayoutRegion` and threaded polygons through service payload parsing and checkpoint reloads.
- Replaced native block matching from centroid-in-box to overlap scoring. Matching now prefers polygon-vs-bbox overlap when polygons exist and falls back to bbox intersection otherwise.
- Fixed a coordinate-space bug in the structured path by scaling native PDF text block coordinates to the same rendered-page zoom used for layout inference.
- Corrected the formula contract in the structured pipeline. PP-DocLayout `formula` regions are now treated as crop-only routing hints for the formula stage instead of being consumed by native-text matching or converted into formula semantics from native/OCR fallback text.
- Split OCR-region processing from formula-region processing so page-level OCR fallback applies only to text-like OCR regions and does not bypass crop-based formula extraction.
- Added an isolated PaddleOCR layout service and provider path so official PaddleOCR layout inference with tuning knobs can be compared against the current Transformers-backed PP-DocLayout path without changing the main runtime dependency stack.

Implementation details:

- `src/pocket_specialist/layout/providers.py`
  - `PPDocLayoutV3LayoutProvider` now uses `AutoImageProcessor.post_process_object_detection(...)` and keeps `polygon_points` plus `order_seq`.
  - Added `PaddleOCRLayoutServiceProvider` with the same `/load`, `/detect`, `/offload`, `/health` contract used by the existing isolated layout services.
- `src/pocket_specialist/phases/extract.py`
  - Added polygon-aware overlap scoring helpers.
  - Stopped native block routing from consuming formula regions.
  - Removed native formula fallback conversion from `_native_contract()`.
  - Split formula-region handling into a dedicated crop-based formula path separate from OCR-region handling.
- `src/pocket_specialist/handlers/intake.py`
  - `get_pdf_native_blocks()` now accepts render zoom/DPI context and scales native PDF coordinates into rendered-image space.
- `src/pocket_specialist/layout/paddle_service.py`
  - Added a standalone PaddleOCR layout microservice with configurable `model_name`, `img_size`, `threshold`, `formula_threshold`, `layout_nms`, `layout_unclip_ratio`, and `layout_merge_bboxes_mode`.
- `src/pocket_specialist/core/config.py`, `pipeline.toml`, and `src/pocket_specialist/cli.py`
  - Added layout tuning/config fields and a new `serve-paddleocr-layout` CLI entrypoint.
- `services/paddleocr_layout_service/requirements.txt`
  - Added the isolated service requirements for the PaddleOCR layout runtime.

Validation performed:

- `.venv` syntax checks passed for the touched modules:
  - `PYTHONPATH=src .venv/bin/python -m py_compile src/pocket_specialist/layout/providers.py src/pocket_specialist/phases/extract.py src/pocket_specialist/handlers/intake.py`
  - `PYTHONPATH=src .venv/bin/python -m py_compile src/pocket_specialist/core/config.py src/pocket_specialist/cli.py src/pocket_specialist/layout/paddle_service.py`
- Real PP-DocLayout page-6 validation at `192` DPI confirmed polygon preservation and overlap matching:
  - `59` layout regions total
  - `59` regions with polygons
  - `44` native blocks matched under the new overlap scorer
- Focused formula-routing validation on page 6 confirmed the crop-only handoff behavior:
  - `33` formula regions total
  - `0` formula regions consumed by native matching
  - `0` native formula blocks emitted
- Additional PP-DocLayout diagnostics confirmed that the raw model itself still emits many `formula` labels on dense scientific prose. A DPI sweep at `96`, `144`, `192`, and `300` DPI showed formula-region counts remained in the `32-34` range, which indicates the remaining false positives are mainly a model-behavior issue rather than a missing documented Transformers integration step.
- Minimal provider wiring sanity checks passed in `.venv` for the new PaddleOCR service path: the new layout tuning fields load from config and `build_layout_provider("paddleocr-layout-service")` resolves to `PaddleOCRLayoutServiceProvider`.

Follow-up implications:

- The active pipeline now better matches the intended architecture: layout detection proposes formula regions, UniMERNet translates those crops to LaTeX, and any future semantic enrichment can be layered afterward rather than being inferred prematurely from layout/native/OCR heuristics.
- The new PaddleOCR layout service is wired but not executed inside the main `.venv`; it still requires its own isolated environment from `services/paddleocr_layout_service/requirements.txt` plus an appropriate Paddle runtime for the target machine.
- PP-DocLayout region quality on page 6 is still noisy even with the corrected integration. The next meaningful comparison is to run the isolated PaddleOCR layout service with tuned thresholds and compare its formula-region artifacts against the current Transformers-backed PP-DocLayout output.

## fix/pp-doclayout - provider split and page-6 comparison workflow

Follow-up work in this session aligned the layout tooling with the updated CLI split and pushed the isolated PaddleOCR service further toward a real side-by-side comparison against PP-DocLayout.

Key changes:

- Confirmed and preserved the new CLI split between the layout entry points:
  - `layout-surya`
  - `layout-pp-doclayout-v3`
  - plus the matching structured extraction variants.
- Fixed the in-process PP-DocLayout provider so it now honors layout config instead of silently hardcoding old defaults:
  - `layout.model_name`
  - `layout.threshold`
  - `layout.formula_threshold`
  - `layout.img_size`
- Normalized the default PP-DocLayout model id to the documented Hugging Face identifier `PaddlePaddle/PP-DocLayoutV3_safetensors` while keeping the service-side PaddleOCR model-name mapping explicit.
- Switched the default layout provider in `pipeline.toml` back to `pp-doclayout-v3` so the default runtime matches the intended layout path.
- Added a reusable comparison helper in `src/pocket_specialist/layout/compare.py` and a CLI command:
  - `compare-layout-page`
  This renders a single page once and writes the same artifact bundle for each provider:
  - base page render
  - full overlay
  - formula-only overlay
  - region crops
  - per-provider JSON summary
- Patched the isolated PaddleOCR layout service to reduce accidental coupling to the main runtime:
  - added a no-op local GPU scheduler fallback when `torch` is absent
  - normalized incoming HF-style PP-DocLayout model ids into PaddleOCR layout model names
  - added retry logic that drops unsupported `predict()` kwargs for version-specific PaddleOCR API differences
  - removed detect-time kwargs that triggered a Paddle runtime `ConvertPirAttribute2RuntimeAttribute` failure on this machine
- Created and populated an isolated service virtual environment under `services/paddleocr_layout_service/.venv` with `paddleocr` and `paddlepaddle`, then brought the service up successfully on localhost.

Validation performed:

- `.venv` syntax checks passed for the touched modules during this follow-up work.
- Real page-6 PP-DocLayout layout-only run at `192` DPI succeeded and wrote artifacts under `/tmp/ppdoclayout_page6_check`.
- Real side-by-side comparison command runs succeeded far enough to generate the PP-DocLayout side cleanly under `/tmp/layout_compare_page6/pp-doclayout-v3`.
- PaddleOCR service validation progressed in stages:
  - service health check returned `True`
  - `/load` now succeeds after model-source and model-name fixes
  - official Paddle model weights were downloaded and cached under `~/.paddlex/official_models/PP-DocLayout_plus-L`
  - `/detect` still fails on this machine, which means the remaining blocker is a PaddleOCR/Paddle runtime inference compatibility issue rather than service reachability or missing artifacts.

Current status:

- PP-DocLayout comparison artifacts are available and usable.
- The new comparison workflow is wired to the updated command structure and is ready for repeated provider checks.
- The remaining open issue is the isolated PaddleOCR service `detect` runtime path; once that inference incompatibility is resolved, the same command should produce both provider bundles side by side without further CLI changes.

## fix/surya-v2 - isolated Surya v2 service, provider-specific entry points, and blocked live verification

This follow-up migrated the isolated Surya layout path to the Surya v2 runtime model, added provider-specific pipeline entry points so Surya and PP-DocLayout can be run without editing config, and attempted a real layout-only verification run against the repository PDF in `RAG-corpus`.

Key changes:

- `src/pocket_specialist/layout/service.py`
  - Reworked the isolated Surya service from the old `FoundationPredictor` pattern to the Surya v2 `SuryaInferenceManager` + `LayoutPredictor` path.
  - Expanded label normalization for the newer Surya output vocabulary and preserved extra v2 fields such as `raw_label`, `polygon`, and `count` in the service payload.
  - Added a more defensive runtime wrapper around manager lifecycle and result field access so the repo-side `LayoutResult` contract stays stable even though the upstream Surya objects changed shape.
- `services/surya_layout_service/requirements.txt`
  - Re-pinned the isolated Surya service environment to `surya-ocr==0.20.0` and let Surya own the matching Transformers dependency rather than forcing the old v1 pin set.
- `src/pocket_specialist/phases/extract.py`
  - Threaded an explicit `layout_provider_name` override through the layout-only and structured extraction paths.
  - This keeps the pipeline implementation shared while allowing the caller to select a provider at runtime instead of mutating `pipeline.toml` or environment defaults.
- `src/pocket_specialist/cli.py`
  - Added provider-scoped layout entry points:
    - `layout-surya`
    - `layout-pp-doclayout-v3`
  - Added provider-scoped structured extraction entry points:
    - `extract-structured-surya`
    - `extract-structured-pp-doclayout-v3`
  - Routed those commands through shared helpers that write to provider-scoped output directories so repeated runs do not overwrite each other.
- `src/pocket_specialist/layout/compare.py`
  - Updated the default comparison pair to `surya-layout-service` vs `pp-doclayout-v3`.
- `tests/test_phase_c_formula.py`
  - Removed stale expectations tied to the deleted in-process `SuryaLayoutProvider` path.
  - Replaced them with coverage for the HTTP-backed Surya service payload normalization and the new Surya v2 runtime wrapper.

Validation performed:

- Syntax validation passed for the touched modules with:
  - `python3 -m py_compile src/pocket_specialist/cli.py src/pocket_specialist/phases/extract.py src/pocket_specialist/layout/compare.py src/pocket_specialist/layout/service.py`
- CLI registration validation in `.venv` confirmed the new commands are live:
  - `layout`
  - `layout-surya`
  - `layout-pp-doclayout-v3`
  - `extract-structured`
  - `extract-structured-surya`
  - `extract-structured-pp-doclayout-v3`
  - `compare-layout-page`
- Created an isolated Surya v2 service environment at `services/surya_layout_service/.venv` and confirmed it imports:
  - `surya.inference`
  - `surya.layout`
  - `surya-ocr 0.20.0`
- Real verification attempt against `RAG-corpus/unknown_2018_mpphys-many-particle-simulation-package.pdf`:
  - started the isolated Surya service on `http://127.0.0.1:8004`
  - ran `PIPELINE_LAYOUT_BASE_URL=http://127.0.0.1:8004 PYTHONPATH=src .venv/bin/python -m pocket_specialist.cli layout-surya RAG-corpus/unknown_2018_mpphys-many-particle-simulation-package.pdf`
  - the layout phase processed rendering checkpoints but failed all `10` pages before any layout JSON was written
  - direct `POST /detect` probing on rendered page 1 returned the concrete blocker:
    - `{"error": "docker binary not found. Install Docker (https://docs.docker.com/get-docker/) and ensure the daemon is running."}`

Current status:

- The repository-side Surya v2 integration and the provider-specific entry points are in place.
- The isolated Surya v2 environment itself imports correctly.
- Live layout verification is currently blocked on this machine because the installed Surya v2 inference manager expects a Docker-backed backend and Docker is not available in the execution environment.
- The PP-DocLayout-specific entry points remain usable independently of this blocker.

Follow-up implication:

- To complete real Surya v2 page verification on this machine, the next required environment change is to provide Docker or configure a supported non-Docker Surya inference backend that the installed `SuryaInferenceManager` can start locally.


## Surya v2 vLLM diagnostics and test isolation

This update was driven by an operational failure mode in the isolated Surya layout service. The service itself was healthy, but first-request layout work was timing out because the client-side layout timeout was far shorter than the backend cold-start path.

Key findings:

- The isolated Surya service venv is on `surya-ocr 0.20.0`, while the main repo venv still carries `surya-ocr 0.17.1`. The v2 behavior lives only in the service environment.
- `surya/inference/__init__.py` in the service venv uses a lazy `SuryaInferenceManager` that auto-selects `vllm` on NVIDIA GPUs and spawns `vllm/vllm-openai:v0.20.1` via Docker.
- The cached vLLM sentinel at `~/.cache/datalab/surya/vllm_server.json` pointed at `surya-vllm-57275` with `pid: null`, which is consistent with Docker-backed startup/cleanup rather than a native process.
- The vLLM container on the RTX 2080 Ti took roughly 294 seconds to fully initialize on first cold start, including weight download, model load, torch compile, and warmup.
- The repo-side layout HTTP client still enforces a 60 second detect timeout, so the first page request can fail even though the backend later becomes healthy.
- Once the backend is warm, page-6 layout detection completes quickly: the page-6-only comparison ran in 4.37 seconds and produced 26 regions.

Verification:

- Installed `PyMuPDF` into `services/surya_layout_service/.venv` so the service env could render PDF pages directly.
- Added a dedicated `tests/test_surya_layout_service.py` so Surya runtime and HTTP-client checks no longer depend on `pp-doclayout` coverage.
- Confirmed the Surya page-6 formula boxes align with the rendered page and correspond to equations (15), (16), (17), and (18) on the PDF page.
- The formula boxes were verified in PDF geometry space, not just by label; each detected formula box overlapped the expected equation text on the rendered page.

Follow-up implication:

- The actionable failure is a timeout mismatch, not a broken model path. If the cold-start path needs to remain supported, the layout client timeout should be raised or the service should be pre-warmed before page work begins.


## Surya v2 offload correctness

This update tightened the Surya runtime teardown path so the isolated layout service now actually offloads the backend rather than only dropping Python references.

Key decisions:

- `SuryaLayoutRuntime.offload()` now prefers the v2 manager's explicit `stop()` method and falls back to the older shutdown/close/terminate methods for compatibility.
- When the loaded backend is Docker-backed vLLM and the backend was spawned by the service, the runtime now stops the matching `surya-vllm-<port>` container during offload.
- The same teardown path preserves the older llama.cpp process cleanup behavior for completeness.

Validation performed:

- Added a regression test in `tests/test_surya_layout_service.py` proving that offload calls the manager stop hook and issues `docker stop surya-vllm-57275` for a spawned vLLM backend.
- Verified the live container `surya-vllm-57275` was stopped before commit time.
- Ran `PYTHONPATH=src .venv/bin/python -m pytest tests/test_surya_layout_service.py -q` and confirmed `5 passed`.

Follow-up implication:

- Surya's `load()`/`detect()` lifecycle remains lazy, but `offload()` now matches the operational intent: the model backend is actually torn down when the service is released.
