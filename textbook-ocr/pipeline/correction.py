"""Stage 4: Ollama LLM correction pass — structured OCR blocks → clean Markdown.

Input:  checkpoints/equations/page_{N:04d}.json  (from Stage 3)
Output: checkpoints/corrected/page_{N:04d}.md    (clean Markdown per page)
"""
from __future__ import annotations
import json
import re
import sys
from pathlib import Path
from typing import Optional

import requests

from config import CORRECTION_DIR, EQUATIONS_DIR
from pipeline.checkpoint import get_status, init_db, set_status, should_process
from pipeline.models import BlockType, TextBlock


_OLLAMA_BASE = "http://localhost:11434"
_OLLAMA_MODEL = "qwen2.5:7b"
_OLLAMA_NUM_CTX = 8192

_RE_HTML = re.compile(r"<[^>]+>")
_RE_SECTION_NUM = re.compile(r'^(?:Chapter\s+)?\d+(?:\.\d+)*$', re.IGNORECASE)
_RE_OUTER_FENCE = re.compile(r'^```\w*\n(.*)\n```$', re.DOTALL)
_RE_DEEP_SECTION_NUM = re.compile(r'^\d+(?:\.\d+){3,}(?:\s|$)')

_SYSTEM_PROMPT = (
    "You are a precise technical document formatter. "
    "You receive OCR-extracted blocks from a physics textbook page; "
    "each line is prefixed with its semantic block type.\n\n"
    "HARD CONSTRAINTS — no exceptions:\n"
    "1. Output ONLY valid Markdown. NEVER wrap the entire response in a code fence.\n"
    "2. NEVER add, invent, complete, or paraphrase any content. "
    "Reproduce only what is given.\n"
    "3. [EQUATION]: emit the LaTeX VERBATIM inside $$...$$. "
    "Do NOT add rows, terms, symbols, or close unclosed environments.\n\n"
    "Block-type rules:\n"
    "- [HEADING]         → heading level by depth:\n"
    "    'Chapter N' or single integer → ##\n"
    "    'N.M' (one dot) → ##\n"
    "    'N.M.P' (two dots) → ###\n"
    "    'N.M.P.Q' or deeper (three+ dots) → ####\n"
    "    No number, or bold phrase not matching the above → plain paragraph (**not** a heading)\n"
    "    NEVER promote a [TEXT] block to a heading regardless of its content.\n"
    "- [TEXT]            → plain paragraph. "
    "NEVER a heading, even as the first or only block on the page.\n"
    "- [EQUATION]        → $$<latex verbatim>$$\n"
    "- [EQUATION_FAILED] → $$% OCR failed\\n<raw text, HTML stripped>$$\n"
    "- [LIST_ITEM]       → `- ...` (strip leading •, -, * characters)\n"
    "- [FIGURE]          → `> [Figure]` on its own line\n"
    "- [FIGURE+CAPTION]  → `> [Figure]` on its own line, then caption as a plain paragraph on the next line\n"
    "- [CAPTION]         → caption as a plain paragraph on its own line\n"
    "- [TABLE]           → Markdown table if parseable, else fenced code block\n"
    "- [FOOTNOTE]        → [^1]: <text> (markdown footnote; number incrementally per page)\n"
    "- [UNKNOWN]         → apply in order:\n"
    "    a) Starts with a digit immediately followed by a letter (e.g. '2There...', '1Also...') — OCR footnote: emit as plain paragraph\n"
    "    b) Matches exactly a section-number pattern (digits/dots, e.g. '7.3.2'): heading\n"
    "    c) Looks like code/pseudocode: fenced code block; "
    "merge consecutive code [UNKNOWN] blocks into one fence\n"
    "    d) Otherwise: plain paragraph\n\n"
    "Additional:\n"
    "- Strip all HTML tags from raw text before emitting.\n"
    "- No preamble, no trailing notes.\n\n"
    "FIGURE FORMAT EXAMPLE (follow exactly):\n"
    "  Input:  [FIGURE+CAPTION] Fig. 3.1. Electron density as a function of radius.\n"
    "  Output: > [Figure]\n"
    "          Fig. 3.1. Electron density as a function of radius.\n"
    "  (Caption is a plain paragraph immediately below. NEVER on the same line as > [Figure].)\n\n"
    "CRITICAL: NEVER emit block-type tags ([TEXT], [EQUATION], [HEADING], [FIGURE], "
    "[CAPTION], [TABLE], [LIST_ITEM], [FOOTNOTE], [UNKNOWN]) in your output. "
    "These are INPUT annotations only.\n\n"
    "MATH DISPLAY RULES:\n"
    "- $$...$$ (display) only for standalone equations on their own line.\n"
    "- $...$ (inline) for all math embedded within a sentence or paragraph.\n"
    "- If an expression is surrounded by prose on the same line in the input, it is inline.\n"
    "- NEVER break a sentence with $$...$$."
)


