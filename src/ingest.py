"""Embed documents and store them in ChromaDB."""
import sys
import json
from pathlib import Path
from pypdf import PdfReader
from db import client, embedding_fn, REPO_ROOT, DB_PATH

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


# ── text extraction ───────────────────────────────────────────────────────────

def _chunk_text(text: str, chunk_size: int = 500, overlap: int = 50) -> list[str]:
    words = text.split()
    chunks, i = [], 0
    while i < len(words):
        chunks.append(" ".join(words[i : i + chunk_size]))
        i += chunk_size - overlap
    return chunks


def _extract_pdf(path: Path) -> list[str]:
    reader = PdfReader(path)
    pages = []
    for page in reader.pages:
        text = (page.extract_text() or "").strip()
        if text:
            pages.append(text)
    return pages


# ── ingestion ─────────────────────────────────────────────────────────────────

def ingest_texts(documents: list[str], ids: list[str], collection_name: str = "default") -> int:
    collection = client.get_or_create_collection(collection_name, embedding_function=embedding_fn)
    collection.upsert(documents=documents, ids=ids)
    return len(documents)


def ingest_file(path: Path, collection_name: str = "default") -> int:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        pages = _extract_pdf(path)
    elif suffix == ".txt":
        pages = [path.read_text(encoding="utf-8")]
    else:
        raise ValueError(f"Unsupported file type: {suffix}")

    chunks, ids = [], []
    for page_num, page_text in enumerate(pages):
        for chunk_num, chunk in enumerate(_chunk_text(page_text)):
            chunks.append(chunk)
            ids.append(f"{path.stem}__p{page_num}__c{chunk_num}")

    if not chunks:
        print(f"  Warning: no text extracted from '{path.name}'.")
        return 0

    collection = client.get_or_create_collection(collection_name, embedding_function=embedding_fn)
    collection.upsert(documents=chunks, ids=ids)
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
