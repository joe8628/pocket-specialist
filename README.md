# pocket-specialist

Local-first document intelligence pipeline for heterogeneous document ingestion, typed extraction, and retrieval-oriented downstream processing.

This branch is being refactored against `docs/document_intelligence_pipeline_spec_v_0_5_1_beta_complete.md`. The current implementation covers the Phase A-C foundation for classification, layout-aware structured extraction, OCR provider routing, formula routing/fallbacks, CIF output, checkpointing, and GPU lifecycle controls. Phase D and Phase F are intentionally skipped for now, and Phase E hardening is not complete.

## Current Scope

Implemented in the active `src/pocket_specialist/` stack:

- typed runtime configuration via `pipeline.toml` and environment overrides
- document classification for PDF, HTML, TXT/Markdown, CSV/TSV, images, DOCX, ODT, XLSX, and EPUB
- Canonical Intermediate Format (CIF) primitives with source coordinates and provenance
- DAG-oriented extraction tasks with SQLite checkpoint and observation state
- layout provider abstraction with PP-DocLayoutV3 through Transformers and Surya compatibility
- OCR provider abstraction with Ollama-backed GLM/DeepSeek providers and Surya compatibility
- region-guided PDF structured extraction with layout, OCR, table, figure, and formula routing
- UniMERNet HTTP formula provider client plus symbolic/OCR fallback paths
- document-scoped structured JSON, layout JSON, figure artifacts, and checkpoint state
- gated corpus batch subroutine for running `extract-structured` over `RAG-corpus`

Compatibility commands for the older render/OCR/equations/correction/assemble flow still exist under `src/pocket_specialist/compat/`, but new structured extraction work should target `extract-structured` and `extract-structured-corpus`.

## Repository Layout

```text
src/pocket_specialist/
  core/          # config, CIF, DAG, GPU, validation, processing units
  handlers/      # intake, classification, native format ingestion
  layout/        # layout provider protocol and adapters
  ocr/           # OCR provider protocol and adapters
  formula/       # UniMERNet client/service, symbolic formula fallback
  phases/        # structured extraction orchestration
  storage/       # SQLite checkpoint and DAG observation store
  serializers/   # Markdown compatibility assembly
  compat/        # legacy render/OCR/equations/correction commands
docs/            # architecture specs and review notes
tests/           # unit and integration-style coverage
RAG-corpus/      # default corpus input directory
checkpoints/     # default document-scoped intermediate outputs
output/          # default compatibility final outputs
data/            # default SQLite DB, artifacts, and local stores
```

`services/` and `scripts/` are currently placeholders only.

## Runtime Requirements

- Python 3.11+
- Local filesystem access for checkpoints, structured outputs, and artifacts
- Transformers for PP-DocLayoutV3 layout detection
- PyTorch/TorchVision for local model execution paths
- Ollama for the default GLM/DeepSeek OCR providers
- Optional CUDA-capable GPU for accelerated layout/OCR/formula paths
- Optional UniMERNet formula service when formula extraction is enabled

## Install

```bash
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
./.venv/bin/pip install -e .
```

If the package is not installed editable in the active environment, run commands with `PYTHONPATH=src`:

```bash
PYTHONPATH=src .venv/bin/python -m pocket_specialist.cli --help
```

## Surya V2 Setup

Surya v2 in this repo runs through the isolated layout service in `src/pocket_specialist/layout/service.py`. The repository-side pipeline can call that service directly, but the Surya runtime itself has additional backend requirements that are not covered by the main `.venv`.

### Option 1: Docker + NVIDIA GPU (`vllm`)

This is the default Surya v2 path on Linux when an NVIDIA GPU is detected. It requires host-level dependencies in addition to the isolated service virtualenv:

- Docker installed and available on `PATH`
- Docker daemon running
- NVIDIA drivers installed
- NVIDIA Container Toolkit installed so Docker can use GPU flags such as `--gpus` / `--runtime nvidia`

#### Host setup checklist

1. Install Docker and make sure the CLI is on your shell `PATH`.
2. Install the NVIDIA driver for your GPU and verify the host can see it:

```bash
nvidia-smi -L
```

3. Install NVIDIA Container Toolkit and restart Docker so GPU containers are enabled.
4. Verify Docker itself is reachable:

```bash
docker --version
docker info
```

5. Verify Docker can access the GPU:

```bash
docker run --rm --gpus all nvidia/cuda:12.3.2-base-ubuntu22.04 nvidia-smi
```

If `docker --version` fails with `docker: command not found`, Docker is either not installed or not on the current shell `PATH`. Check with:

```bash
which docker
command -v docker
echo $PATH
```

If `docker info` fails, the daemon is not reachable from the current user/session.

#### Ubuntu-style install outline

