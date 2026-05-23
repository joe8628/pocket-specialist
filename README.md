# pocket-specialist

Local-first document intelligence pipeline for heterogeneous document ingestion, typed extraction, and retrieval-oriented downstream processing.

This branch is being refactored against [docs/document_intelligence_pipeline_spec_v_0_5_1_beta_complete.md](/home/jjmr/github-repos/pocket-specialist/docs/document_intelligence_pipeline_spec_v_0_5_1_beta_complete.md:1). The current implementation target is **Phase B — Layout & OCR** from the May 2026 beta spec.

## Phase A/B Scope

Phase A/B establishes the architectural baseline and the first structured extraction path for the new system:

- project scaffold for a DAG-oriented extraction pipeline
- typed runtime configuration
- OCR provider abstraction
- output validation and repair layer
- deterministic GPU scheduling
- typed Canonical Intermediate Format (CIF) primitives
- document classification for scanned PDFs, digital PDFs, and HTML
- structured extraction output for HTML and PDF inputs

The code layout now follows the spec terminology directly under `src/pocket_specialist/`. Stage-specific compatibility code is isolated in `compat/` while active Phase A/B subsystems live in `core/`, `handlers/`, `layout/`, `ocr/`, `phases/`, `storage/`, and `serializers/`.

## Architectural Direction

The new spec changes the repo from a Markdown-first OCR pipeline into a structured document intelligence system with these rules:

- typed intermediates are the authoritative internal state
- OCR performs extraction, not final semantic interpretation
- layout is first-class and separate from OCR
- execution is graph-shaped, not purely linear
- GPU ownership is explicit and serialized
- providers are isolated behind protocols
- Markdown is a rendering/export layer, not storage
- provenance must be preserved on extracted artifacts

## Current Foundation Modules

Phase A/B foundation code lives under `src/pocket_specialist/` and currently includes:

- `core/config.py`: typed settings and path management
- `core/tasks.py`: extraction tasks and strongly typed processing units
- `handlers/intake.py`: document classification and CIF ingestion for supported source types
- `layout/providers.py`: layout provider protocol and default routing
- `ocr/providers.py`: `OCRProvider` protocol, Ollama providers, and Surya compatibility adapter
- `phases/extract.py`: Phase B structured extraction orchestration
- `core/validation.py`: JSON parsing, repair, and schema gatekeeping
- `core/gpu.py`: serialized GPU access and cache release hooks
- `core/cif.py`: CIF blocks, artifacts, source coordinates, and provenance

## Repository Layout

```text
src/pocket_specialist/
  core/
  handlers/
  layout/
  ocr/
  phases/
  storage/
  serializers/
  compat/
services/
  formula_extractor/
scripts/
docs/
```

## Runtime Requirements

- Python 3.11+
- CUDA-capable GPU for Surya-backed OCR paths
- Local filesystem access for checkpoints and artifacts
- Optional Ollama runtime for legacy correction paths that have not been removed yet

## Install

```bash
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
./.venv/bin/pip install -e .
```

Surya model weights download on first use.

## Current CLI Surface

The installed CLI exposes compatibility commands plus the Phase B structured extraction path:

```bash
pocket-specialist --help
pocket-specialist render <pdf>
pocket-specialist ocr <pdf>
pocket-specialist equations <pdf>
pocket-specialist correct <pdf>
pocket-specialist assemble <pdf>
pocket-specialist run <pdf>
pocket-specialist extract-structured <pdf-or-html>
```

Commands with older names are compatibility entry points around spec-aligned subsystems. New work should depend on the `src/pocket_specialist/` package layout, not root-level wrappers or the retired `pipeline/` package.

## Configuration Model

Runtime configuration now centers on typed settings in `src/pocket_specialist/core/config.py` and `pipeline.toml`, with environment-variable overrides for paths and key runtime values.

Examples:

```bash
export PIPELINE_CHECKPOINT_DIR=/data/checkpoints
export PIPELINE_OUTPUT_DIR=/data/output
export PIPELINE_OCR_PROVIDER=surya
export PIPELINE_OLLAMA_MODEL=qwen2.5vl:3b
```

## Testing

```bash
./.venv/bin/python -m pytest
```

Focused unit coverage currently exercises the Phase A foundation and Phase B intake/extraction paths.

## Status

Implemented in Phase A/B:

- typed configuration scaffold
- OCR provider abstraction baseline
- mandatory validation layer primitives
- GPU scheduler baseline
- CIF data primitives
- upgraded package layout under `src/pocket_specialist/`
- scanned/digital PDF and HTML intake classification
- layout provider abstraction baseline
- structured extraction document output

Not yet implemented from the spec:

- production-grade region-guided OCR routing
- formula subsystem implementation
- chunk serialization and retrieval APIs
- phase-wide hardening and scaling features
