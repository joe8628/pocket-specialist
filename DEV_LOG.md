# Development Log

This log records the implementation process and engineering decisions behind the current DAG OCR refactor line. It is intentionally more detailed than commit messages: each entry captures context, tradeoffs, validation, bugs found, and follow-up implications.

## Branch Lineage

- `main` baseline: legacy stage-oriented OCR/correction pipeline.
- `DAG-OCR-phase-A`: Phase A foundation work from the document-intelligence beta spec.
- `DAG-OCR-phase-B`: Phase B layout/OCR and package-structure migration.
- `feat/checkpoints`: current checkpoint observation work. The requested branch name `feat:checkpoints` was not used because `:` is invalid in Git ref names.

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
