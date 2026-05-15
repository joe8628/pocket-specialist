"""Stage 0: Standardize RAG-corpus filenames and write a metadata manifest.

Naming convention produced: {lastname}_{year}_{title_slug}.pdf

Metadata priority for each file:
  1. Anna's Archive filename format  (title, author, year, isbn, publisher)
  2. PDF internal metadata           (if not garbage placeholder values)
  3. Generic filename heuristics     (split on ' _ ' separator, last token = author)
  4. Fallback                        (stem as title, year from PDF creation date)

Non-destructive: a dry run prints proposed renames without touching files.
The manifest at checkpoints/rename_manifest.json is always written (or updated),
mapping original filename → new filename → extracted metadata.
"""
from __future__ import annotations
import json
import re
import unicodedata
from pathlib import Path
from typing import Optional


# ── Placeholder / garbage metadata guards ────────────────────────────────────

_PLACEHOLDER_TITLE_RE = re.compile(
    r'^(?:microsoft\s+word|untitled|document\s*\d*|new\s+document'
    r'|temp(?:orary)?|draft|\.docx?|presentation\s*\d*|workbook\s*\d*'
    r'|copy\s+of|revision\s+\d)',
    re.IGNORECASE,
)
_PLACEHOLDER_AUTHOR_RE = re.compile(
    r'^(?:[a-z]{1,3}|unknown|author|user|admin|default|n/?a|na|\d+)\s*$',
    re.IGNORECASE,
)


def _is_garbage_title(t: str) -> bool:
    return not t or bool(_PLACEHOLDER_TITLE_RE.match(t.strip()))


def _is_garbage_author(a: str) -> bool:
    return not a or bool(_PLACEHOLDER_AUTHOR_RE.match(a.strip()))


# ── Slug helpers ──────────────────────────────────────────────────────────────

_STOP_WORDS = frozenset({
    'a', 'an', 'the', 'and', 'or', 'of', 'in', 'for', 'with',
    'on', 'at', 'to', 'by', 'from', 'is', 'are', 'its', 'via',
    'as', 'be', 'this', 'that',
})
_YEAR_RE = re.compile(r'\b(19|20)\d{2}\b')


def _to_slug(text: str, max_words: int = 5, sep: str = '-') -> str:
    """Lowercase ASCII slug from *text*, at most *max_words* content words."""
    normalized = unicodedata.normalize('NFKD', text)
    ascii_text = normalized.encode('ascii', errors='ignore').decode()
    words = re.findall(r'[a-zA-Z0-9]+', ascii_text.lower())
    content = [w for w in words if w not in _STOP_WORDS and len(w) > 1]
    return sep.join(content[:max_words]) or 'untitled'


def _lastname_slug(author: str) -> str:
    """Return an ASCII slug of the last name of the first listed author."""
    if not author:
        return 'unknown'
    # First author only (split on comma/semicolon/'and'/'et al.')
    first = re.split(r',|;|\band\b|\bet\s+al\b', author, maxsplit=1)[0].strip()
    # Handle "Last, First" vs "First Last"
    if ',' in first:
        lastname = first.split(',')[0].strip()
    else:
        parts = first.split()
        # Drop single-character initials at the end
        while len(parts) > 1 and re.match(r'^[A-Z]\.?$', parts[-1]):
            parts.pop()
        lastname = parts[-1] if parts else first
    return _to_slug(lastname, max_words=1)


def _extract_year(text: str) -> str:
    m = _YEAR_RE.search(text)
    return m.group() if m else ''


def _pdf_creation_year(path: Path) -> str:
    """Return the 4-digit creation year from a PDF's /CreationDate field."""
    try:
        import pypdf
        reader = pypdf.PdfReader(str(path), strict=False)
        raw = str((reader.metadata or {}).get('/CreationDate', ''))
        m = re.match(r'D:(\d{4})', raw)
        return m.group(1) if m else ''
    except Exception:
        return ''


