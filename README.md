# textbook-ocr

GPU-accelerated pipeline that converts physics textbook PDFs into clean, structured Markdown with properly rendered equations. Stage 4 now verifies equation crops one at a time with a vision model and then renders the page deterministically.

## Pipeline overview

| Stage | Command | Model(s) |
|-------|---------|----------|
| S0 — Rename corpus | `rename-corpus` | — |
| S1 — Render pages | `render` | PyMuPDF |
| S2 — OCR | `ocr` | Surya (detection + recognition) |
| S3 — Layout + equations | `equations` | Surya layout + Surya LaTeX OCR |
| S4 — Equation correction | `correct` | `qwen2.5vl:3b` via Ollama |
| S5 — Assemble | `assemble` | — |

## Requirements

- Python 3.10+
- CUDA-capable GPU (tuned for RTX 2080 Ti, 11 GB VRAM)
- [Ollama](https://ollama.com) running locally

```bash
# Install Python deps (order matters — see requirements.txt comments)
pip install -r requirements.txt

# Pull the LLM
ollama pull qwen2.5vl:3b
```

Surya model weights are downloaded automatically on first run.

## Usage

### Full pipeline (recommended)

```bash
python3 cli.py run <pdf>
```

Options:

| Flag | Default | Description |
|------|---------|-------------|
| `--start-page N` | 1 | First page (1-indexed) |
| `--end-page N` | last | Last page inclusive |
| `--zoom FLOAT` | 2.0 | Render scale (2.0 ≈ 150 DPI) |
| `--no-llm` | off | Skip Stage 4 equation correction |
| `--ollama-model NAME` | `qwen2.5vl:3b` | Ollama model for Stage 4 |
| `--parallel-pages N` | `2` | Max Stage 4 pages to process concurrently |
| `--output-dir PATH` | `output/` | Final output directory |

Examples:

```bash
# Full book
python3 cli.py run RAG-corpus/scherer.pdf

# Pages 50–59 only
python3 cli.py run RAG-corpus/scherer.pdf --start-page 50 --end-page 59

# Single page
python3 cli.py run RAG-corpus/scherer.pdf --start-page 42 --end-page 42

# Skip Stage 4 equation correction (faster, Surya-only output)
python3 cli.py run RAG-corpus/scherer.pdf --no-llm

# Ten non-consecutive pages (run once per page; checkpoints accumulate)
for p in 12 34 56 78 100 123 145 167 200 220; do
  python3 cli.py run RAG-corpus/scherer.pdf --start-page $p --end-page $p
done
```

### Run all PDFs in corpus

```bash
python3 cli.py run-all
python3 cli.py run-all RAG-corpus/ --start-page 1 --end-page 100
```

### Per-stage commands

Run individual stages when you need to re-process or debug a specific step.

```bash
# Stage 0: normalize filenames
python3 cli.py rename-corpus RAG-corpus/

# Stage 1: render PDF pages to PNG
python3 cli.py render RAG-corpus/scherer.pdf --start-page 1 --end-page 50

# Stage 2: Surya OCR
python3 cli.py ocr --start-page 1 --end-page 50

# Stage 3: layout detection + equation OCR
python3 cli.py equations --start-page 1 --end-page 50

# Stage 4: equation correction from Stage 3 crop images
python3 cli.py correct --start-page 1 --end-page 50
python3 cli.py correct --start-page 1 --end-page 50 --parallel-pages 1

# Stage 5: assemble corrected pages into final .md and .json
python3 cli.py assemble RAG-corpus/scherer.pdf
```

### Checkpoint management

Each stage records its progress in `checkpoints/pipeline.db`. Re-running a stage skips already-completed pages.

`reset` is destructive by design for development: it clears both checkpoint rows and the on-disk outputs for the selected stage and every downstream stage.

Cascade behavior:
- `reset --stage render` clears `rendered/`, `ocr/`, `equations/`, `crops/`, and `corrected/`
- `reset --stage ocr` clears `ocr/`, `equations/`, `crops/`, and `corrected/`
- `reset --stage equations` clears `equations/`, `crops/`, and `corrected/`
- `reset --stage correction` clears `corrected/`

```bash
# Show per-stage progress
python3 cli.py status

# Re-run Stage 4 only
python3 cli.py reset --stage correction -y
python3 cli.py correct --start-page 1 --end-page 50

# Rebuild equations and everything downstream
python3 cli.py reset --stage equations -y
python3 cli.py equations --start-page 1 --end-page 50
python3 cli.py correct --start-page 1 --end-page 50

# Reset all stage artifacts and checkpoints
python3 cli.py reset --yes
```

## Configuration

All tuneable constants live in `config.py`:

| Constant | Default | Description |
|----------|---------|-------------|
| `OLLAMA_MODEL` | `qwen2.5vl:3b` | Vision model used in Stage 4 equation correction |
| `RENDER_ZOOM` | `2.0` | PNG render scale |
| `EQUATION_CONF_THRESHOLD` | `0.5` | Min confidence for equation detection |
| `HEADER_STRIP_RATIO` | `0.10` | Top fraction stripped as running header |
| `FOOTER_STRIP_RATIO` | `0.90` | Bottom fraction threshold |

## Outputs

```
checkpoints/
  rendered/       page_NNNN.png        Stage 1 output
  ocr/            page_NNNN.json       Stage 2 output
  equations/      page_NNNN.json       Stage 3 output
  crops/          page_NNNN_eq_MM.png  Stage 3 equation crop output
  corrected/      page_NNNN.md         Stage 4 output
  pipeline.db                          checkpoint state

output/
  <bookname>.md                        final assembled Markdown
  <bookname>.json                      page manifest with metadata
```
