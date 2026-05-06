"""Embed documents and store them in ChromaDB."""
import sys
import re
import json
from pathlib import Path
from pypdf import PdfReader
from db import client, embedding_fn, REPO_ROOT, DB_PATH
import graph as kg

CORPUS_PATH = REPO_ROOT / "RAG-corpus"
MANIFEST_PATH = DB_PATH / ".manifest.json"
SUPPORTED = {".pdf", ".txt"}


# ── manifest ──────────────────────────────────────────────────────────────────

def _load_manifest() -> dict[str, float]:
    if MANIFEST_PATH.exists():
        return json.loads(MANIFEST_PATH.read_text())
    return {}


def _save_manifest(manifest: dict[str, float]) -> None:
    DB_PATH.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2))


# ── text extraction & smart chunking ─────────────────────────────────────────

_HEADING_RE = re.compile(
    r'^(?:'
    r'\d+(\.\d+)*\.?\s{1,4}[A-Z][a-z]'  # numbered: "1.3 Numerical Methods"
    r'|(Algorithm|Theorem|Lemma|Proof|Corollary|Definition|Remark|Example|Exercise)\s*[\d.:]'
    r')'
)


def _is_heading(line: str) -> bool:
    line = line.strip()
    if not line or len(line) > 80:
        return False
    if _HEADING_RE.match(line):
        return True
    # ALL CAPS short line with at least 4 alpha chars (chapter/section titles)
    alpha = [c for c in line if c.isalpha()]
    if len(alpha) >= 4 and len(line) < 60 and line.upper() == line:
        return True
    return False


def _split_sentences(text: str) -> list[str]:
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
            current_heading = line.strip()
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
    """Extract all text from a PDF as a single string (pages joined with double newline)."""
    reader = PdfReader(path)
    pages = []
    for page in reader.pages:
        text = (page.extract_text() or "").strip()
        if text:
            pages.append(text)
    return "\n\n".join(pages)


# ── ingestion ─────────────────────────────────────────────────────────────────

def ingest_texts(documents: list[str], ids: list[str], collection_name: str = "default") -> int:
    collection = client.get_or_create_collection(collection_name, embedding_function=embedding_fn)
    collection.upsert(documents=documents, ids=ids)
    kg.update(documents, ids)
    return len(documents)


def ingest_file(path: Path, collection_name: str = "default") -> int:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        full_text = _extract_pdf(path)
    elif suffix == ".txt":
        full_text = path.read_text(encoding="utf-8")
    else:
        raise ValueError(f"Unsupported file type: {suffix}")

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
    file_path = Path(sys.argv[1])
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