# ── Metadata parsers ──────────────────────────────────────────────────────────

_ANNAS_RE = re.compile(
    # Title -- Author -- Publisher info -- isbn13 N -- hash -- Anna[']s Archive
    r"^(.+?)\s+--\s+(.+?)\s+--\s+(.+?)\s+--\s+isbn13\s+(\d+)\s+--\s+([a-f0-9]+)\s+--\s+Anna.s\s+Archive$",
    re.IGNORECASE,
)


def _unescape_annas(s: str) -> str:
    """Reverse Anna's Archive filename encoding conventions."""
    # "O_J_" → "O.J."  (initials with underscores → dots)
    s = re.sub(r'\b([A-Z])_([A-Z])_\b', r'\1.\2.', s)
    # Trailing underscore before space or colon → colon  ("Physics_ Sim" → "Physics: Sim")
    s = re.sub(r'_(?=[\s:])', ':', s)
    return s.strip()


def _parse_annas_archive(stem: str) -> Optional[dict]:
    m = _ANNAS_RE.match(stem.strip())
    if not m:
        return None
    title_raw, author_raw, publisher_raw, isbn, checksum = m.groups()
    title  = _unescape_annas(title_raw)
    author = _unescape_annas(author_raw)
    year   = _extract_year(publisher_raw)
    return {
        'title':     title,
        'author':    author,
        'publisher': publisher_raw.strip(),
        'isbn':      isbn,
        'year':      year,
        'checksum':  checksum,
        'source':    'annas_archive',
    }


def _parse_pdf_metadata(path: Path) -> Optional[dict]:
    """Return metadata dict if PDF has non-garbage internal metadata."""
    try:
        import pypdf
        reader = pypdf.PdfReader(str(path), strict=False)
        meta = reader.metadata or {}
        title  = str(meta.get('/Title',  '')).strip()
        author = str(meta.get('/Author', '')).strip()
    except Exception:
        return None

    if _is_garbage_title(title) and _is_garbage_author(author):
        return None

    year = ''
    raw_date = str((reader.metadata or {}).get('/CreationDate', ''))
    m = re.match(r'D:(\d{4})', raw_date)
    if m:
        year = m.group(1)

    return {
        'title':     title  if not _is_garbage_title(title)  else '',
        'author':    author if not _is_garbage_author(author) else '',
        'year':      year,
        'isbn':      '',
        'publisher': '',
        'checksum':  '',
        'source':    'pdf_metadata',
    }


def _parse_generic_stem(stem: str) -> dict:
    """
    Heuristic parser for filenames that follow neither Anna's Archive nor
    standard PDF metadata conventions.

    Handles the common pattern: "Title text _ Author" or "_Title text_ _ Author"
    where ' _ ' separates the title from the author's last name.
    """
    cleaned = stem.strip('_').strip()

    # Split on ' _ ' — last token is treated as author if it looks like a name
    parts = re.split(r'\s+_\s+', cleaned)
    if len(parts) >= 2:
        candidate_author = parts[-1].strip().strip('_').strip()
        title_part       = ' '.join(p.strip('_').strip() for p in parts[:-1])
        # Accept as author if 1–3 words, no digits, no special chars
        if (
            1 <= len(candidate_author.split()) <= 3
            and re.match(r'^[A-Za-z\s.\-]+$', candidate_author)
        ):
            return {
                'title':     title_part,
                'author':    candidate_author,
                'year':      '',
                'isbn':      '',
                'publisher': '',
                'checksum':  '',
                'source':    'filename_heuristic',
            }

    # No author found — use the full cleaned stem as title
    return {
        'title':     cleaned,
        'author':    '',
        'year':      '',
        'isbn':      '',
        'publisher': '',
        'checksum':  '',
        'source':    'filename_heuristic',
    }


# ── Public API ────────────────────────────────────────────────────────────────

