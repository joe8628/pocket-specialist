# pocket-specialist

Local-first document intelligence pipeline for heterogeneous document ingestion, typed extraction, and retrieval-oriented downstream processing.

This branch is being refactored against [docs/document_intelligence_pipeline_spec_v_0_5_0_beta.md](/home/jjmr/github-repos/pocket-specialist/docs/document_intelligence_pipeline_spec_v_0_5_0_beta.md:1). The current implementation target is **Phase A — Foundation** from the May 2026 beta spec.

## Phase A Scope

Phase A establishes the architectural baseline for the new system:

- project scaffold for a DAG-oriented extraction pipeline
- typed runtime configuration
- OCR provider abstraction
- output validation and repair layer
- deterministic GPU scheduling
- typed Canonical Intermediate Format (CIF) primitives

This is an architecture transition point, not the final end-state pipeline. Some legacy stage modules still exist in the repo while they are being retired behind the new foundation layer.

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

Phase A foundation code lives under `pipeline/foundation/` and currently includes:

- `config.py`: typed settings and path management
- `tasks.py`: extraction tasks and strongly typed processing units
- `ocr.py`: `OCRProvider` protocol and Surya-backed provider adapter
- `validation.py`: JSON parsing, repair, and schema gatekeeping
- `gpu.py`: serialized GPU access and cache release hooks
- `cif.py`: CIF blocks, artifacts, source coordinates, and provenance

The legacy top-level `config.py` remains as a compatibility facade while the rest of the repo is migrated.

## Repository Layout

```text
pipeline/
  foundation/
    cif.py
    config.py
    gpu.py
    ocr.py
    tasks.py
    validation.py
  checkpoint.py
  ocr.py
  equations.py
  correction.py
  assemble.py
  render.py
cli.py
config.py
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
```

Surya model weights download on first use.

## Current CLI Surface

The CLI still exposes the legacy commands while the architecture is being migrated:

```bash
python3 cli.py --help
python3 cli.py render <pdf>
python3 cli.py ocr <pdf>
python3 cli.py equations <pdf>
python3 cli.py correct <pdf>
python3 cli.py assemble <pdf>
python3 cli.py run <pdf>
```

Those commands should now be understood as compatibility entry points around an in-progress refactor. The new authoritative design is the spec, not the older stage naming.

## Configuration Model

Runtime configuration now centers on typed settings in `pipeline/foundation/config.py`, with environment-variable overrides for paths and key runtime values.

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

If legacy tests fail on missing old modules, that indicates the repo still contains pre-refactor test artifacts that need to be migrated or removed as part of the architecture cleanup.

## Status

Implemented in Phase A:

- typed configuration scaffold
- OCR provider abstraction baseline
- mandatory validation layer primitives
- GPU scheduler baseline
- CIF data primitives

Not yet implemented from the spec:

- first-class layout subsystem
- region-guided OCR routing
- formula subsystem isolation
- chunk serialization and retrieval APIs
- phase-wide hardening and scaling features
