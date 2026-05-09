"""Embed documents and store them in ChromaDB using Nougat for PDF extraction.

Nougat (Meta) renders each PDF page through a vision encoder-decoder and outputs
academic Markdown with LaTeX equations. It runs on CPU (slow) or GPU (recommended).
Batch size is computed automatically from available VRAM. The small model
(0.1.0-small, ~250 M params) is used by default; set NOUGAT_MODEL=0.1.0 to use
the larger model. Non-PDF formats (txt, md, csv) are read directly.
"""
import sys
import re
import csv
import io
import json
from functools import partial
from pathlib import Path

from db import client, embedding_fn, REPO_ROOT, DB_PATH
import graph as kg

CORPUS_PATH = REPO_ROOT / "RAG-corpus"
MANIFEST_PATH = DB_PATH / ".manifest-nougat.json"
SUPPORTED = {".pdf", ".txt", ".md", ".markdown", ".csv"}

EXTRACTOR_META = {"extractor": "nougat"}

_nougat_model = None


def _get_nougat_model():
    global _nougat_model
    if _nougat_model is not None:
        return _nougat_model

    import torch
    from nougat import NougatModel
    from nougat.utils.checkpoint import get_checkpoint
    from nougat.utils.device import move_to_device

    model_tag = __import__("os").environ.get("NOUGAT_MODEL", "0.1.0-small")
    device_label = torch.cuda.get_device_name(0) if torch.cuda.is_available() else "CPU"
    print(f"  Loading Nougat model ({model_tag}) on {device_label} (first run only)...")

    checkpoint = get_checkpoint(model_tag=model_tag)
    model = NougatModel.from_pretrained(checkpoint)
    # bf16 + CUDA when available; falls back to CPU automatically
    model = move_to_device(model, bf16=True, cuda=torch.cuda.is_available())
    model.eval()
    _nougat_model = model
    return model


# ── manifest ──────────────────────────────────────────────────────────────────

def _load_manifest() -> dict[str, float]:
    if MANIFEST_PATH.exists():
        return json.loads(MANIFEST_PATH.read_text())
    return {}


def _save_manifest(manifest: dict[str, float]) -> None:
    DB_PATH.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2))


# ── format detection ─────────────────────────────────────────────────────────

_PDF_MAGIC = b'%PDF'


def _sniff_format(path: Path) -> str:
    """Returns one of: 'pdf', 'markdown', 'csv', 'text', 'unsupported'."""
    try:
        with open(path, 'rb') as f:
            raw = f.read(8192)
    except OSError:
        return 'unsupported'

    if raw.startswith(_PDF_MAGIC):
        return 'pdf'

    if b'\x00' in raw:
        return 'unsupported'

    sample = None
    for enc in ('utf-8', 'latin-1'):
        try:
            sample = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if sample is None:
        return 'unsupported'

    lines = [l for l in sample.splitlines() if l.strip()]
    if not lines:
        return 'text'

    if any(re.match(r'^#{1,6}\s+\S', l) for l in lines[:40]):
        return 'markdown'

    try:
        dialect = csv.Sniffer().sniff(sample[:4096], delimiters=',;\t|')
        rows = list(csv.reader(io.StringIO('\n'.join(lines[:20])), dialect))
        non_empty = [r for r in rows if r]
        if non_empty and len(non_empty[0]) > 1:
            col_counts = [len(r) for r in non_empty[:10]]
            if max(col_counts) - min(col_counts) <= 1:
                return 'csv'
    except csv.Error:
        pass

    return 'text'


# ── text extraction & smart chunking ─────────────────────────────────────────

_HEADING_RE = re.compile(
    r'^(?:'
    r'\d+(\.\d+)*\.?\s{1,4}[A-Z][a-z]'
    r'|(Algorithm|Theorem|Lemma|Proof|Corollary|Definition|Remark|Example|Exercise)\s*[\d.:]'
    r')'
)

# Nougat missing-page sentinel patterns
_MISSING_PAGE_RE = re.compile(r'\[MISSING_PAGE_(?:FAIL|EMPTY|POST):[^\]]*\]')


