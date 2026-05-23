"""Serialization and final export assembly utilities.

Input:  checkpoints/corrected/page_{N:04d}.md
        checkpoints/equations/page_{N:04d}.json

Output: output/{stem}.md   — rendered export, pages delimited by <!-- page N -->
        output/{stem}.json — structured manifest (OutputManifest)
"""
from __future__ import annotations
import json
import re
import sys
from pathlib import Path

from pocket_specialist.core.models import BlockType, OutputManifest, OutputRecord, TextBlock


_RE_HEADING = re.compile(r'^#{1,6}\s+(.+)$', re.MULTILINE)
_RE_LATEX_BLOCK = re.compile(r'\$\$(.*?)\$\$', re.DOTALL)


def extract_headings(markdown_text: str) -> list[str]:
    return _RE_HEADING.findall(markdown_text)


def extract_latex_blocks(markdown_text: str) -> list[str]:
    return [m.strip() for m in _RE_LATEX_BLOCK.findall(markdown_text)]


def _deduplicate(content: str) -> str:
    seen: set[str] = set()
    result: list[str] = []
    for block in re.split(r'\n{2,}', content):
        key = block.strip()
        if key and key not in seen:
            seen.add(key)
            result.append(block)
    return "\n\n".join(result)


def _unwrap_spurious_containers(markdown: str) -> str:
    lines = markdown.splitlines()
    result = []
    for line in lines:
        if re.match(r'^>\s', line) and not re.search(r'\[Figure\]', line, re.IGNORECASE):
            result.append(line[2:])
        else:
            result.append(line)
    content = "\n".join(result)
    content = re.sub(
        r'```\w*\n((?:[^`]|\n)*?\$\$(?:[^`]|\n)*?)```',
        r'\1', content
    )
    return content


def _page_num(path: Path) -> int:
    return int(path.stem.split("_")[1])


def _format_blocks_as_markdown(blocks: list[TextBlock]) -> str:
    """Lightweight fallback formatter: enriched JSON blocks to Markdown export."""
    lines: list[str] = []
    for b in blocks:
        text = b.raw_text.strip()
        if not text and b.block_type != BlockType.FIGURE:
            continue
        if b.block_type == BlockType.HEADING:
            lines.append(f"## {text}")
        elif b.block_type == BlockType.EQUATION:
            lines.append(f"$$\n{b.latex or text}\n$$")
        elif b.block_type == BlockType.EQUATION_FAILED:
            lines.append(f"$$\n% OCR failed\n{text}\n$$")
        elif b.block_type == BlockType.LIST_ITEM:
            lines.append(f"- {text.lstrip('•-* ')}")
        elif b.block_type == BlockType.FIGURE:
            lines.append("> [Figure]")
        elif b.block_type == BlockType.CAPTION:
            lines.append(f"*{text}*")
        elif b.block_type == BlockType.TABLE:
            lines.append(f"```\n{text}\n```")
        else:
            lines.append(text)
    return "\n\n".join(lines)


def _collect_pages(
    corrected_dir: Path,
    equations_dir: Path,
) -> list[tuple[int, str | None]]:
    """Return (page_num, content_or_None) sorted by page.

    Prefers corrected .md exports; falls back to formatting enriched JSON inline;
    yields None only if a page number exists in one dict but not the other
    (shouldn't occur in practice).
    """
    md_by_page: dict[int, str] = {}
    if corrected_dir.exists():
        for p in corrected_dir.glob("page_*.md"):
            md_by_page[_page_num(p)] = p.read_text(encoding="utf-8")

    json_by_page: dict[int, Path] = {}
    if equations_dir.exists():
        for p in equations_dir.glob("page_*.json"):
            json_by_page[_page_num(p)] = p

    all_nums = sorted(set(md_by_page) | set(json_by_page))
    result: list[tuple[int, str | None]] = []
    for pn in all_nums:
        if pn in md_by_page:
            result.append((pn, _deduplicate(_unwrap_spurious_containers(md_by_page[pn]))))
        elif pn in json_by_page:
            raw = json.loads(json_by_page[pn].read_text())
            blocks = [TextBlock.from_dict(b) for b in raw["blocks"]]
            result.append((pn, _deduplicate(_unwrap_spurious_containers(_format_blocks_as_markdown(blocks)))))
        else:
            result.append((pn, None))
    return result


