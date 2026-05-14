"""Embed documents and store them in ChromaDB."""
import sys
import re
import csv
import io
import json
from pathlib import Path

from chromadb.api.types import Metadata
from db import client, embedding_fn, REPO_ROOT, DB_PATH
import graph as kg
from marker_postprocess import clean as _marker_clean

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
    # Must be set before surya.foundation is imported: FoundationPredictor reads
    # settings.RECOGNITION_BATCH_SIZE as a class-level attribute at definition time.
    # Default of 256 pre-allocates a 3.56 GB KV cache; 32 keeps peak VRAM ~7 GB
    # on an 11 GB card (RTX 2080 Ti) instead of crashing the GPU driver at ~10.5 GB.
    from surya.settings import settings as _surya_settings
    _surya_settings.RECOGNITION_BATCH_SIZE = 256
    # batch=36 at 1200x1200 needs 3.09 GB for a single 512-ch feature map — OOM.
    # batch=24 needs 2.06 GB — still OOM (8.06 GB allocated + 2.06 GB > 10.55 GB).
    # batch=16 needs ~1.37 GB, leaving ~2.3 GB headroom — safe on RTX 2080 Ti.
    _surya_settings.DETECTOR_BATCH_SIZE = 16
    print(f"  Loading Marker models on {torch.cuda.get_device_name(0)} (first run only)...")
    from marker.converters.pdf import PdfConverter
    from marker.models import create_model_dict
    _marker_converter = PdfConverter(artifact_dict=create_model_dict())
    return _marker_converter

CORPUS_PATH = REPO_ROOT / "RAG-corpus"
MARKER_CACHE_PATH = REPO_ROOT / "marker_cache"
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

# Single-word/short section titles that Marker emits without ATX markers or numbering
_SECTION_HEADING_NAMES_RE = re.compile(
    r'^(?:references?|bibliography|acknowledgements?|acknowledgments?|'
    r'further\s+reading|preface|foreword|abstract|appendix\b|index|'
    r'contents?|notation|glossary)\s*$',
    re.IGNORECASE,
)

# Boilerplate sections to drop entirely (not worth indexing)
_BOILERPLATE_HEADING_RE = re.compile(
    r'^(?:references?|bibliography|acknowledgements?|acknowledgments?|'
    r'further\s+reading|index|'
    # Journal delivery footers (AIP, APS, etc.)
    r'articles?\s+you\s+may\s+be\s+interested\s+in|'
    r'related\s+content|cited\s+by|'
    r'recommended\s+articles?|'
    r'supplementary\s+(?:material|data|information)|'
    r'author\s+(?:information|affiliations?|contributions?)|'
    r'funding\s+information|'
    r'conflict(?:s)?\s+of\s+interest|'
    r'data\s+availability)\s*$',
    re.IGNORECASE,
)

# Bullet/numbered-list TOC entry: "  3. Chapter Name .... 47" or "  • Section"
_TOC_ENTRY_RE = re.compile(
    r'^\s*(?:[\d]+\.[\d.]*\s+\S|\•|\–|\-)\s*.{3,60}(?:\.{3,}|\s{3,})\s*\d{1,4}\s*$'
)

# Matches BASIC/Fortran line-numbered code: "1234 PRINT X" or "10 FOR I=1 TO N"
_CODE_LINE_RE = re.compile(r'^\s*\d{2,5}\s+[A-Za-z\'`]')

_MIN_PROSE_WORDS = 25
_MEANINGFUL_LATEX_RE = re.compile(r'\\(?:frac|int|sum|prod|partial|nabla|cdot|times|alpha|beta|gamma|delta|sigma|omega|lambda|mu|pi|theta|phi|psi|hat|bar|vec|mathbf|mathrm)\b|\$\$')


def _is_boilerplate_heading(heading: str) -> bool:
    """True for section headings whose content should be dropped entirely."""
    clean = re.sub(r'[\*_`#]', '', heading).strip()
    return bool(_BOILERPLATE_HEADING_RE.match(clean))


