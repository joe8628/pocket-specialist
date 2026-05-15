"""Stage 4: Llama 3.1 8B Q4_K_M correction pass — structured OCR → clean Markdown.

STUB — not yet implemented.
"""
from __future__ import annotations
from pathlib import Path

from pipeline.models import TextBlock


def build_prompt(blocks: list[TextBlock]) -> str:
    """Serialize a page's blocks into a structured LLM prompt (Option B format)."""
    raise NotImplementedError("Stage 4 (Correction) is not yet implemented.")


def correct_page(page_num: int, blocks: list[TextBlock], llm) -> str:  # noqa: ANN001
    """Run LLM correction on one page. Returns corrected Markdown string."""
    raise NotImplementedError("Stage 4 (Correction) is not yet implemented.")


def correction_all(
    equations_dir: Path,
    model_path: Path,
    start_page: int = 1,
    end_page: int | None = None,
    batch_size: int = 4,
    context_tokens: int = 4096,
) -> None:
    """Load LLM once, correct all pages in batches; write .md files and update checkpoints."""
    raise NotImplementedError("Stage 4 (Correction) is not yet implemented.")