def _strip_html(text: str) -> str:
    return _RE_HTML.sub("", text).strip()


def _merge_headings(blocks: list[TextBlock]) -> list[TextBlock]:
    """Merge adjacent (number, title) or (title, number) HEADING pairs into one block.

    Handles cases like ['7.3.2', 'Fast Fourier Transformation'] or
    ['Short Time Fourier Transform', '8.1'] that are split by the layout detector.
    """
    result: list[TextBlock] = []
    i = 0
    while i < len(blocks):
        b = blocks[i]
        if (
            b.block_type == BlockType.HEADING
            and i + 1 < len(blocks)
            and blocks[i + 1].block_type == BlockType.HEADING
        ):
            t1 = _strip_html(b.raw_text)
            t2 = _strip_html(blocks[i + 1].raw_text)
            n1 = bool(_RE_SECTION_NUM.match(t1))
            n2 = bool(_RE_SECTION_NUM.match(t2))
            if n1 != n2:  # exactly one is a pure number — merge with number first
                num, title = (t1, t2) if n1 else (t2, t1)
                merged = TextBlock(
                    bbox=b.bbox,
                    raw_text=f"{num} {title}",
                    confidence=b.confidence,
                    block_type=BlockType.HEADING,
                )
                result.append(merged)
                i += 2
                continue
        result.append(b)
        i += 1
    return result


def _strip_outer_fence(text: str) -> str:
    """Remove a ``` wrapper that the LLM occasionally puts around the entire output."""
    m = _RE_OUTER_FENCE.match(text)
    return m.group(1) if m else text


def _merge_figure_captions(blocks: list[TextBlock]) -> list[TextBlock]:
    """Merge each [FIGURE] block with its immediately following [CAPTION] blocks.

    Produces a single [FIGURE] block whose raw_text holds the merged caption,
    serialised as [FIGURE+CAPTION] so the LLM gets one unambiguous signal.
    """
    result: list[TextBlock] = []
    i = 0
    while i < len(blocks):
        b = blocks[i]
        if b.block_type == BlockType.FIGURE:
            j = i + 1
            caption_parts: list[str] = []
            while j < len(blocks) and blocks[j].block_type == BlockType.CAPTION:
                part = _strip_html(blocks[j].raw_text).strip()
                if part:
                    caption_parts.append(part)
                j += 1
            caption = " ".join(caption_parts)
            merged = TextBlock(
                bbox=b.bbox,
                raw_text=caption,
                confidence=b.confidence,
                block_type=BlockType.FIGURE,
            )
            result.append(merged)
            i = j
        else:
            result.append(b)
            i += 1
    return result


_RE_FIGURE_LINE = re.compile(r'^\s*>\s*\[Figure\]', re.IGNORECASE)
_RE_FIGURE_REF_HEADING = re.compile(r'^#{1,4}\s+Fig(?:ure)?\b', re.IGNORECASE)


def _fix_figure_hallucination(markdown: str, input_blocks: list[TextBlock]) -> str:
    """Remove > [Figure] lines that the LLM hallucinated from figure text-references.

    Keeps at most as many > [Figure] lines as there are FIGURE blocks in the input.
    Also demotes any heading that starts with 'Figure N' or 'Fig.' to plain text.
    """
    expected = sum(1 for b in input_blocks if b.block_type == BlockType.FIGURE)
    lines = markdown.splitlines()
    kept_figures = 0
    result = []
    for line in lines:
        if _RE_FIGURE_LINE.match(line):
            if kept_figures < expected:
                result.append(line)
                kept_figures += 1
            # else: drop hallucinated figure line
        elif _RE_FIGURE_REF_HEADING.match(line):
            # Demote figure-reference heading to plain text
            result.append(re.sub(r'^#{1,4}\s+', '', line))
        else:
            result.append(line)
    return "\n".join(result)


