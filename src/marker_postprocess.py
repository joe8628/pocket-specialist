"""Post-processor for raw Marker PDF-to-markdown output.

Applies a fixed-order pipeline of fixers to clean artifact classes
that Marker introduces for any PDF document.
"""
import re
from collections import Counter

# ── Fixer 1: span tags ────────────────────────────────────────────────────────
_SPAN_OPEN_RE = re.compile(r'<span[^>]*>', re.IGNORECASE)
_SPAN_CLOSE_RE = re.compile(r'</span>', re.IGNORECASE)

# ── Fixer 2: double-encoded footnote sups ────────────────────────────────────
# Marker wraps a pre-rendered HTML footnote anchor in another <sup> tag:
# <sup>&</sup>lt;sup>N</sup>. Must be removed before fixer 3.
_DOUBLE_SUP_RE = re.compile(r'<sup>&</sup>lt;sup>\d+</sup>', re.IGNORECASE)

# ── Fixer 3: residual lt;sup>N</sup> text fragments ──────────────────────────
# Left behind after the outer <sup>&</sup> is removed by fixer 2 or fixer 5.
_RESIDUAL_LT_SUP_RE = re.compile(r'lt;sup>\d+</sup>', re.IGNORECASE)

# ── Fixer 4: math-variable sups/subs ─────────────────────────────────────────
# Non-digit content (1-10 chars) → inline LaTeX. Digit-only → fixer 5.
_MATH_SUP_RE = re.compile(r'<sup>(?!\d+</sup>)([^<]{1,10})</sup>', re.IGNORECASE)
_MATH_SUB_RE = re.compile(r'<sub>(?!\d+</sub>)([^<]{1,10})</sub>', re.IGNORECASE)

# ── Fixer 5: footnote-number sups ────────────────────────────────────────────
# Digit-only <sup>N</sup> remaining after fixer 4 ran.
_FOOTNOTE_SUP_RE = re.compile(r'<sup>\d+</sup>', re.IGNORECASE)

# ── Fixer 6: dangling Marker image refs ──────────────────────────────────────
# Empty alt text + local _page_N_ path. Real external image links are preserved.
_MARKER_IMG_RE = re.compile(
    r'!\[\]\(_page_\d+_[^)]+\.(?:jpe?g|png|gif|webp|svg|tiff?)\)',
    re.IGNORECASE,
)

# ── Fixer 7a: OCR artifacts (migrated from ingest._strip_ocr_artifacts) ──────
_BR_RE = re.compile(r'<br\s*/?>', re.IGNORECASE)
# Catch any remaining sup variants not handled by fixers 2-5
_SUP_REMNANT_RE = re.compile(
    r'<sup>(?:&lt;sup&gt;.*?&lt;/sup&gt;|.*?)</sup>', re.IGNORECASE | re.DOTALL
)
_INVISIBLE_RE = re.compile(
    '[\xad'      # soft hyphen U+00AD
    '​'     # zero-width space
    '‌'     # zero-width non-joiner
    '‍'     # zero-width joiner
    '﻿'     # BOM / zero-width no-break space
    ']'
)

# ── Fixer 7b: page artifacts (migrated from ingest._strip_page_artifacts) ────
_PAGE_NUMBER_RE = re.compile(
    r'^\s*(?:Page\s+)?\d{1,4}(?:\s+of\s+\d{1,4})?\s*$',
    re.IGNORECASE,
)
# Local copy — ingest.py keeps its own for heading/code detection there.
_CODE_LINE_RE = re.compile(r'^\s*\d{2,5}\s+[A-Za-z\'`]')


# ── Fixer implementations ─────────────────────────────────────────────────────

def _strip_spans(text: str) -> str:
    text = _SPAN_OPEN_RE.sub('', text)
    text = _SPAN_CLOSE_RE.sub('', text)
    return text


def _fix_double_sup(text: str) -> str:
    return _DOUBLE_SUP_RE.sub('', text)


def _fix_residual_lt_sup(text: str) -> str:
    return _RESIDUAL_LT_SUP_RE.sub('', text)


def _fix_math_sups(text: str) -> str:
    text = _MATH_SUP_RE.sub(lambda m: f'$^{{{m.group(1)}}}$', text)
    text = _MATH_SUB_RE.sub(lambda m: f'$_{{{m.group(1)}}}$', text)
    return text


def _strip_footnote_sups(text: str) -> str:
    return _FOOTNOTE_SUP_RE.sub('', text)


def _strip_image_refs(text: str) -> str:
    return _MARKER_IMG_RE.sub('', text)


def _strip_ocr_artifacts(text: str) -> str:
    text = _SUP_REMNANT_RE.sub('', text)
    text = _BR_RE.sub(' ', text)
    text = _INVISIBLE_RE.sub('', text)
    text = text.replace('\xa0', ' ')   # non-breaking space → regular space
    return text


def _strip_page_artifacts(text: str) -> str:
    lines = text.splitlines()
    total = len(lines)

    freq: Counter[str] = Counter()
    for line in lines:
        s = line.strip()
        if s and len(s) < 72 and not _CODE_LINE_RE.match(s):
            freq[s] += 1

    rh_threshold = max(3, total // 30)
    running_headers: set[str] = set()
    for line_text, count in freq.items():
        if count >= rh_threshold:
            if re.search(r'[\\|`${}]', line_text):
                continue
            running_headers.add(line_text)

    cleaned: list[str] = []
    for line in lines:
        s = line.strip()
        if _PAGE_NUMBER_RE.match(s):
            continue
        if s in running_headers:
            continue
        cleaned.append(line)

    return '\n'.join(cleaned)


# ── Public API ────────────────────────────────────────────────────────────────

def clean(text: str) -> str:
    """Clean raw Marker markdown output. Returns cleaned markdown."""
    text = _strip_spans(text)
    text = _fix_double_sup(text)
    text = _fix_residual_lt_sup(text)
    text = _fix_math_sups(text)
    text = _strip_footnote_sups(text)
    text = _strip_image_refs(text)
    text = _strip_ocr_artifacts(text)
    text = _strip_page_artifacts(text)
    return text