def extract_metadata(path: Path) -> dict:
    """
    Extract structured metadata from a corpus file.

    Priority:
      1. Anna's Archive filename format
      2. Non-garbage PDF internal metadata
      3. Generic filename heuristics
    Then fill in the year from the PDF creation date if still missing.
    """
    stem = path.stem

    meta = (
        _parse_annas_archive(stem)
        or _parse_pdf_metadata(path)
        or _parse_generic_stem(stem)
    )

    # Fill year from PDF creation date if not already present
    if not meta.get('year') and path.suffix.lower() == '.pdf':
        meta['year'] = _pdf_creation_year(path)

    return meta


def generate_name(meta: dict, suffix: str = '.pdf') -> str:
    """
    Produce a structured filename from extracted metadata.

    Format:  {lastname}_{year}_{title_slug}{suffix}
    Example: scherer_2017_computational-physics-simulation.pdf
    """
    lastname   = _lastname_slug(meta.get('author', ''))
    year       = meta.get('year', '') or 'unknown'
    title_slug = _to_slug(meta.get('title', ''), max_words=5)
    return f"{lastname}_{year}_{title_slug}{suffix}"


def rename_corpus(
    corpus_dir: Path,
    manifest_path: Path,
    dry_run: bool = False,
) -> list[dict]:
    """
    Rename all supported files in *corpus_dir* to the standard convention.

    Files already matching their target name are skipped (idempotent).
    Returns a list of rename records; also writes the manifest JSON.
    """
    supported = {'.pdf', '.txt', '.md', '.markdown', '.csv'}
    candidates = sorted(
        f for f in corpus_dir.iterdir()
        if f.is_file() and f.suffix.lower() in supported
    )

    # Load any existing manifest so previously-renamed files stay stable.
    # Dry-run records are NOT considered settled — re-process them on a real run.
    # Index by both original name and new name: after renaming, the file exists
    # under its new name and must be found by new_name to avoid re-processing.
    existing_by_original: dict[str, dict] = {}
    existing_by_new: dict[str, dict] = {}
    if manifest_path.exists():
        for r in json.loads(manifest_path.read_text()):
            if r.get('status') != 'dry_run':
                existing_by_original[r['original']] = r
                existing_by_new[r['new_name']]      = r

    taken_names: set[str] = {r['new_name'] for r in existing_by_original.values()}

    # Build the initial records list from the manifest, refreshing status to
    # 'unchanged' when the renamed file is confirmed present on disk.
    records: list[dict] = []
    for r in existing_by_original.values():
        updated = dict(r)
        if (corpus_dir / r['new_name']).exists():
            updated['status'] = 'unchanged'
        records.append(updated)

    for path in candidates:
        if path.name in existing_by_original or path.name in existing_by_new:
            continue  # already processed (original still present) or already renamed in a previous run

        meta     = extract_metadata(path)
        new_name = generate_name(meta, suffix=path.suffix.lower())

        # Deduplicate collisions with a numeric suffix
        base, ext = new_name.rsplit('.', 1)
        counter = 2
        while new_name in taken_names:
            new_name = f"{base}_{counter}.{ext}"
            counter += 1
        taken_names.add(new_name)

        unchanged = path.name == new_name
        status = 'unchanged' if unchanged else ('dry_run' if dry_run else 'renamed')

        record = {
            'original': path.name,
            'new_name': new_name,
            'metadata': meta,
            'status':   status,
        }

        if status == 'renamed':
            path.rename(corpus_dir / new_name)

        records.append(record)

        if unchanged:
            print(f"  [skip]    {path.name}")
        elif dry_run:
            print(f"  [dry-run] {path.name}")
            print(f"            → {new_name}")
        else:
            print(f"  [renamed] {path.name}")
            print(f"            → {new_name}")

    # Always write / update the manifest (even for dry runs — records are marked)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(records, indent=2, ensure_ascii=False))

    return records
