"""Completeness check: verify marker cache and ChromaDB chunks for every corpus file."""
import sys
from pathlib import Path

from db import client, embedding_fn, REPO_ROOT
from ingest import CORPUS_PATH, MARKER_CACHE_PATH, SUPPORTED, _marker_cache_file, _sniff_format

# Minimum markdown size to be considered non-empty (bytes)
_MIN_MD_BYTES = 500


def _corpus_pdfs() -> list[Path]:
    return sorted(
        f for f in CORPUS_PATH.rglob("*")
        if f.is_file() and f.suffix.lower() in SUPPORTED
    )


def _chunks_per_source(collection) -> dict[str, int]:
    """Return {relative_source_path: chunk_count} for every document in the collection."""
    result = collection.get(include=["metadatas"])
    counts: dict[str, int] = {}
    for meta in result["metadatas"]:
        src = meta.get("source", "")
        counts[src] = counts.get(src, 0) + 1
    return counts


def run_check() -> bool:
    """
    Print a completeness report and return True if everything is present, False otherwise.
    """
    pdfs = _corpus_pdfs()
    if not pdfs:
        print(f"No supported files found in {CORPUS_PATH}")
        return False

    try:
        collection = client.get_collection("default", embedding_function=embedding_fn)
        chunks_by_source = _chunks_per_source(collection)
        total_chunks = sum(chunks_by_source.values())
    except Exception:
        chunks_by_source = {}
        total_chunks = 0

    COL_FILE   = 52
    COL_MD     = 10
    COL_CHUNKS = 8

    header = f"{'File':<{COL_FILE}}  {'Markdown':<{COL_MD}}  {'Chunks':<{COL_CHUNKS}}  Issues"
    print(header)
    print("-" * (len(header) + 20))

    all_ok = True

    for pdf in pdfs:
        fmt = _sniff_format(pdf)
        rel = str(pdf.relative_to(REPO_ROOT))
        short_name = pdf.name if len(pdf.name) <= COL_FILE else pdf.name[:COL_FILE - 1] + "…"

        # Markdown cache — only relevant for PDFs
        if fmt == "pdf":
            cache = _marker_cache_file(pdf)
            if not cache.exists():
                md_status = "MISSING"
                issues = ["no marker cache"]
            elif cache.stat().st_size < _MIN_MD_BYTES:
                md_status = "EMPTY"
                issues = [f"cache only {cache.stat().st_size}B"]
            else:
                size_kb = cache.stat().st_size // 1024
                md_status = f"{size_kb}KB"
                issues = []
        else:
            md_status = "n/a"
            issues = []

        # ChromaDB chunks
        chunk_count = chunks_by_source.get(rel, 0)
        chunk_status = str(chunk_count) if chunk_count > 0 else "0"
        if chunk_count == 0:
            issues.append("no chunks in DB")

        if issues:
            all_ok = False

        issue_str = ", ".join(issues) if issues else "OK"
        print(f"{short_name:<{COL_FILE}}  {md_status:<{COL_MD}}  {chunk_status:<{COL_CHUNKS}}  {issue_str}")

    print("-" * (len(header) + 20))
    print(f"Total: {len(pdfs)} files | {total_chunks} chunks in DB")

    # Warn about DB sources that have no matching corpus file
    known_rels = {str(p.relative_to(REPO_ROOT)) for p in pdfs}
    orphan_sources = [src for src in chunks_by_source if src not in known_rels]
    if orphan_sources:
        print("\nOrphan DB sources (chunks exist but file not in corpus):")
        for src in sorted(orphan_sources):
            print(f"  {src}  ({chunks_by_source[src]} chunks)")
        all_ok = False

    print()
    if all_ok:
        print("All files complete.")
    else:
        print("Issues found — re-run `python src/ingest.py` to fix missing chunks/cache.")

    return all_ok


if __name__ == "__main__":
    ok = run_check()
    sys.exit(0 if ok else 1)