def _is_heading(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    if re.match(r'^#{1,6}\s+\S', stripped):
        return True
    if len(stripped) > 80:
        return False
    if _HEADING_RE.match(stripped):
        return True
    alpha = [c for c in stripped if c.isalpha()]
    if len(alpha) >= 4 and len(stripped) < 60 and stripped.upper() == stripped:
        return True
    return False


def _split_sentences(text: str) -> list[str]:
    if text.strip().startswith('$$') or text.strip().startswith('\\['):
        return [text.strip()]
    parts = re.split(r'(?<=[.!?])\s+', text.strip())
    return [p.strip() for p in parts if p.strip()]


def _merge_into_chunks(paragraphs: list[str], max_words: int = 500, overlap: int = 2) -> list[str]:
    chunks: list[str] = []
    buffer: list[str] = []

    for para in paragraphs:
        for sentence in _split_sentences(para):
            buffer.append(sentence)
            if sum(len(s.split()) for s in buffer) >= max_words:
                chunks.append(" ".join(buffer))
                buffer = buffer[-overlap:] if len(buffer) > overlap else buffer[:]

    if buffer:
        last = " ".join(buffer)
        if not chunks or last != chunks[-1]:
            chunks.append(last)

    return [c for c in chunks if c.strip()]


def _smart_chunk(text: str, max_words: int = 500, overlap: int = 2) -> list[tuple[str, str]]:
    sections: list[tuple[str, list[str]]] = []
    current_heading = ""
    current_lines: list[str] = []

    for line in text.splitlines():
        if _is_heading(line):
            if current_lines:
                sections.append((current_heading, current_lines))
            current_heading = re.sub(r'^#{1,6}\s+', '', line.strip())
            current_lines = []
        else:
            current_lines.append(line)

    if current_lines:
        sections.append((current_heading, current_lines))

    result: list[tuple[str, str]] = []
    for heading, lines in sections:
        paragraphs = [
            p.strip()
            for p in re.split(r'\n\s*\n', "\n".join(lines))
            if p.strip()
        ]
        if not paragraphs:
            continue
        for chunk in _merge_into_chunks(paragraphs, max_words, overlap):
            body = f"{heading}\n{chunk}" if heading else chunk
            result.append((heading, body))

    return result


def _extract_pdf(path: Path) -> str:
    """Extract PDF as academic Markdown with LaTeX equations via Nougat."""
    import torch
    from nougat.utils.dataset import LazyDataset
    from nougat.utils.device import default_batch_size
    from nougat.postprocessing import markdown_compatible

    model = _get_nougat_model()
    batch_size = max(1, default_batch_size())

    dataset = LazyDataset(
        path,
        partial(model.encoder.prepare_input, random_padding=False),
    )
    dataloader = torch.utils.data.DataLoader(
        dataset,
        batch_size=batch_size,
        shuffle=False,
        collate_fn=LazyDataset.ignore_none_collate,
    )

    pages: list[str] = []
    page_num = 0

    for sample, is_last_page in dataloader:
        if sample is None:
            continue
        model_output = model.inference(image_tensors=sample, early_stopping=True)

        for j, output in enumerate(model_output["predictions"]):
            page_num += 1
            if output.strip() == "[MISSING_PAGE_POST]":
                pages.append(f"\n\n[MISSING_PAGE_EMPTY:{page_num}]\n\n")
            elif model_output["repeats"][j] is not None:
                if model_output["repeats"][j] > 0:
                    pages.append(f"\n\n[MISSING_PAGE_FAIL:{page_num}]\n\n")
                else:
                    pages.append(f"\n\n[MISSING_PAGE_EMPTY:{page_num}]\n\n")
            else:
                pages.append(markdown_compatible(output))

    full_text = "".join(pages)
    full_text = re.sub(r"\n{3,}", "\n\n", full_text).strip()
    # Remove missing-page sentinels — they carry no retrieval value
    full_text = _MISSING_PAGE_RE.sub("", full_text).strip()
    return full_text


def _extract_csv(path: Path) -> str:
    with open(path, newline='', encoding='utf-8', errors='replace') as f:
        sample = f.read(4096)
        f.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=',;\t|')
            has_header = csv.Sniffer().has_header(sample)
        except csv.Error:
            dialect = csv.excel
            has_header = False
        rows = list(csv.reader(f, dialect))

    if not rows:
        return ''

    headers = rows[0] if has_header else [f'col{i}' for i in range(len(rows[0]))]
    data_rows = rows[1:] if has_header else rows

    lines = []
    for row in data_rows:
        if not any(cell.strip() for cell in row):
            continue
        parts = [f'{h}: {v}' for h, v in zip(headers, row) if v.strip()]
        if parts:
            lines.append(' | '.join(parts))

    return '\n\n'.join(lines)


# ── collection helpers ────────────────────────────────────────────────────────

