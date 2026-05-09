"""Embed documents and store them in ChromaDB using olmOCR for PDF extraction.

olmOCR processes each PDF page through a vision-language model (allenai/olmOCR-7B-0725-FP8),
so it requires a CUDA GPU with ≥15 GB VRAM and poppler (pdftoppm/pdfinfo) installed.
Non-PDF formats (txt, md, csv) are read directly without the VLM.
"""
import sys
import re
import csv
import io
import json
import base64
from io import BytesIO
from pathlib import Path

from db import client, embedding_fn, REPO_ROOT, DB_PATH
import graph as kg

CORPUS_PATH = REPO_ROOT / "RAG-corpus"
MANIFEST_PATH = DB_PATH / ".manifest-olmocr.json"
SUPPORTED = {".pdf", ".txt", ".md", ".markdown", ".csv"}

MODEL_NAME = "allenai/olmOCR-7B-0725-FP8"
EXTRACTOR_META = {"extractor": "olmocr"}

_model = None
_processor = None


def _get_model_and_processor():
    global _model, _processor
    if _model is not None:
        return _model, _processor

    import torch
    if not torch.cuda.is_available():
        raise RuntimeError(
            "olmOCR requires a CUDA-capable GPU with ≥15 GB VRAM. "
            "Check that your NVIDIA driver and PyTorch CUDA versions are compatible."
        )
    gpu_mem_gb = torch.cuda.get_device_properties(0).total_memory / 1024**3
    if gpu_mem_gb < 15:
        raise RuntimeError(
            f"olmOCR needs ≥15 GB GPU VRAM; found {gpu_mem_gb:.1f} GB on "
            f"{torch.cuda.get_device_name(0)}."
        )

    from transformers import AutoProcessor, Qwen2_5_VLForConditionalGeneration

    print(f"  Loading olmOCR model on {torch.cuda.get_device_name(0)} (first run only)...")
    try:
        model = Qwen2_5_VLForConditionalGeneration.from_pretrained(
            MODEL_NAME,
            torch_dtype=torch.bfloat16,
            device_map="auto",
            attn_implementation="flash_attention_2",
        ).eval()
    except (ImportError, ValueError):
        # flash_attention_2 may not be installed; fall back to eager attention
        model = Qwen2_5_VLForConditionalGeneration.from_pretrained(
            MODEL_NAME,
            torch_dtype=torch.bfloat16,
            device_map="auto",
        ).eval()

    processor = AutoProcessor.from_pretrained(MODEL_NAME)
    _model, _processor = model, processor
    return model, processor


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
    if text.strip().startswith('$$'):
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


def _extract_page(pdf_path: str, page_num: int, model, processor) -> str:
    """Run olmOCR on a single PDF page and return extracted natural text."""
    import torch
    from PIL import Image
    from olmocr.data.renderpdf import render_pdf_to_base64png
    from olmocr.prompts import PageResponse, build_no_anchoring_yaml_prompt
    from olmocr.train.front_matter import FrontMatterParser

    image_base64 = render_pdf_to_base64png(pdf_path, page_num, target_longest_image_dim=1024)
    prompt = build_no_anchoring_yaml_prompt()

    messages = [{
        "role": "user",
        "content": [
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_base64}"}},
            {"type": "text", "text": prompt},
        ],
    }]

    text_input = processor.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    main_image = Image.open(BytesIO(base64.b64decode(image_base64)))

    inputs = processor(text=[text_input], images=[main_image], padding=True, return_tensors="pt")
    device = next(model.parameters()).device
    inputs = {k: v.to(device) for k, v in inputs.items()}

    MAX_NEW_TOKENS = 3000
    with torch.no_grad():
        output = model.generate(
            **inputs,
            temperature=0.1,
            max_new_tokens=MAX_NEW_TOKENS,
            num_return_sequences=1,
            do_sample=True,
        )

    prompt_length = inputs["input_ids"].shape[1]
    new_tokens = output[:, prompt_length:]
    text_output = processor.tokenizer.batch_decode(new_tokens, skip_special_tokens=True)[0]

    parser = FrontMatterParser(front_matter_class=PageResponse)
    front_matter, body = parser._extract_front_matter_and_text(text_output)
    try:
        page_response = parser._parse_front_matter(front_matter, body)
        return page_response.natural_text or ""
    except Exception:
        # If YAML front matter is malformed, return the raw body text
        return body


def _extract_pdf(path: Path) -> str:
    """Extract all pages of a PDF using olmOCR and concatenate as plain text."""
    from pypdf import PdfReader

    model, processor = _get_model_and_processor()

    num_pages = len(PdfReader(str(path)).pages)
    pages: list[str] = []

    for page_num in range(1, num_pages + 1):
        print(f"    page {page_num}/{num_pages}", end=" ", flush=True)
        try:
            text = _extract_page(str(path), page_num, model, processor)
            if text:
                pages.append(text)
            print("ok", end=" ", flush=True)
        except Exception as e:
            print(f"(skip: {e})", end=" ", flush=True)

    print()
    return "\n\n".join(pages)


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
    """Get or create a collection tagged with the olmocr extractor metadata."""
    existing = {c.name for c in client.list_collections()}
    if collection_name in existing:
        col = client.get_collection(collection_name, embedding_function=embedding_fn)
        other = col.metadata and col.metadata.get("extractor") not in (None, "olmocr")
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

    file_path = Path(args[0])
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