The exact package names may vary by distro, but on Ubuntu-like systems the host setup usually looks like this:

```bash
# Docker
sudo apt-get update
sudo apt-get install -y docker.io
sudo systemctl enable --now docker

# Optional: allow running docker without sudo after re-login
sudo usermod -aG docker $USER

# NVIDIA driver verification
nvidia-smi -L
```

Then install NVIDIA Container Toolkit using the official NVIDIA instructions for your distro and restart Docker. After that, rerun the Docker verification commands above before starting the Surya service.

Create the isolated Surya service environment:

```bash
python3 -m venv services/surya_layout_service/.venv
services/surya_layout_service/.venv/bin/pip install -r services/surya_layout_service/requirements.txt
```

Recommended environment for a single-GPU host using an RTX 2080 Ti:

```bash
export SURYA_INFERENCE_BACKEND=vllm
export VLLM_GPUS=0
export VLLM_GPU_TYPE=4090
export VLLM_DTYPE=float16
export DOCKER_HF_CACHE_PATH=$HOME/.cache/huggingface
```

The installed Surya package does not currently list `2080ti` as a supported `VLLM_GPU_TYPE` value. On this hardware, use `VLLM_GPU_TYPE=4090` as a sizing override and force `VLLM_DTYPE=float16` because RTX 2080 Ti is a Turing-generation card and should not use the default `bfloat16` path. You may still need to tune `VLLM_GPU_MEMORY_UTILIZATION` or switch to `SURYA_INFERENCE_BACKEND=llamacpp` if the `vllm` container proves too heavy for the card.

Start the isolated Surya service:

```bash
PYTHONPATH=src services/surya_layout_service/.venv/bin/python \
  -m pocket_specialist.layout.service --host 127.0.0.1 --port 8004
```

Then point the pipeline at that service for the current run:

```bash
PIPELINE_LAYOUT_BASE_URL=http://127.0.0.1:8004 \
PYTHONPATH=src .venv/bin/python -m pocket_specialist.cli layout-surya /path/to/file.pdf
```

Or run the full structured path:

```bash
PIPELINE_LAYOUT_BASE_URL=http://127.0.0.1:8004 \
PYTHONPATH=src .venv/bin/python -m pocket_specialist.cli extract-structured-surya /path/to/file.pdf
```

### Option 2: Native `llama.cpp` (`llamacpp`)

If you do not want Docker, Surya v2 can use its `llamacpp` backend instead. That path requires:

- `llama-server` installed and available on `PATH`, or `LLAMA_CPP_BINARY` set explicitly
- enough local disk/cache space for the Surya GGUF assets

Example:

```bash
export SURYA_INFERENCE_BACKEND=llamacpp
export LLAMA_CPP_BINARY=$(command -v llama-server)
```

Then start the same isolated service command shown above.

### Preflight behavior

The Surya layout service now performs a startup preflight before model initialization. If Surya v2 selects `vllm` and Docker or NVIDIA runtime support is missing, the service returns a clear error immediately instead of timing out later during page detection. The same applies to the `llamacpp` path when `llama-server` is not installed.

## CLI

The current CLI exposes both the active structured extraction path and legacy compatibility commands:

```bash
pocket-specialist --help
pocket-specialist extract-structured <document>
pocket-specialist extract-structured-corpus --enabled
pocket-specialist layout <pdf>
pocket-specialist status [--pdf <pdf> | --doc <doc-slug>]
pocket-specialist reset [--pdf <pdf> | --doc <doc-slug>] [--stage <stage>] --yes

# compatibility pipeline
pocket-specialist render <pdf>
pocket-specialist ocr <pdf>
pocket-specialist equations <pdf>
pocket-specialist correct <pdf>
pocket-specialist assemble <pdf>
pocket-specialist run <pdf>
pocket-specialist run-all [corpus-dir]
```

For a single document:

```bash
pocket-specialist extract-structured /path/to/file.pdf
```

For automatic corpus processing from `RAG-corpus`:

```bash
pocket-specialist extract-structured-corpus --enabled
```

You can also scan another directory:

```bash
pocket-specialist extract-structured-corpus --enabled --corpus-dir /path/to/corpus
```

The corpus subroutine is gated by design. Enable it per run with `--enabled`, or persist the toggle with `[batch].structured_corpus_enabled = true` / `PIPELINE_STRUCTURED_CORPUS_ENABLED=1`.

## Outputs

Default structured extraction outputs are document-scoped:

```text
checkpoints/<doc-slug>/layout/page_0001.json
checkpoints/<doc-slug>/structured/page_0001.json
checkpoints/<doc-slug>/structured/document.json
data/artifacts/<doc-slug>/figures/<block-id>.png
data/pipeline.db
```

