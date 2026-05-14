"""Post-processor for raw Marker PDF-to-markdown output.

Applies a fixed-order pipeline of fixers to clean artifact classes
that Marker introduces for any PDF document.
"""
import re

_SPAN_OPEN_RE = re.compile(r'<span[^>]*>', re.IGNORECASE)
_SPAN_CLOSE_RE = re.compile(r'</span>', re.IGNORECASE)

# Fixer 2: double-encoded footnote sup — <sup>&</sup>lt;sup>N</sup>
# Marker wraps a pre-rendered HTML footnote anchor in another <sup> tag.
_DOUBLE_SUP_RE = re.compile(r'<sup>&</sup>lt;sup>\d+</sup>', re.IGNORECASE)

# Fixer 3: residual lt;sup>N</sup> text left after outer <sup>&</sup> removal
_RESIDUAL_LT_SUP_RE = re.compile(r'lt;sup>\d+</sup>', re.IGNORECASE)

# Fixer 4: math-variable sups/subs — non-digit content, 1-10 chars.
# Digit-only content is a footnote number, handled by fixer 5.
_MATH_SUP_RE = re.compile(r'<sup>(?!\d+</sup>)([^<]{1,10})</sup>', re.IGNORECASE)
_MATH_SUB_RE = re.compile(r'<sub>(?!\d+</sub>)([^<]{1,10})</sub>', re.IGNORECASE)

# Fixer 5: remaining footnote-number sups (digit-only, after fixer 4 ran)
_FOOTNOTE_SUP_RE = re.compile(r'<sup>\d+</sup>', re.IGNORECASE)

# Fixer 6: dangling Marker image refs — empty alt text, local _page_N_ path.
# Only matches Marker-generated local paths; preserves real external image links.
_MARKER_IMG_RE = re.compile(
    r'!\[\]\(_page_\d+_[^)]+\.(?:jpe?g|png|gif|webp|svg|tiff?)\)',
    re.IGNORECASE,
)


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


def clean(text: str) -> str:
    """Clean raw Marker markdown output. Returns cleaned markdown."""
    text = _strip_spans(text)
    text = _fix_double_sup(text)
    text = _fix_residual_lt_sup(text)
    text = _fix_math_sups(text)
    text = _strip_footnote_sups(text)
    text = _strip_image_refs(text)
    return text
