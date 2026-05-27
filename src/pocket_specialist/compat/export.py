"""Optional export-normalization pass for enriched OCR blocks.

Input:  checkpoints/equations/page_{N:04d}.json
Output: checkpoints/corrected/page_{N:04d}.md
"""
from __future__ import annotations
import base64
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

import requests

from pocket_specialist.core.config import OLLAMA_BASE, OLLAMA_MODEL, correction_dir_for, crops_dir_for, equations_dir_for
from pocket_specialist.storage.checkpoint import get_status, init_db, set_status, should_process
from pocket_specialist.core.dev_checkpoints import write_development_checkpoint
from pocket_specialist.core.progress import model_status, phase_complete, phase_error, phase_start, phase_validation, progress_bar
from pocket_specialist.serializers.markdown import _deduplicate, _format_blocks_as_markdown, _unwrap_spurious_containers
from pocket_specialist.core.models import BlockType, TextBlock


_OLLAMA_BASE = OLLAMA_BASE
_OLLAMA_MODEL = OLLAMA_MODEL
_OLLAMA_NUM_CTX = 6192
_EQUATION_RETRIES = 2
_EQUATION_TIMEOUT_SECS = 45

_EQUATION_SYSTEM_PROMPT = (
    "You are verifying a single OCR-extracted textbook equation against one attached equation crop image. "
    "Output only the corrected LaTeX for that single equation. "
    "Do not explain, do not add prose, and do not wrap the answer in $$ delimiters."
)

_RE_HTML = re.compile(r"<[^>]+>")
_RE_SECTION_NUM = re.compile(r'^(?:Chapter\s+)?\d+(?:\.\d+)*$', re.IGNORECASE)
_RE_OUTER_FENCE = re.compile(r'^```\w*\n(.*)\n```$', re.DOTALL)
_RE_DEEP_SECTION_NUM = re.compile(r'^\d+(?:\.\d+){3,}(?:\s|$)')
_RE_ORDERED_ITEM = re.compile(r'^\d+\.\s')

_SYSTEM_PROMPT = (
    "You are a precise technical document formatter. "
    "You receive a rendered textbook page image plus OCR-extracted blocks from that same page; "
    "each line is prefixed with its semantic block type.\n\n"
    "Use the attached equation crop images to visually verify superscripts/subscripts, matrices, delimiters, alignment, and whether extracted equation text is malformed. "
    "Use the typed blocks as the canonical content source: do not invent text that is not supported by the blocks.\n\n"
    "HARD CONSTRAINTS — no exceptions:\n"
    "1. Output ONLY valid Markdown. NEVER wrap the entire response in a code fence.\n"
    "2. NEVER add, invent, complete, or paraphrase any content. "
    "Reproduce only what is given.\n"
    "3. [EQUATION] or [EQUATION eq_XX]: emit the LaTeX VERBATIM inside $$...$$. "
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
    "- [ORDERED_ITEM]    → N. <text> (numeric list item, not bullet)\n"
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
    "These are INPUT annotations only. If an equation block includes an eq_XX suffix, it refers to the matching attached crop image in the same order. NEVER emit the eq_XX suffix in output.\n\n"
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
    r'^\[(?:TEXT|EQUATION|HEADING|FIGURE|CAPTION|TABLE|LIST_ITEM|ORDERED_ITEM|FOOTNOTE|UNKNOWN)[^\]]*\]\s*',
    re.MULTILINE,
)


def _scrub_block_tags(markdown: str) -> str:
    return _RE_BLOCK_TAG.sub("", markdown)