def _get_or_create_collection(collection_name: str):
    """Get or create a collection tagged with the nougat extractor metadata."""
    existing = {c.name for c in client.list_collections()}
    if collection_name in existing:
        col = client.get_collection(collection_name, embedding_function=embedding_fn)
        other = col.metadata and col.metadata.get("extractor") not in (None, "nougat")
        if other:
            print(
                f"Warning: collection '{collection_name}' was created with "
                f"{col.metadata['extractor']}. Consider using a different name."
            )
        return col
    return client.create_collection(
        collection_name,
        embedding_function=embedding_fn,
        metadata=EXTRACTOR_META,
    )


# ── ingestion ─────────────────────────────────────────────────────────────────

def ingest_texts(documents: list[str], ids: list[str], collection_name: str = "default") -> int:
    collection = _get_or_create_collection(collection_name)
    collection.upsert(documents=documents, ids=ids)
    kg.update(documents, ids)
    return len(documents)


def ingest_file(path: Path, collection_name: str = "default") -> int:
    fmt = _sniff_format(path)

    if fmt == 'pdf':
        full_text = _extract_pdf(path)
    elif fmt in ('text', 'markdown'):
        full_text = path.read_text(encoding='utf-8', errors='replace')
    elif fmt == 'csv':
        full_text = _extract_csv(path)
    else:
        print(f"  Warning: unrecognised format for '{path.name}', skipping.")
        return 0

    chunk_pairs = _smart_chunk(full_text)

    if not chunk_pairs:
        print(f"  Warning: no text extracted from '{path.name}'.")
        return 0

    chunks = [text for _, text in chunk_pairs]
    ids = [f"{path.stem}__ch{i}" for i in range(len(chunks))]

    collection = _get_or_create_collection(collection_name)
    collection.upsert(documents=chunks, ids=ids)
    kg.update(chunks, ids)
    return len(chunks)


def ingest_corpus(corpus_dir: Path = CORPUS_PATH, collection_name: str = "default") -> None:
    if not corpus_dir.exists():
        print(f"Error: corpus folder not found: {corpus_dir}")
        sys.exit(1)

    manifest = _load_manifest()
    candidates = [f for f in corpus_dir.rglob("*") if f.is_file() and f.suffix.lower() in SUPPORTED]

    if not candidates:
        print(f"No supported files found in '{corpus_dir}'.")
        return

    new_count = changed_count = skipped_count = 0

    for file in sorted(candidates):
        key = str(file.relative_to(REPO_ROOT))
        mtime = file.stat().st_mtime

        if key not in manifest:
            status = "new"
            new_count += 1
        elif manifest[key] < mtime:
            status = "changed"
            changed_count += 1
        else:
            skipped_count += 1
            continue

        print(f"  [{status}] {file.name} ...", end=" ", flush=True)
        try:
            n = ingest_file(file, collection_name)
            manifest[key] = mtime
            print(f"{n} chunks ingested.")
        except Exception as e:
            print(f"failed ({e})")

    _save_manifest(manifest)
    print(f"\nDone. {new_count} new, {changed_count} updated, {skipped_count} skipped.")


def list_collections() -> None:
    """Print all ChromaDB collections with their extractor tag."""
    collections = client.list_collections()
    if not collections:
        print("No collections found.")
        return

    print(f"{'Collection':<30} {'Extractor':<14} {'Docs':>6}")
    print("-" * 54)
    for col in sorted(collections, key=lambda c: c.name):
        meta = col.metadata or {}
        extractor = meta.get("extractor", "marker (legacy)")
        full_col = client.get_collection(col.name, embedding_function=embedding_fn)
        print(f"{col.name:<30} {extractor:<14} {full_col.count():>6}")


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    args = sys.argv[1:]

    if args and args[0] == "--list":
        list_collections()
        sys.exit(0)

    if not args:
        print(f"Scanning '{CORPUS_PATH}' for new or changed files...\n")
        ingest_corpus()
        sys.exit(0)

    file_path = Path(args[0]).resolve()
    collection_name = args[1] if len(args) > 1 else "default"

    if not file_path.exists():
        print(f"Error: file not found: {file_path}")
        sys.exit(1)

    try:
        n = ingest_file(file_path, collection_name)
        manifest = _load_manifest()
        manifest[str(file_path.relative_to(REPO_ROOT))] = file_path.stat().st_mtime
        _save_manifest(manifest)
        print(f"Ingested {n} chunks from '{file_path.name}' into collection '{collection_name}'.")
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)
