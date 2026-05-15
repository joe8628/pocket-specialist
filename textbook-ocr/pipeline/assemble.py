"""Stage 5: Assemble corrected per-page Markdown into final .md and .json outputs.

STUB — not yet implemented.
"""
from __future__ import annotations
from pathlib import Path

from pipeline.models import OutputManifest


def assemble(
    corrected_dir: Path,
    output_dir: Path,
    source_pdf: Path,
) -> OutputManifest:
    """Concatenate per-page Markdown and build the structured JSON manifest."""
    raise NotImplementedError("Stage 5 (Assemble) is not yet implemented.")