def _serialize_block(block: TextBlock, equation_ref: str | None = None) -> str:
    eq_suffix = f" {equation_ref}" if equation_ref else ""
    if block.block_type == BlockType.EQUATION and block.latex:
        return f"[EQUATION{eq_suffix}] {block.latex}"
    if block.block_type == BlockType.FIGURE:
        caption = _strip_html(block.raw_text).strip()
        return f"[FIGURE+CAPTION] {caption}" if caption else "[FIGURE]"
    text = _strip_html(block.raw_text)
    if block.block_type == BlockType.LIST_ITEM and _RE_ORDERED_ITEM.match(text):
        return f"[ORDERED_ITEM] {text}"
    tag = block.block_type.value.upper()
    return f"[{tag}{eq_suffix}] {text}"


def build_prompt(blocks: list[TextBlock], equation_refs: dict[int, str] | None = None) -> str:
    blocks = _merge_headings(blocks)
    blocks = _merge_figure_captions(blocks)
    lines = []
    for idx, b in enumerate(blocks):
        if not b.raw_text.strip() and b.block_type != BlockType.FIGURE:
            continue
        lines.append(_serialize_block(b, equation_refs.get(idx) if equation_refs else None))
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


def _equation_ref_map(page_num: int, blocks: list[TextBlock], crops_dir: Path) -> tuple[dict[int, str], list[Path]]:
    crop_paths = sorted(crops_dir.glob(f"page_{page_num:04d}_eq_*.png"))
    if not crop_paths:
        return {}, []

    eq_block_indexes = [
        idx for idx, block in enumerate(blocks)
        if block.block_type in (BlockType.EQUATION, BlockType.EQUATION_FAILED)
    ]
    refs: dict[int, str] = {}
    for crop_idx, block_idx in enumerate(eq_block_indexes[:len(crop_paths)]):
        refs[block_idx] = f"eq_{crop_idx:02d}"
    return refs, crop_paths


def _encode_image(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode("ascii")


def _normalize_latex(text: str) -> str:
    cleaned = text.strip()
    cleaned = _strip_outer_fence(cleaned)
    cleaned = cleaned.strip()
    if cleaned.startswith("$$") and cleaned.endswith("$$"):
        cleaned = cleaned[2:-2].strip()
    cleaned = re.sub(r'^\$\s*', '', cleaned)
    cleaned = re.sub(r'\s*\$$', '', cleaned)
    return cleaned.strip()


def correct_equation_latex(
    page_num: int,
    ref_name: str,
    crop_path: Path,
    candidate_latex: str,
    model: str = _OLLAMA_MODEL,
) -> str:
    user_content = (
        f"Equation reference: {ref_name}\n"
        "Candidate LaTeX from OCR follows. Correct it to match the attached equation crop exactly, "
        "preserving symbols, superscripts, subscripts, matrices, delimiters, and alignment cues where representable in LaTeX.\n\n"
        f"Candidate LaTeX:\n{candidate_latex}"
    )
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": _EQUATION_SYSTEM_PROMPT},
            {"role": "user", "content": user_content, "images": [_encode_image(crop_path)]},
        ],
        "options": {"num_ctx": min(_OLLAMA_NUM_CTX, 2048)},
        "keep_alive": 0,
        "stream": False,
    }
    last_exc: Exception | None = None
    for attempt in range(1, _EQUATION_RETRIES + 1):
        try:
            resp = requests.post(
                f"{_OLLAMA_BASE}/api/chat",
                json=payload,
                timeout=_EQUATION_TIMEOUT_SECS,
            )
            resp.raise_for_status()
            result = _normalize_latex(resp.json()["message"]["content"])
            if not result:
                raise ValueError(f"Empty LaTeX correction for {ref_name} on page {page_num}")
            return result
        except requests.HTTPError as exc:
            last_exc = exc
            status_code = exc.response.status_code if exc.response is not None else None
            if status_code and 500 <= status_code < 600:
                raise exc
            if attempt < _EQUATION_RETRIES:
                print(
                    f"  [correction] page {page_num} {ref_name}: retry {attempt}/{_EQUATION_RETRIES - 1} after error — {exc}"
                )
        except Exception as exc:
            last_exc = exc
            if attempt < _EQUATION_RETRIES:
                print(
                    f"  [correction] page {page_num} {ref_name}: retry {attempt}/{_EQUATION_RETRIES - 1} after error — {exc}"
                )
    assert last_exc is not None
    raise last_exc


