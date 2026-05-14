"""Post-processor for raw Marker PDF-to-markdown output.

Applies a fixed-order pipeline of fixers to clean artifact classes
that Marker introduces for any PDF document.
"""
import re

_SPAN_OPEN_RE = re.compile(r'<span[^>]*>', re.IGNORECASE)
_SPAN_CLOSE_RE = re.compile(r'</span>', re.IGNORECASE)


def _strip_spans(text: str) -> str:
    text = _SPAN_OPEN_RE.sub('', text)
    text = _SPAN_CLOSE_RE.sub('', text)
    return text


def clean(text: str) -> str:
    """Clean raw Marker markdown output. Returns cleaned markdown."""
    text = _strip_spans(text)
    return text