`layout/page_*.json` contains layout regions, bounding boxes, confidence, and reading order. `structured/page_*.json` contains per-page structured blocks. `structured/document.json` contains the full CIF aggregate returned by `extract-structured`.

Development-only status output is printed during ingestion phases. It includes `[START]`, `[VALIDATION]`, `[MODEL]`, `[PROGRESS]`, `[PAGE START]`, `[PAGE COMPLETE]`, `[REGION START]`, `[REGION COMPLETE]`, `[COMPLETED]`, and `[ERROR]` messages for script and model processing. Page-stage and region-stage messages include elapsed timing so slow pages and individual OCR/formula regions are visible from the terminal log. Final command output also includes `elapsed=<duration>` and `finished_at=<local ISO timestamp>` so ingestion runs can be benchmarked end to end. This is implemented as temporary development instrumentation and should be removed before release with the development checkpoints.

Development-only checkpoints are also written after ingestion stages. These are temporary debugging breadcrumbs intended to be removed before release, not disabled as a runtime feature:

```text
checkpoints/<doc-slug>/_dev_checkpoints/intake/document.md
checkpoints/<doc-slug>/_dev_checkpoints/render/page_0001.md
checkpoints/<doc-slug>/_dev_checkpoints/render/artifacts/page_0001.png
checkpoints/<doc-slug>/_dev_checkpoints/layout/page_0001.md
checkpoints/<doc-slug>/_dev_checkpoints/ocr/page_0001.md
checkpoints/<doc-slug>/_dev_checkpoints/formula/page_0001.md
checkpoints/<doc-slug>/_dev_checkpoints/structured/page_0001.md
checkpoints/<doc-slug>/_dev_checkpoints/structured/document.md
```

Each stage folder owns its own `artifacts/` subfolder for debug copies such as page renderings, layout JSON, OCR crops, formula crops, equation crops, and structured JSON snapshots.

The older compatibility pipeline writes these outputs:

```text
checkpoints/<doc-slug>/rendered/page_0001.png
checkpoints/<doc-slug>/ocr/page_0001.json
checkpoints/<doc-slug>/equations/page_0001.json
checkpoints/<doc-slug>/crops/page_0001_eq_*.png
checkpoints/<doc-slug>/corrected/page_0001.md
output/<doc-slug>/<doc-slug>.md
output/<doc-slug>/<doc-slug>.json
```

## Configuration

Runtime configuration is loaded from `pipeline.toml`, with environment-variable overrides for key settings.

Examples:

```bash
export PIPELINE_CHECKPOINT_DIR=/data/checkpoints
export PIPELINE_OUTPUT_DIR=/data/output
export PIPELINE_DB_PATH=/data/pipeline.db
export PIPELINE_LAYOUT_PROVIDER=pp-doclayout-v3
export PIPELINE_OCR_PROVIDER=glm-ocr
export PIPELINE_OCR_FALLBACK_PROVIDER=deepseek-ocr
export PIPELINE_OCR_PAGE_FALLBACK_REGION_THRESHOLD=12
export PIPELINE_OCR_MAX_PARALLEL_REQUESTS=1
export PIPELINE_FORMULA_DEFER=0
export PIPELINE_STRUCTURED_CORPUS_ENABLED=1
```

Relevant defaults in `pipeline.toml`:

```toml
[layout]
provider = "pp-doclayout-v3"
enabled = true

[ocr]
provider = "glm-ocr"
fallback_provider = "deepseek-ocr"
page_fallback_region_threshold = 12
max_parallel_requests = 1
skip_residual_text_regions_for_native_pdf = true

[formula]
enabled = true
fallback_to_ocr = true
defer = false

[batch]
structured_corpus_enabled = false

[storage]
sqlite_path = "./data/pipeline.db"
artifact_path = "./data/artifacts"
chroma_path = "./data/chroma"
```

## Checkpoints And Status

Checkpointing uses SQLite tables for legacy stage state plus DAG node observations:

- `dag_node_state` stores current `(document, page, node)` status for resume decisions.
- `dag_observations` stores append-only node events.
- Stage tables preserve compatibility with `render`, `ocr`, `layout`, `equations`, `correction`, and `structured` status.
- `pocket-specialist status` reports both stage summaries and DAG node summaries.
- `pocket-specialist reset` removes document-scoped output files and clears checkpoint rows for the selected stage and later stages.

## Testing

```bash
PYTHONPATH=src .venv/bin/python -m pytest
```

Focused tests cover config, DAG/checkpoint behavior, intake, structured extraction, OCR retry behavior, and formula routing.

## Known Gaps

- Phase E production hardening is not complete.
- Phase D and Phase F are intentionally out of the current implementation scope.
- Retrieval/indexing modules are placeholders and are not wired into a production retrieval flow.
- The compatibility `run` pipeline is separate from the active `extract-structured` pipeline.