def correct_page(
    page_num: int,
    blocks: list[TextBlock],
    model: str = _OLLAMA_MODEL,
    image_paths: list[Path] | None = None,
    equation_refs: dict[int, str] | None = None,
) -> tuple[str, bool]:
    """Run Ollama correction on one page.

    Returns (markdown, truncated) where truncated=True if the output failed
    coverage after one retry.
    """
    user_content = build_prompt(blocks, equation_refs=equation_refs)
    if image_paths:
        ref_line = "Attached equation crop images, in order: " + ", ".join(f"eq_{i:02d}" for i in range(len(image_paths)))
        user_content = ref_line + "\n\n" + user_content
    user_message: dict[str, object] = {"role": "user", "content": user_content}
    if image_paths:
        user_message["images"] = [_encode_image(path) for path in image_paths]

    messages: list[dict[str, object]] = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        user_message,
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


def _correct_page_job(jp: Path, model: str, crops_dir: Path) -> tuple[int, str]:
    pn = int(jp.stem.split("_")[1])
    raw = json.loads(jp.read_text())
    blocks = [TextBlock.from_dict(b) for b in raw["blocks"]]
    equation_refs, crop_paths = _equation_ref_map(pn, blocks, crops_dir)
    if not crop_paths:
        raise ValueError(f"No equation crops found for page {pn}")

    ref_to_index = {ref: idx for idx, ref in equation_refs.items()}
    equation_failures: list[str] = []
    for crop_idx, crop_path in enumerate(crop_paths):
        ref_name = f"eq_{crop_idx:02d}"
        block_idx = ref_to_index.get(ref_name)
        if block_idx is None:
            continue
        block = blocks[block_idx]
        candidate = (block.latex or _strip_html(block.raw_text)).strip()
        if not candidate:
            continue
        try:
            corrected = correct_equation_latex(pn, ref_name, crop_path, candidate, model=model)
            block.latex = corrected
            block.latex_confidence = 1.0
            block.block_type = BlockType.EQUATION
        except Exception as exc:
            equation_failures.append(f"{ref_name}: {exc}")
            print(f"  [correction] page {pn} {ref_name}: kept original OCR — {exc}")

    markdown = _deduplicate(_unwrap_spurious_containers(_format_blocks_as_markdown(blocks)))
    if equation_failures:
        warning = "<!-- WARNING: equation crop corrections failed for " + ", ".join(equation_failures) + " -->"
        markdown = warning + "\n\n" + markdown
    return pn, markdown