def _is_heading(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    # Never treat BASIC/Fortran line-numbered code as a heading
    if _CODE_LINE_RE.match(stripped):
        return False
    # Markdown ATX headings — with rejection guards for non-heading content
    if re.match(r'^#{1,6}\s+\S', stripped):
        content = re.sub(r'^#{1,6}\s+', '', stripped)
        # Display math masquerading as ATX heading (e.g. "# $$\frac{a}{b}$$")
        if content.startswith('$$') or content.startswith('\\['):
            return False
        # Binary/hex literals and pure-symbol lines with no real words
        if not re.search(r'[a-zA-Z]{3,}', content):
            return False
        # Algorithm pseudocode labels that Marker ATX-encodes: "# Step 1:", "# Output:"
        # These look like headings but are indented code structure — detected by the
        # surrounding fence context in _smart_chunk; reject bare keyword-colon lines.
        if re.match(r'^(?:input|output|step\s*\d|procedure|function|return|if|else|end)\s*[:\d]?$', content, re.IGNORECASE):
            return False
        return True
    if len(stripped) > 80:
        return False
    if _HEADING_RE.match(stripped):
        return True
    # Plain single-word section names Marker emits without ATX markers
    plain = re.sub(r'[\*_`#]', '', stripped).strip()
    if _SECTION_HEADING_NAMES_RE.match(plain):
        return True
    # ALL CAPS short line with at least 4 alpha chars (chapter/section titles)
    alpha = [c for c in stripped if c.isalpha()]
    if len(alpha) >= 4 and len(stripped) < 60 and stripped.upper() == stripped:
        return True
    return False


def _merge_code_runs(lines: list[str]) -> list[str]:
    """Collapse consecutive BASIC/Fortran line-numbered code lines into one fenced block."""
    result: list[str] = []
    code_buf: list[str] = []

    def flush_code():
        if code_buf:
            result.append("```\n" + "\n".join(code_buf) + "\n```")
            code_buf.clear()

    for line in lines:
        if _CODE_LINE_RE.match(line):
            code_buf.append(line.rstrip())
        else:
            flush_code()
            result.append(line)
    flush_code()
    return result


def _prose_word_count(text: str) -> int:
    """Count words in prose lines, ignoring text inside fenced code blocks."""
    in_fence = False
    count = 0
    for line in text.splitlines():
        if line.strip().startswith('```'):
            in_fence = not in_fence
            continue
        if not in_fence:
            count += len(re.findall(r'[a-zA-Z]{3,}', line))
    return count


def _is_table_row_dominated(text: str) -> bool:
    """True when the majority of lines are markdown pipe-table rows (TOC artifacts)."""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if len(lines) < 2:
        return False
    pipe_lines = sum(1 for l in lines if l.startswith('|'))
    return pipe_lines / len(lines) >= 0.5


def _is_toc_chunk(text: str) -> bool:
    """True when the majority of lines are TOC entries (dotted leaders + page numbers)."""
    lines = [l for l in text.splitlines() if l.strip()]
    if len(lines) < 3:
        return False
    toc_lines = sum(1 for l in lines if _TOC_ENTRY_RE.match(l))
    return toc_lines / len(lines) >= 0.5


_URL_RE = re.compile(r'^\s*(?:https?://|www\.)\S+\s*$')


def _is_url_dominated(text: str) -> bool:
    """True when most non-empty lines are bare URLs (reference/link-dump artifacts)."""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if len(lines) < 2:
        return False
    url_lines = sum(1 for l in lines if _URL_RE.match(l))
    return url_lines / len(lines) >= 0.6


def _is_meaningful_chunk(text: str) -> bool:
    """Return False for chunks that are pure noise (no prose and no meaningful math)."""
    if _is_table_row_dominated(text):
        return False
    if _is_toc_chunk(text):
        return False
    if _is_url_dominated(text):
        return False
    prose_words = _prose_word_count(text)
    if prose_words >= _MIN_PROSE_WORDS:
        return True
    # Short math-heavy chunks are OK if they contain real LaTeX notation
    return bool(_MEANINGFUL_LATEX_RE.search(text)) and prose_words >= 5


def _split_sentences(text: str) -> list[str]:
    # Treat display math blocks as atomic — never split inside $$...$$
    if text.strip().startswith('$$'):
        return [text.strip()]
    parts = re.split(r'(?<=[.!?])\s+', text.strip())
    return [p.strip() for p in parts if p.strip()]


# Sentence-opening patterns that signal a mid-derivation context orphan.
# These phrases only make sense when the predecessor sentence/equation is present.
_ORPHAN_OPENING_RE = re.compile(
    r'^(?:'
    # Bare equation back-references: "(38)," "(3.14);" "(A.2),"
    r'\([\d.A-Za-z]+\)\s*[,;]'
    # Named equation references: "Eq. (5)", "Eqs. (3)–(5)"
    r'|[Ee]q(?:uation)?s?\.?\s*\('
    # Anaphoric connectives that open a result sentence
    r'|(?:therefore|thus|hence|consequently|accordingly),?\s'
    # Anaphoric pronoun + verb (optional intervening noun): "This gives", "These results show", "Its value is"
    r'|(?:this|these|those|its|their)\s+(?:\w+\s+)?(?:gives?|shows?|implies?|is\b|are\b|means?|yields?|results?)'
    # Math-relative clauses that anchor to a preceding expression
    r'|where\s+[A-Za-z_]\w*\s+(?:is|are|denotes?|represents?)'
    r'|with\s+[A-Za-z_]\w*\s+(?:being|denoting)'
    r'|in\s+which\s'
    # In-formula continuations: "into (38)", "from (3.2)", "substituting into ("
    r'|into\s*\('
    r'|from\s+\('
    r'|substitut\w+\s+(?:into\s+)?\('
    r')',
    re.IGNORECASE,
)


def _merge_orphan_openers(chunks: list[str], max_words: int = 500) -> list[str]:
    """Backward-merge chunks whose first sentence is an orphaned back-reference.

    Two strategies depending on combined size:
      1. Full merge: ≤ 120% of max_words → concatenate predecessor and orphan.
      2. Context prepend: too large → prefix the orphan with the predecessor's
         last sentence, giving the minimum context to interpret the reference.
    Operates within a single section's chunk list; cross-section orphans are
    out of scope.
    """
    if len(chunks) <= 1:
        return chunks

    result = [chunks[0]]
    for chunk in chunks[1:]:
        sentences = _split_sentences(chunk)
        first = sentences[0].strip() if sentences else ""
        if first and _ORPHAN_OPENING_RE.match(first):
            prev = result[-1]
            if len((prev + " " + chunk).split()) <= int(max_words * 1.2):
                result[-1] = prev + " " + chunk
                continue
            # Too large to fully merge — prepend predecessor's last sentence
            prev_sentences = _split_sentences(prev)
            if len(prev_sentences) >= 2:
                chunk = prev_sentences[-1] + " " + chunk
        result.append(chunk)
    return result


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

    chunks = [c for c in chunks if c.strip()]
    return _merge_orphan_openers(chunks, max_words)


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
        if _is_boilerplate_heading(heading):
            continue
        # Strip trailing page numbers fused onto headings by the printer
        # e.g. "6.1 Root Finding 103" → "6.1 Root Finding"
        heading = re.sub(r'\s+\d{2,4}$', '', heading).strip()
        merged_lines = _merge_code_runs(lines)
        paragraphs = [
            p.strip()
            for p in re.split(r'\n\s*\n', "\n".join(merged_lines))
            if p.strip()
        ]
        if not paragraphs:
            continue
        for chunk in _merge_into_chunks(paragraphs, max_words, overlap):
            body = f"{heading}\n{chunk}" if heading else chunk
            if _is_meaningful_chunk(body):
                result.append((heading, body))

    return result


def _verify_cache_content(pdf_path: Path, cache_file: Path, raw: str) -> None:
    """Warn if the cached markdown has no word overlap with the PDF filename stem.

    Catches source-content swaps where a misaligned cache file gets written
    (Escande/Fujita-style bug): the stem words of the PDF name should appear
    somewhere in the first 200 lines of the extracted text.
    """
    stem_words = {w.lower() for w in re.findall(r'[a-zA-Z]{4,}', pdf_path.stem)}
    if not stem_words:
        return
    head = "\n".join(raw.splitlines()[:200]).lower()
    overlap = sum(1 for w in stem_words if w in head)
    if overlap == 0:
        print(
            f"  Warning: cache content may be misaligned — none of the filename words "
            f"({', '.join(sorted(stem_words)[:5])}) appear in the extracted text of "
            f"{cache_file.name}. Check for a source-content swap."
        )


def _marker_cache_file(pdf_path: Path) -> Path:
    """Return the cache path for a PDF's raw Marker output."""
    rel = pdf_path.relative_to(CORPUS_PATH)
    # Flatten subdirectory separators so the cache stays a flat directory
    name = str(rel.with_suffix('.md')).replace('/', '__').replace('\\', '__')
    return MARKER_CACHE_PATH / name


def _extract_pdf(path: Path) -> str:
    """Extract PDF as Markdown via Marker, caching the raw output to marker_cache/.

    Marker only runs when the PDF is newer than its cached markdown. The cache
    stores the unmodified Marker output — useful for inspecting OCR quality and
    tuning the cleaning/chunking pipeline without re-running the GPU.

    Raises RuntimeError for encrypted/password-protected PDFs so the caller can
    skip the file with a clear message rather than a cryptic PDFium traceback.
    """
    cache_file = _marker_cache_file(path)
    pdf_mtime = path.stat().st_mtime

    if cache_file.exists() and cache_file.stat().st_mtime >= pdf_mtime:
        print(f"    (from cache)", end=" ", flush=True)
        raw = cache_file.read_text(encoding='utf-8')
    else:
        try:
            converter = _get_marker_converter()
            rendered = converter(str(path))
            raw = rendered.markdown
        except Exception as exc:
            msg = str(exc)
            if "security scheme" in msg.lower() or "encrypted" in msg.lower() or "password" in msg.lower():
                raise RuntimeError(
                    f"PDF is encrypted or password-protected — cannot extract. "
                    f"Decrypt the file and re-ingest. (Original: {msg})"
                ) from exc
            raise
        MARKER_CACHE_PATH.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(raw, encoding='utf-8')
        _verify_cache_content(path, cache_file, raw)
        print(f"    cached → {cache_file.name}")

    return _marker_clean(raw)


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


# ── metadata helpers ──────────────────────────────────────────────────────────

_PDF_DATE_RE = re.compile(r"D:(\d{4})(\d{2})(\d{2})")

# Titles that are clearly word-processor artifacts, not real document titles.
# \b is placed inside each alternative (not after the group) so dash-terminated
# patterns like "Microsoft Word -" don't fail the boundary check.
_PLACEHOLDER_TITLE_RE = re.compile(
    r'^(?:'
    r'microsoft\s+word\s*[-–—]'      # "Microsoft Word - Document1"
    r'|untitled\b'                    # "Untitled"
    r'|document\s*\d*\b'             # "Document", "Document1"  (not "Documentation")
    r'|new\s+document\b'
    r'|temp(?:orary)?\b'
    r'|draft\b'
    r'|\.docx?\b'
    r'|presentation\s*\d*\b'
    r'|workbook\s*\d*\b'
    r'|copy\s+of\b'
    r'|revision\s+\d'
    r')',
    re.IGNORECASE,
)
# Authors that are clearly placeholder/garbage: 1-3 chars, all digits, or common filler words
_PLACEHOLDER_AUTHOR_RE = re.compile(
    r'^(?:[a-z]{1,3}|unknown|author|user|admin|default|n/?a|na|\d+)\s*$',
    re.IGNORECASE,
)


# Anna's Archive filename: "Title -- Author -- Publisher -- isbn13 X -- hash -- Anna's Archive"
_ANNAS_ARCHIVE_RE = re.compile(
    r"^(.+?)\s+--\s+(.+?)\s+--\s+.+--\s+Anna's\s+Archive$",
    re.IGNORECASE,
)


def _parse_annas_archive_stem(stem: str) -> tuple[str, str]:
    """Return (title, author) parsed from an Anna's Archive PDF filename stem, or ('', '')."""
    m = _ANNAS_ARCHIVE_RE.match(stem.strip())
    if not m:
        return "", ""

    def _unescape(s: str) -> str:
        # "O_J_" style initials → "O.J."
        s = re.sub(r'\b([A-Z])_([A-Z])_\b', r'\1.\2.', s)
        # Trailing underscore before a space → colon+space: "Physics_ Sim" → "Physics: Sim"
        s = re.sub(r'_(?=\s)', ':', s)
        return s.strip()

    return _unescape(m.group(1)), _unescape(m.group(2))


def _parse_pdf_date(raw: str) -> str:
    """Convert PDF date string 'D:YYYYMMDDHHmmss...' to 'YYYY-MM-DD', or ''."""
    m = _PDF_DATE_RE.match(raw or "")
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else ""


def _pdf_doc_metadata(path: Path) -> dict:
    """Extract title, author, page_count, creation_date from PDF without GPU."""
    try:
        import pypdf
        reader = pypdf.PdfReader(str(path), strict=False)
        meta = reader.metadata or {}

        raw_title = str(meta.get("/Title", "")).strip()
        if raw_title and _PLACEHOLDER_TITLE_RE.match(raw_title):
            print(f"  Warning: ignoring placeholder PDF title '{raw_title}' for {path.name}")
            raw_title = ""

        raw_author = str(meta.get("/Author", "")).strip()
        if raw_author and _PLACEHOLDER_AUTHOR_RE.match(raw_author):
            print(f"  Warning: ignoring placeholder PDF author '{raw_author}' for {path.name}")
            raw_author = ""

        # When embedded metadata is missing/garbage, try parsing from the filename
        if not raw_title or not raw_author:
            fn_title, fn_author = _parse_annas_archive_stem(path.stem)
            if not raw_title and fn_title:
                raw_title = fn_title
            if not raw_author and fn_author:
                raw_author = fn_author

        return {
            "title": raw_title or path.stem,
            "author": raw_author,
            "page_count": len(reader.pages),
            "creation_date": _parse_pdf_date(str(meta.get("/CreationDate", ""))),
        }
    except Exception:
        return {"title": path.stem, "author": "", "page_count": 0, "creation_date": ""}


def _file_metadata(path: Path, fmt: str) -> dict:
    """Base provenance metadata common to all document types."""
    stat = path.stat()
    base = {
        "source": str(path.relative_to(REPO_ROOT)),
        "filename": path.name,
        "doc_type": fmt,
        "mtime": stat.st_mtime,
    }
    if fmt == "pdf":
        base.update(_pdf_doc_metadata(path))
    else:
        base.update({"title": path.stem, "author": "", "page_count": 0, "creation_date": ""})
    return base


# ── ingestion ─────────────────────────────────────────────────────────────────

def ingest_texts(
    documents: list[str],
    ids: list[str],
    metadatas: list[Metadata] | None = None,
    collection_name: str = "default",
) -> int:
    collection = client.get_or_create_collection(collection_name, embedding_function=embedding_fn)
    collection.upsert(documents=documents, ids=ids, metadatas=metadatas)
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

    base_meta = _file_metadata(path, fmt)
    chunks = []
    ids = []
    metadatas: list[Metadata] = []
    for i, (heading, text) in enumerate(chunk_pairs):
        chunks.append(text)
        ids.append(f"{path.stem}__ch{i}")
        metadatas.append({**base_meta, "heading": heading, "chunk_index": i})

    collection = client.get_or_create_collection(collection_name, embedding_function=embedding_fn)
    collection.upsert(documents=chunks, ids=ids, metadatas=metadatas)
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

    new_count = changed_count = skipped_count = cached_count = 0

    for file in sorted(candidates):
        key = str(file.relative_to(REPO_ROOT))
        mtime = file.stat().st_mtime
        db_current = key in manifest and manifest[key] >= mtime

        if db_current:
            # DB is up to date — but write the marker cache if it is missing
            if _sniff_format(file) == 'pdf':
                cache_file = _marker_cache_file(file)
                if not cache_file.exists():
                    print(f"  [cache] {file.name} ...", end=" ", flush=True)
                    try:
                        _extract_pdf(file)
                        cached_count += 1
                        print("cached.")
                    except Exception as e:
                        print(f"failed ({e})")
                    continue
            skipped_count += 1
            continue

        status = "new" if key not in manifest else "changed"
        if status == "new":
            new_count += 1
        else:
            changed_count += 1

        print(f"  [{status}] {file.name} ...", end=" ", flush=True)
        try:
            n = ingest_file(file, collection_name)
            manifest[key] = mtime
            print(f"{n} chunks ingested.")
        except Exception as e:
            print(f"failed ({e})")

    _save_manifest(manifest)
    parts = [f"{new_count} new", f"{changed_count} updated",
             f"{skipped_count} skipped"]
    if cached_count:
        parts.append(f"{cached_count} cache-only")
    print(f"\nDone. {', '.join(parts)}.")


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