def assemble_markdown(
    corrected_dir: Path,
    output_path: Path,
    equations_dir: Path,
) -> int:
    """Write full-book Markdown with <!-- page N --> delimiters.

    Returns the number of pages with real content (excludes placeholders).
    """
    pages = _collect_pages(corrected_dir, equations_dir)
    if not pages:
        print(f"Error: no pages found in {corrected_dir} or {equations_dir}.", file=sys.stderr)
        return 0

    parts: list[str] = []
    succeeded = 0
    for pn, content in pages:
        parts.append(f"<!-- page {pn} -->")
        if content and content.strip():
            parts.append(content)
            succeeded += 1
        else:
            parts.append(f"<!-- page {pn}: OCR failed -->")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n\n".join(parts) + "\n", encoding="utf-8")
    return succeeded


def assemble_manifest(
    corrected_dir: Path,
    output_path: Path,
    source_pdf: Path,
    equations_dir: Path,
) -> int:
    """Write JSON manifest (OutputManifest). Returns pages with content."""
    pages = _collect_pages(corrected_dir, equations_dir)
    if not pages:
        print(f"Error: no pages found in {corrected_dir} or {equations_dir}.", file=sys.stderr)
        return 0

    records: list[OutputRecord] = []
    succeeded = 0
    for pn, content in pages:
        if content and content.strip():
            headings = extract_headings(content)
            records.append(OutputRecord(
                page_num=pn,
                heading=headings[0] if headings else None,
                content=content,
                latex_blocks=extract_latex_blocks(content),
            ))
            succeeded += 1
        else:
            records.append(OutputRecord(page_num=pn, heading=None, content=None))

    manifest = OutputManifest(
        source_pdf=str(source_pdf),
        total_pages=max(pn for pn, _ in pages),
        pages_succeeded=succeeded,
        pages_failed=len(pages) - succeeded,
        records=records,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(manifest.to_dict(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return succeeded


def assemble_document(
    corrected_dir: Path,
    output_dir: Path,
    source_pdf: Path,
    equations_dir: Path,
) -> OutputManifest:
    """Concatenate per-page Markdown and write .md + .json to output_dir."""
    pages = _collect_pages(corrected_dir, equations_dir)
    if not pages:
        raise ValueError(
            f"No pages found in {corrected_dir} or {equations_dir}. "
            "Run the required upstream pipeline steps first."
        )

    stem = source_pdf.stem
    output_dir.mkdir(parents=True, exist_ok=True)
    md_path   = output_dir / f"{stem}.md"
    json_path = output_dir / f"{stem}.json"

    # ── Rendered Export ─────────────────────────────────────────────────────────
    parts: list[str] = []
    succeeded = 0
    for pn, content in pages:
        parts.append(f"<!-- page {pn} -->")
        if content and content.strip():
            parts.append(content)
            succeeded += 1
        else:
            parts.append(f"<!-- page {pn}: OCR failed -->")
    md_path.write_text("\n\n".join(parts) + "\n", encoding="utf-8")

    # ── Manifest ──────────────────────────────────────────────────────────────
    records: list[OutputRecord] = []
    for pn, content in pages:
        if content and content.strip():
            headings = extract_headings(content)
            records.append(OutputRecord(
                page_num=pn,
                heading=headings[0] if headings else None,
                content=content,
                latex_blocks=extract_latex_blocks(content),
            ))
        else:
            records.append(OutputRecord(page_num=pn, heading=None, content=None))

    failed = len(pages) - succeeded
    manifest = OutputManifest(
        source_pdf=str(source_pdf),
        total_pages=max(pn for pn, _ in pages),
        pages_succeeded=succeeded,
        pages_failed=failed,
        records=records,
    )
    json_path.write_text(
        json.dumps(manifest.to_dict(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print(f"Assembly complete: {succeeded} pages assembled, {failed} failed/missing.")
    print(f"  Rendered export → {md_path}")
    print(f"  Manifest → {json_path}")
    return manifest
