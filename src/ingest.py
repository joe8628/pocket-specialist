"""Embed documents and store them in ChromaDB."""
import sys
import re
import csv
import io
import json
from pathlib import Path

from db import client, embedding_fn, REPO_ROOT, DB_PATH
import graph as kg

_marker_converter = None


def _get_marker_converter():
    global _marker_converter
    if _marker_converter is not None:
        return _marker_converter
    import torch
    if not torch.cuda.is_available():
        raise RuntimeError(
            "Marker requires a CUDA-capable GPU. "
            "Check that your NVIDIA driver and PyTorch CUDA versions are compatible."
        )
    print(f"  Loading Marker models on {torch.cuda.get_device_name(0)} (first run only)...")
    from marker.converters.pdf import PdfConverter
    from marker.models import create_model_dict
    _marker_converter = PdfConverter(artifact_dict=create_model_dict())
    return _marker_converter

CORPUS_PATH = REPO_ROOT / "RAG-corpus"
MANIFEST_PATH = DB_PATH / ".manifest.json"
SUPPORTED = {".pdf", ".txt", ".md", ".markdown", ".csv"}


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
    """
    Detect document format by inspecting file content, not just extension.
    Returns one of: 'pdf', 'markdown', 'csv', 'text', 'unsupported'.
    """
    try:
        with open(path, 'rb') as f:
            raw = f.read(8192)
    except OSError:
        return 'unsupported'

    # PDF: magic bytes take priority over extension
    if raw.startswith(_PDF_MAGIC):
        return 'pdf'

    # Null bytes → binary format we can't handle
    if b'\x00' in raw:
        return 'unsupported'

    # Decode as text; try UTF-8 then Latin-1 as fallback
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

    # Markdown: at least one ATX heading (#) in the first 40 lines
    if any(re.match(r'^#{1,6}\s+\S', l) for l in lines[:40]):
        return 'markdown'

    # CSV: consistent column count across first 10 data lines
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
    r'\d+(\.\d+)*\.?\s{1,4}[A-Z][a-z]'  # numbered: "1.3 Numerical Methods"
    r'|(Algorithm|Theorem|Lemma|Proof|Corollary|Definition|Remark|Example|Exercise)\s*[\d.:]'
    r')'
)


def _is_heading(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    # Markdown ATX headings — check before length guard (headings can be long)
    if re.match(r'^#{1,6}\s+\S', stripped):
        return True
    if len(stripped) > 80:
        return False
    if _HEADING_RE.match(stripped):
        return True
    # ALL CAPS short line with at least 4 alpha chars (chapter/section titles)
    alpha = [c for c in stripped if c.isalpha()]
    if len(alpha) >= 4 and len(stripped) < 60 and stripped.upper() == stripped:
        return True
    return False


def _split_sentences(text: str) -> list[str]:
    # Treat display math blocks as atomic — never split inside $$...$$
    if text.strip().startswith('$$'):
        return [text.strip()]
    parts = re.split(r'(?<=[.!?])\s+', text.strip())
    return [p.strip() for p in parts if p.strip()]


def _merge_into_chunks(paragraphs: list[str], max_words: int = 500, overlap: int = 2) -> list[str]:
    """Merge paragraphs into sentence-bounded chunks with sentence-level overlap."""
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
    """
    Tier 1: paragraph-aware splitting with sentence-boundary overflow and overlap.
    Tier 2: section headings detected as hard breaks; heading prepended to each chunk.
    Returns list of (heading, chunk_text) pairs.
    """
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
    """Extract PDF as Markdown with LaTeX equations preserved via Marker."""
    converter = _get_marker_converter()
    rendered = converter(str(path))
    # Strip HTML span tags that marker injects for page anchors — pure noise for RAG
    return re.sub(r'<span[^>]*>.*?</span>', '', rendered.markdown, flags=re.DOTALL)


def _extract_csv(path: Path) -> str:
    """
    Convert CSV rows to readable text: 'Header1: val1 | Header2: val2 ...' per row.
    Each row becomes one paragraph so the chunker treats it as a logical unit.
    """
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


# ── ingestion ─────────────────────────────────────────────────────────────────

def ingest_texts(documents: list[str], ids: list[str], collection_name: str = "default") -> int:
    collection = client.get_or_create_collection(collection_name, embedding_function=embedding_fn)
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

    collection = client.get_or_create_collection(collection_name, embedding_function=embedding_fn)
    collection.upsert(documents=chunks, ids=ids)
    kg.update(chunks, ids)
    return len(chunks)


def ingest_corpus(corpus_dir: Path = CORPUS_PATH, collection_name: str = "default") -> None:
    """Scan corpus_dir for new or changed files and ingest them."""
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
    print(
        f"\nDone. {new_count} new, {changed_count} updated, {skipped_count} skipped."
    )


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # No args → scan RAG-corpus for new/changed files
    if len(sys.argv) == 1:
        print(f"Scanning '{CORPUS_PATH}' for new or changed files...\n")
        ingest_corpus()
        sys.exit(0)

    # Single file path provided
    file_path = Path(sys.argv[1]).resolve()
    collection_name = sys.argv[2] if len(sys.argv) > 2 else "default"

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
