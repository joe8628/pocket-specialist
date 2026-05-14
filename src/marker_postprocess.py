"""Post-processor for raw Marker PDF-to-markdown output.

Applies a fixed-order pipeline of fixers to clean artifact classes
that Marker introduces for any PDF document.
"""
import re


def clean(text: str) -> str:
    """Clean raw Marker markdown output. Returns cleaned markdown."""
    return text