def _fix_leading_text_heading(markdown: str, input_blocks: list[TextBlock]) -> str:
    """Strip accidental heading markers from the first output line when the first
    input block is TEXT. Also normalizes headings deeper than #### to ####."""
    lines = markdown.splitlines()

    if input_blocks and input_blocks[0].block_type == BlockType.TEXT:
        for idx, line in enumerate(lines):
            stripped = line.strip()
            if stripped:
                if stripped.startswith("#"):
                    lines[idx] = stripped.lstrip("#").strip()
                break

    # Normalize any #### or deeper heading whose text starts with a 3+ dot section
    # number (e.g. 7.3.2.1) to exactly ####; catches LLM over-nesting.
    for idx, line in enumerate(lines):
        m = re.match(r'^(#{4,})\s+(.*)', line)
        if m and _RE_DEEP_SECTION_NUM.match(m.group(2).strip()):
            lines[idx] = f"#### {m.group(2).strip()}"

    return "\n".join(lines)


def _fix_figure_format(markdown: str) -> str:
    """Ensure > [Figure] is always followed by caption on the next line, not inline."""
    lines = markdown.splitlines()
    result = []
    for line in lines:
        m = re.match(r'^(>\s*\[Figure\])\s+(.+)$', line, re.IGNORECASE)
        if m:
            result.append(m.group(1))
            result.append("")
            result.append(m.group(2).strip("*"))
        else:
            if result and _RE_FIGURE_LINE.match(result[-1]) and re.match(r'^\*(.+)\*$', line):
                result.append(line.strip("*"))
            else:
                result.append(line)
    return "\n".join(result)


_RE_BLOCK_TAG = re.compile(
    r'^\[(?:TEXT|EQUATION|HEADING|FIGURE|CAPTION|TABLE|LIST_ITEM|FOOTNOTE|UNKNOWN)[^\]]*\]\s*',
    re.MULTILINE,
)


def _scrub_block_tags(markdown: str) -> str:
    return _RE_BLOCK_TAG.sub("", markdown)


def _serialize_block(block: TextBlock) -> str:
    if block.block_type == BlockType.EQUATION and block.latex:
        return f"[EQUATION] {block.latex}"
    if block.block_type == BlockType.FIGURE:
        caption = _strip_html(block.raw_text).strip()
        return f"[FIGURE+CAPTION] {caption}" if caption else "[FIGURE]"
    tag = block.block_type.value.upper()
    return f"[{tag}] {_strip_html(block.raw_text)}"


def build_prompt(blocks: list[TextBlock]) -> str:
    blocks = _merge_headings(blocks)
    blocks = _merge_figure_captions(blocks)
    lines = []
    for b in blocks:
        if not b.raw_text.strip() and b.block_type != BlockType.FIGURE:
            continue
        lines.append(_serialize_block(b))
    return "\n".join(lines)


def _check_ollama(model: str) -> None:
    try:
        resp = requests.get(f"{_OLLAMA_BASE}/api/tags", timeout=5)
        resp.raise_for_status()
    except Exception:
        print(f"Error: Ollama is not running. Start it with: ollama serve", file=sys.stderr)
        sys.exit(1)

    names = [m["name"] for m in resp.json().get("models", [])]
    if not any(m.startswith(model.split(":")[0]) for m in names):
        print(f"Error: model '{model}' not found in Ollama.", file=sys.stderr)
        print(f"Pull it with: ollama pull {model}", file=sys.stderr)
        print(f"Available: {names}", file=sys.stderr)
        sys.exit(1)


def _coverage_ok(blocks: list[TextBlock], markdown: str, min_ratio: float = 0.50) -> bool:
    input_words = sum(
        len(b.raw_text.split())
        for b in blocks
        if b.block_type in (BlockType.TEXT, BlockType.HEADING, BlockType.LIST_ITEM)
    )
    output_words = len(markdown.split())
    return input_words == 0 or (output_words / input_words) >= min_ratio


def _post_process(markdown: str, blocks: list[TextBlock]) -> str:
    markdown = _strip_outer_fence(markdown)
    markdown = _fix_figure_hallucination(markdown, blocks)
    markdown = _fix_figure_format(markdown)
    markdown = _fix_leading_text_heading(markdown, blocks)
    markdown = _scrub_block_tags(markdown)
    return markdown