def correct_pages(
    document: str,
    equations_dir: Path | None = None,
    correction_dir: Path | None = None,
    model: str = _OLLAMA_MODEL,
    start_page: Optional[int] = None,
    end_page: Optional[int] = None,
    max_parallel: int = 2,
    crops_dir: Path | None = None,
) -> tuple[int, int]:
    """Correct all pages via Ollama; write .md files. Returns (done, failed)."""

    def _pnum(p: Path) -> int:
        return int(p.stem.split("_")[1])

    phase_start("correction", document)
    equations_dir = equations_dir or equations_dir_for(document)
    correction_dir = correction_dir or correction_dir_for(document)
    crops_dir = crops_dir or crops_dir_for(document)

    eq_jsons = sorted(equations_dir.glob("page_*.json"))
    if not eq_jsons:
        print(f"Error: no equation output in {equations_dir}. Run layout/formula enrichment first.", file=sys.stderr)
        return 0, 0

    if start_page or end_page:
        lo = start_page or 1
        hi = end_page or _pnum(eq_jsons[-1])
        eq_jsons = [p for p in eq_jsons if lo <= _pnum(p) <= hi]

    to_process: list[Path] = []
    skipped = skipped_no_crops = pre_failed = 0
    for jp in eq_jsons:
        pn = _pnum(jp)
        if not list(crops_dir.glob(f"page_{pn:04d}_eq_*.png")):
            print(f"  [correction] page {pn}: no equation crops, skipping.")
            skipped_no_crops += 1
            continue
        if not should_process("correction", document, pn):
            status, _ = get_status("correction", document, pn)
            if status == "done":
                skipped += 1
            else:
                print(f"  [correction] page {pn}: exhausted retries, skipping.")
                pre_failed += 1
        else:
            to_process.append(jp)

    if not to_process:
        phase_validation("correction", "no pages eligible after checkpoint/crop filtering")
        parts = []
        if skipped:
            parts.append(f"{skipped} already done")
        if skipped_no_crops:
            parts.append(f"{skipped_no_crops} without equation crops")
        summary = ", ".join(parts) if parts else "0 pages eligible"
        print(f"Correction: no pages eligible ({summary}).")
        phase_complete("correction", f"0 done, {pre_failed} failed")
        return 0, pre_failed

    phase_validation("correction", f"queued {len(to_process)} page(s) model={model} output={correction_dir}")
    model_status("correction", f"checking Ollama model {model}")
    _check_ollama(model)
    model_status("correction", f"model available: {model}")
    correction_dir.mkdir(parents=True, exist_ok=True)
    crops_dir = crops_dir.resolve()
    init_db()

    max_parallel = max(1, int(max_parallel))
    print(
        f"Running correction via Ollama ({model}) on {len(to_process)} pages "
        f"with up to {max_parallel} parallel request(s)..."
    )

    done = failed = 0

    def _commit_success(pn: int, markdown: str) -> None:
        nonlocal done
        out_path = correction_dir / f"page_{pn:04d}.md"
        out_path.write_text(markdown, encoding="utf-8")
        set_status("correction", document, pn, "done", str(out_path))
        write_development_checkpoint(
            document,
            "correction",
            page=pn,
            summary={"output": str(out_path), "line_count": markdown.count("\n") + 1},
            artifacts={out_path.name: out_path},
        )
        done += 1
        lines = markdown.count("\n") + 1
        print(f"  [correction] page {pn} → {out_path.name}  ({lines} lines)")

    def _commit_failure(pn: int, exc: Exception) -> None:
        nonlocal failed
        set_status("correction", document, pn, "failed")
        failed += 1
        phase_error("correction", f"page {pn}: {exc}")
        print(f"  [correction] page {pn}: FAILED — {exc}")

    if max_parallel == 1:
        for index, jp in enumerate(to_process, 1):
            pn = _pnum(jp)
            progress_bar("correction", index, len(to_process), f"page {pn}")
            try:
                result_pn, markdown = _correct_page_job(jp, model, crops_dir)
                _commit_success(result_pn, markdown)
            except Exception as exc:
                _commit_failure(pn, exc)
    else:
        with ThreadPoolExecutor(max_workers=max_parallel) as executor:
            future_to_page = {executor.submit(_correct_page_job, jp, model, crops_dir): _pnum(jp) for jp in to_process}
            completed_pages = 0
            for future in as_completed(future_to_page):
                pn = future_to_page[future]
                completed_pages += 1
                progress_bar("correction", completed_pages, len(to_process), f"page {pn}")
                try:
                    result_pn, markdown = future.result()
                    _commit_success(result_pn, markdown)
                except Exception as exc:
                    _commit_failure(pn, exc)

    skip_parts = []
    if skipped:
        skip_parts.append(f"{skipped} already done")
    if skipped_no_crops:
        skip_parts.append(f"{skipped_no_crops} without equation crops")
    skipped_summary = ", ".join(skip_parts) if skip_parts else "0 skipped"
    print(f"\nCorrection complete: {done} done, {skipped_summary}, {failed + pre_failed} failed.")
    return done, failed + pre_failed
