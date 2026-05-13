# Pocket Specialist

A local RAG (Retrieval-Augmented Generation) pipeline for scientific and technical documents. Ingests PDFs, plain text, Markdown, and CSV files into a ChromaDB vector store with an optional knowledge graph layer for concept-expanded search.

---

## Architecture

```
RAG-corpus/          ← drop documents here
    └─ *.pdf / *.txt / *.md / *.csv

src/
    db.py            ← shared ChromaDB client + BAAI/bge-large-en-v1.5 embeddings
    ingest.py        ← Stage 1 pipeline: detect → extract → clean → chunk → embed
    graph.py         ← spaCy entity extraction + NetworkX knowledge graph
    search.py        ← vector search + graph-expanded search CLI

chroma_db/           ← persistent vector store (ChromaDB)
knowledge_graph/     ← persisted entity co-occurrence graph (JSON)
```

---

## Installation

```bash
pip install -r requirements.txt
python -m spacy download en_core_web_sm
```

GPU required for PDF ingestion (Marker uses CUDA). Search and non-PDF ingestion run on CPU.

---

## Ingestion

```bash
# Ingest all new/changed files in RAG-corpus/
python src/ingest.py

# Ingest a single file
python src/ingest.py path/to/document.pdf

# Ingest into a named collection
python src/ingest.py path/to/document.pdf my-collection
```

The manifest (`chroma_db/.manifest.json`) tracks mtimes so unchanged files are skipped on re-runs.

To reset the database and re-ingest from scratch:

```bash
rm -rf chroma_db
python src/ingest.py
```

---

## Search

```bash
# Vector similarity search (default)
python src/search.py "Hamiltonian eigenvalue quantum mechanics"

# Graph-expanded search (spaCy entities → concept expansion → similarity)
python src/search.py --mode graph "Fourier transform convolution"

# Show concept map for a query
python src/search.py --mode map "Navier-Stokes turbulence"

# Control result count and graph traversal depth
python src/search.py --n 10 --hops 3 "Boltzmann entropy"
```

---

## Stage 1 Pipeline — Intake & Normalization

### Format Detection (`_sniff_format`)

Content-based detection that ignores file extensions:

- **PDF** — magic bytes (`%PDF`)
- **Markdown** — ATX headings (`# ...`) in first 40 lines
- **CSV** — consistent column count via `csv.Sniffer`
- **Plain text** — fallback after binary and UTF-8/Latin-1 checks
- **Binary/unsupported** — null-byte detection, skipped with a warning

### Extraction

| Format | Method |
|---|---|
| PDF | [Marker](https://github.com/VikParuchuri/marker) — layout-aware neural OCR on GPU, preserves LaTeX equations as `$$...$$` |
| Markdown / Text | Direct `read_text` with UTF-8 → Latin-1 fallback |
| CSV | `csv.Sniffer` dialect detection, header inference, rows serialized as `Header: value \| ...` prose |

### Cleaning

Applied to PDF output after Marker extraction, in order:

1. **HTML span tags** — Marker injects `<span>` anchors for page references; stripped via regex
2. **`<br>` tags** — Marker emits `<br>` inside table cells (common in TOC tables); replaced with spaces
3. **Invisible Unicode** — soft hyphens (U+00AD), zero-width spaces (U+200B/200C/200D), BOM (U+FEFF), non-breaking spaces (U+00A0) removed or normalized
4. **Page headers/footers** (`_strip_page_artifacts`):
   - *Bare page numbers* — lines matching `[Page ]N[ of M]` are dropped
   - *Running headers* — short lines (< 72 chars, no code/math markers) appearing more than `max(3, total_lines // 30)` times are treated as repeating headers/footers and removed

### Segmentation (`_smart_chunk`)

Structure-aware chunking in two tiers:

- **Tier 1** — section headings act as hard breaks; detected patterns: numbered headings (`1.3 Methods`), Markdown ATX (`## ...`), ALL-CAPS titles, algorithm/theorem/lemma markers, and plain section-name lines (`References`, `Acknowledgments`, `Abstract`, etc.)
- **Tier 2** — within each section, paragraphs are merged into ≤500-word chunks at sentence boundaries with 2-sentence overlap
- **Code block merging** — consecutive BASIC/Fortran line-numbered lines (e.g. `1234 PRINT X`) are collapsed into a single fenced ` ``` ` block instead of being split into per-line fragments
- **Boilerplate section filter** — sections whose heading matches a boilerplate pattern (`References`, `Bibliography`, `Acknowledgments`, `Further Reading`, `Index`) are dropped entirely before chunking
- **Noise filter** — chunks are discarded when:
  - More than 50% of lines are markdown pipe-table rows (table-of-contents artifacts)
  - Fewer than 25 prose words outside fenced code blocks, with no meaningful LaTeX (`\frac`, `\int`, `$$`, etc.) — bare code snippets and one-liner comments no longer survive as standalone chunks

### Metadata Capture

Each chunk is stored in ChromaDB with structured metadata:

| Field | Description |
|---|---|
| `source` | Relative path from repo root |
| `filename` | Bare filename |
| `doc_type` | `pdf`, `text`, `markdown`, or `csv` |
| `mtime` | File modification timestamp (float) |
| `title` | From PDF metadata, or filename stem |
| `author` | From PDF metadata |
| `page_count` | PDF page count (0 for non-PDF) |
| `creation_date` | PDF creation date as `YYYY-MM-DD` |
| `heading` | Section heading the chunk falls under |
| `chunk_index` | Position of chunk within the document |

---

## Knowledge Graph

Built incrementally during ingestion using spaCy (`en_core_web_sm`):

- Named entities and noun phrases extracted from each chunk
- Co-occurring entities within a chunk are connected with weighted edges
- Graph persisted as `knowledge_graph/graph.json` (NetworkX node-link format)
- `search --mode graph` expands a query by walking N hops from matched entities before running vector search
- `search --mode map` prints the concept neighbourhood for a query term

---

## Embedding Model

`BAAI/bge-large-en-v1.5` via `sentence-transformers`. Downloaded automatically on first run; cached in `~/.cache/huggingface/hub/`.