def correct_page(
    page_num: int, blocks: list[TextBlock], model: str = _OLLAMA_MODEL
) -> tuple[str, bool]:
    """Run Ollama correction on one page.

    Returns (markdown, truncated) where truncated=True if the output failed
    coverage after one retry.
    """
    user_content = build_prompt(blocks)
    messages: list[dict] = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user",   "content": user_content},
    ]
    payload = {
        "model": model,
        "messages": messages,
        "options": {"num_ctx": _OLLAMA_NUM_CTX},
        "stream": False,
    }
    resp = requests.post(f"{_OLLAMA_BASE}/api/chat", json=payload, timeout=120)
    resp.raise_for_status()
    result = _post_process(resp.json()["message"]["content"].strip(), blocks)

    if _coverage_ok(blocks, result):
        return result, False

    # Retry once with an explicit coverage nudge
    messages = messages + [
        {"role": "assistant", "content": result},
        {
            "role": "user",
            "content": (
                "WARNING: your previous output was missing content. "
                "Reproduce ALL blocks from the input."
            ),
        },
    ]
    payload["messages"] = messages
    resp2 = requests.post(f"{_OLLAMA_BASE}/api/chat", json=payload, timeout=120)
    resp2.raise_for_status()
    result2 = _post_process(resp2.json()["message"]["content"].strip(), blocks)
    truncated = not _coverage_ok(blocks, result2)
    if truncated:
        print(f"  [correction] page {page_num}: coverage still low after retry — flagging as truncated.", file=sys.stderr)
    return result2, truncated


def correct_pages(
    equations_dir: Path = EQUATIONS_DIR,
    correction_dir: Path = CORRECTION_DIR,
    model: str = _OLLAMA_MODEL,
    start_page: Optional[int] = None,
    end_page: Optional[int] = None,
) -> tuple[int, int]:
    """Correct all pages via Ollama; write .md files. Returns (done, failed)."""

    def _pnum(p: Path) -> int:
        return int(p.stem.split("_")[1])

    eq_jsons = sorted(equations_dir.glob("page_*.json"))
    if not eq_jsons:
        print(f"Error: no equation output in {equations_dir}. Run Stage 3 first.", file=sys.stderr)
        return 0, 0

    if start_page or end_page:
        lo = start_page or 1
        hi = end_page or _pnum(eq_jsons[-1])
        eq_jsons = [p for p in eq_jsons if lo <= _pnum(p) <= hi]

    to_process: list[Path] = []
    skipped = pre_failed = 0
    for jp in eq_jsons:
        pn = _pnum(jp)
        if not should_process("correction", pn):
            status, _ = get_status("correction", pn)
            if status == "done":
                skipped += 1
            else:
                print(f"  [correction] page {pn}: exhausted retries, skipping.")
                pre_failed += 1
        else:
            to_process.append(jp)

    if not to_process:
        print(f"Correction: all pages already processed ({skipped} done).")
        return 0, pre_failed

    _check_ollama(model)
    correction_dir.mkdir(parents=True, exist_ok=True)
    init_db()

    print(f"Running correction via Ollama ({model}) on {len(to_process)} pages...")

    done = failed = 0
    for jp in to_process:
        pn = _pnum(jp)
        try:
            raw = json.loads(jp.read_text())
            blocks = [TextBlock.from_dict(b) for b in raw["blocks"]]
            markdown, truncated = correct_page(pn, blocks, model)
            if not markdown:
                raise ValueError("Ollama returned empty output")
            if truncated:
                markdown = f"<!-- WARNING: page {pn} may be truncated -->\n\n{markdown}"
            out_path = correction_dir / f"page_{pn:04d}.md"
            out_path.write_text(markdown, encoding="utf-8")
            set_status("correction", pn, "done", str(out_path))
            done += 1
            lines = markdown.count("\n") + 1
            print(f"  [correction] page {pn} → {out_path.name}  ({lines} lines)")
        except Exception as exc:
            set_status("correction", pn, "failed")
            failed += 1
            print(f"  [correction] page {pn}: FAILED — {exc}")

    print(f"\nCorrection complete: {done} done, {skipped} skipped, {failed + pre_failed} failed.")
    return done, failed + pre_failed
