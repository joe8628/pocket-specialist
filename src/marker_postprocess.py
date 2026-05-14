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


def _strip_spans(text: str) -> str:
    text = _SPAN_OPEN_RE.sub('', text)
    text = _SPAN_CLOSE_RE.sub('', text)
    return text


def _fix_double_sup(text: str) -> str:
    return _DOUBLE_SUP_RE.sub('', text)


def _fix_residual_lt_sup(text: str) -> str:
    return _RESIDUAL_LT_SUP_RE.sub('', text)


def clean(text: str) -> str:
    """Clean raw Marker markdown output. Returns cleaned markdown."""
    text = _strip_spans(text)
    text = _fix_double_sup(text)
    text = _fix_residual_lt_sup(text)
    return text
