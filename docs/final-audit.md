# OCR Pipeline — Audit & Fix Recommendations

Derived from a systematic page-by-page visual audit of the Scherer textbook (640 pages), comparing Stage 5 corrected markdown against source PNG renders. Patterns and fixes are general and apply to any document of similar type (scientific, multi-level structure, math-heavy, multi-column).

**Pipeline stages referenced:**

| Stage | File | What it does |
|---|---|---|
| S1 | `pipeline/render.py` | PDF → PNG at 2× zoom (PyMuPDF) |
| S2 | `pipeline/ocr.py` | Surya OCR → per-page JSON (all blocks typed UNKNOWN) |
| S3 | `pipeline/equations.py` | Surya layout detection + Surya LaTeX OCR on equation crops → typed blocks JSON |
| S4 | `pipeline/correction.py` | Ollama `qwen2.5vl:3b` converts typed block list → clean Markdown per page |
| S5 | `pipeline/assemble.py` | Concatenates per-page Markdown → final `.md` + `.json` manifest |

---

## Part 1 — Defect Patterns

### P1 — Equation numbers stripped universally

**Affected pages:** ~579 | **Severity:** MINOR (universal baseline)

Every numbered displayed equation `(N.M)` loses its label. The `_remove_eq_numbers` function in Stage 3 explicitly drops blocks matching `^\s*\(\d+(?:\.\d+)*\)\s*$` that are tagged as `EQUATION`. This is intentional but wrong — the labels are needed for cross-reference. Because this affects nearly every content page it is the most pervasive single defect.

**Escalates** when equations are referenced by number in surrounding prose (e.g. "see Eq. 5.14"), turning every such reference into a dangling pointer.

---

### P2 — Heading level inflation

**Affected pages:** ~506 | **Severity:** MAJOR → CRITICAL

Headings are assigned one or two levels higher than the source hierarchy:

| Source element | Expected | Typical output |
|---|---|---|
| Chapter N | `##` | `#` |
| Section N.M | `##` | `#` or `##` |
| Subsection N.M.P | `###` | `##` or `#` |
| Sub-subsection N.M.P.Q | `####` | `##` or `###` |
| "Problems" section | `##` | `#` or `**Problems**` |
| Individual problem N.M | `###` | `#` or `##` |
| Appendix / Index heading | `##` | `#` |

**Sub-pattern P2a — Informal run-in headings inflated (~37 pages, MAJOR):** Bold phrases that are paragraph labels in the source (e.g. "Dressed Exciton", "Solitonic Solution") are promoted to `##` or `#` headings.

**Sub-pattern P2b — False headings invented by the LLM (~12 pages, MAJOR–CRITICAL):** The LLM fabricates heading text from prose fragments that are not headings in the source (e.g. "Given Derivative Values").

**Sub-pattern P2c — Duplicate headings (~7 pages, MAJOR–CRITICAL):** The same heading appears twice — once from structure detection, once from the LLM correction pass.

---

### P3 — Table structure failure

**Affected pages:** ~57 (27 CRITICAL, 14 MAJOR, 16 MODERATE) | **Severity:** MODERATE → CRITICAL

Tables are the most failure-prone element. Distinct failure modes:

| Failure mode | Severity | Pages |
|---|---|---|
| Column headers swapped / promoted to data cells | CRITICAL | ~20 |
| Entire table collapsed into a single pipe-delimited row | CRITICAL | ~12 |
| Columns absent (e.g. "Pages" column missing) | CRITICAL | ~10 |
| Rows merged; multiple entries in one cell | MAJOR | ~14 |
| Extra spurious columns created from data values | MAJOR | ~8 |
| Partial rows lost; table truncated | MODERATE | ~16 |

Multi-page tables degrade further: each page independently re-derives the column schema, so a table spanning 8 pages (Appendix B) is completely unrecoverable.

---

### P4 — Figure/caption format failures

**Affected pages:** ~220 | **Severity:** MODERATE → MAJOR

Expected format: `> [Figure]` on its own line, then caption as plain paragraph below. Five distinct failure modes:

| Failure mode | Severity | Pages |
|---|---|---|
| No `> [Figure]` placeholder; caption as plain prose | MAJOR | ~106 |
| Caption on same line as `> [Figure]` (inline) | MODERATE | ~26 |
| Caption placed before `> [Figure]` (inverted) | MODERATE | ~25 |
| Caption rendered as list items (`*` or `-`) | MODERATE | ~30 |
| Caption in italic markdown | MODERATE | ~63 |
| Figure label absent from caption | MODERATE | ~15 |

---

### P5 — Superscript/subscript garbling in LaTeX

**Affected pages:** ~121 (8 CRITICAL, 84 MAJOR, 80 MODERATE) | **Severity:** MODERATE → CRITICAL

- Exponent `³` rendered as `3 \quad` + `\overline{…}` instead of `\sqrt[3]{…}`
- Nested subscripts dropped or collapsed
- Subscript collision when summation variable and bound share the same letter
- Spurious characters inserted into super/subscript positions (e.g. `\kappa_{I_1a}` instead of `\kappa r_{1a}`)

Escalates to CRITICAL when the garbling produces a mathematically wrong value.

---

### P6 — Footnote handling failures

**Affected pages:** ~92 (1 CRITICAL, ~40 MAJOR, ~43 MODERATE) | **Severity:** MODERATE → MAJOR

| Mode | Pages |
|---|---|
| Footnote text absorbed inline into body paragraph | ~64 |
| Footnote entirely omitted | ~27 |
| Superscript footnote call-out (`¹`) carried into body as stray character | ~47 |

Root cause: Stage 3's `_LAYOUT_TO_BLOCKTYPE` has no entry for "Footnote". Footnote regions detected by Surya's layout model fall through to `UNKNOWN` and are treated as body text.

---

### P7 — Spurious structural prefix tags and stray text

**Affected pages:** ~90 (11 CRITICAL, 51 MAJOR, 64 MODERATE) | **Severity:** MODERATE → CRITICAL

**P7a — `[TEXT]`, `[EQUATION]`, `[HEADING]` prefix tags leak into output (~11 CRITICAL pages):** The LLM passes its input annotations through literally rather than converting them. On some pages every line is prefixed, making the output unparseable.

**P7b — Stray axis labels, figure numbers, metadata in prose (~51 MAJOR):** Short strings that were isolated layout elements appear as freestanding sentences.

---

### P8 — Content missing (partial or total page loss)

**Affected pages:** ~47 (18 CRITICAL, 22 MAJOR) | **Severity:** MAJOR → CRITICAL

Equations, paragraphs, or entire sections are silently absent from the output with no placeholder. No coverage check exists: Stage 4 can produce an output shorter than its input with no warning.

---

### P9 — Content reordering

**Affected pages:** ~62 (9 CRITICAL, 14 MAJOR) | **Severity:** MODERATE → CRITICAL

Pipeline reads elements out of source order: whole sections from the page bottom appear at the top of output; figures are displaced; equation sequences are interleaved with prose out of logical order. Root cause: Stage 2/3 uses a simple gap-based x-center column reordering heuristic (`reorder_columns`) rather than a model-based reading order predictor.

---

### P10 — Mathematical operator substitution

**Affected pages:** ~60+ | **Severity:** MODERATE → CRITICAL

| Substitution | Context | Severity |
|---|---|---|
| `∑` → `∫` | Fourier/spectral derivations | CRITICAL |
| `∑` → `\quad` (whitespace) | Dense display math | CRITICAL |
| `∏` → single fraction | Product notation | CRITICAL |
| `∇` components → duplicated wrong-axis terms | Laplacian stencil | CRITICAL |
| `>` / `<` → `\rangle` / `\langle` (bra-ket) | Quantum mechanics chapters | MAJOR (~35 pages) |
| `h` ↔ `\hbar` | Quantum chapters | MAJOR (~13 pages) |
| `&` → `α` or other character | Matrix column separator | MODERATE |
| Comparison operators stripped | Algorithm pseudocode | CRITICAL |

---

### P11 — Inline vs. display math confusion

**Affected pages:** ~77 (2 CRITICAL, 11 MAJOR, 11 MODERATE) | **Severity:** MODERATE → MAJOR

- Display `$$…$$` used mid-sentence instead of inline `$…$`
- A numbered displayed equation rendered as inline math
- Plain text instead of any math delimiters (e.g. `O(h^2)` with no `$`)

---

### P12 — Matrix/alignment environment failures

**Affected pages:** ~37 (3 CRITICAL, 23 MAJOR, 24 MODERATE) | **Severity:** MODERATE → CRITICAL

- Wrong alignment characters (`\ ` instead of `\\`, `&` in wrong column)
- Matrix split into two disconnected `$$` blocks (upper half / lower half separately)
- Matrix entries replaced by garbled text or raw HTML
- Block matrix rendered as nested fractions

---

### P13 — Equation label truncation

**Affected pages:** ~50 (4 CRITICAL, 24 MAJOR, 32 MODERATE) | **Severity:** MODERATE → MAJOR

Labels that survive Stage 3's number-stripping arrive truncated:
- `(24.74)` → `(24.7)` (last digit dropped)
- `(24.)` (number cut mid-number)
- `\tag{2}` instead of `\tag{24.2}` (chapter prefix lost)

---

### P14 — Content duplication

**Affected pages:** ~24 (4 CRITICAL, 16 MAJOR) | **Severity:** MODERATE → CRITICAL

Equations or entire derivations appear verbatim twice within a single page output. Likely caused by the LLM correction pass re-emitting content it already output in a prior step.

---

### P15 — Markdown container misuse

**Affected pages:** ~24 (3 CRITICAL, 15 MAJOR) | **Severity:** MODERATE → CRITICAL

**P15a — Spurious `>` blockquote wrapping (~17 pages):** Body prose or equations wrapped in `>` blockquote, likely contaminated by the `> [Figure]` format rule being applied to non-figure content.

**P15b — Code fence wrapping (~7 pages):** An entire section or page enclosed in triple-backtick fences, suppressing all markdown rendering.

---

### P16 — Running header / page number bleed

**Affected pages:** ~27 | **Severity:** MINOR → MODERATE

`_strip_header_footer` removes the top 8% and bottom 8% of each page. Headers positioned close to but below the 8% threshold survive, appearing as the first line of body content.

---

### P17 — Sign errors in equations

**Affected pages:** ~39 (2 CRITICAL, 7 MAJOR, 12 MODERATE) | **Severity:** MODERATE → CRITICAL

`+` / `-` / `±` signs are flipped or dropped. Hard to detect without ground truth; constitute semantic errors rather than formatting issues.

---

### P18 — Bibliography/reference list formatting

**Affected pages:** ~21 | **Severity:** MINOR

Numbered reference list rendered as an unordered bullet list (`-`) without reference numbers. Root cause: `[LIST_ITEM]` blocks lose their ordinal in the block serialisation; Stage 4 prompt rule says `[LIST_ITEM] → - …` unconditionally.

---

### P19 — Raw LaTeX macros without math delimiters

**Affected pages:** ~17 | **Severity:** MINOR

Isolated symbols outside equation environments (e.g. `Variable ε` in the index) emitted as raw LaTeX macros (`\varepsilon`, `\mathbf{z}`) without `$…$` wrappers.

---

### Summary table

| ID | Pattern | Pages | Severity range |
|---|---|---|---|
| P1 | Equation numbers stripped | ~579 | MINOR |
| P2 | Heading level inflation | ~506 | MAJOR → CRITICAL |
| P3 | Table structure failure | ~57 | MODERATE → CRITICAL |
| P4 | Figure/caption format | ~220 | MODERATE → MAJOR |
| P5 | Superscript/subscript garbling | ~121 | MODERATE → CRITICAL |
| P6 | Footnote handling | ~92 | MODERATE → MAJOR |
| P7 | Spurious tags and stray text | ~90 | MODERATE → CRITICAL |
| P8 | Content missing | ~47 | MAJOR → CRITICAL |
| P9 | Content reordering | ~62 | MODERATE → CRITICAL |
| P10 | Mathematical operator substitution | ~60 | MODERATE → CRITICAL |
| P11 | Inline vs. display math confusion | ~77 | MODERATE → MAJOR |
| P12 | Matrix/alignment environment failures | ~37 | MODERATE → CRITICAL |
| P13 | Equation label truncation | ~50 | MODERATE → MAJOR |
| P14 | Content duplication | ~24 | MODERATE → CRITICAL |
| P15 | Blockquote / code fence misuse | ~24 | MODERATE → CRITICAL |
| P16 | Running header bleed | ~27 | MINOR → MODERATE |
| P17 | Sign errors | ~39 | MODERATE → CRITICAL |
| P18 | Bibliography list formatting | ~21 | MINOR |
| P19 | Raw LaTeX without delimiters | ~17 | MINOR |

---

---

## Part 2 — Fix Recommendations

Fixes are organised into tiers by implementation cost and projected impact. Within each tier they target the root cause in the earliest stage possible rather than patching downstream.

---

### Tier 1 — Stage 3 logic changes (no new models, highest ROI)

DONE #### FX-1 · Preserve equation numbers as `\tag{}` (fixes P1, partially P13)

**Stage:** S3 (`pipeline/equations.py`)  
**Root cause:** `_remove_eq_numbers` silently drops `(N.M)` blocks.  
**Fix:** Instead of dropping them, associate each label with the immediately preceding `EQUATION` block and append it as `\tag{N.M}` inside that block's LaTeX string. The LaTeX then contains the tag natively and the LLM only needs to echo it verbatim.

```python
# in _remove_eq_numbers — replace the drop logic with:
def _attach_eq_numbers(blocks):
    result = []
    for b in blocks:
        if b.block_type == BlockType.EQUATION and _RE_EQ_NUMBER.match(b.raw_text):
            # find and annotate the last equation block
            for prev in reversed(result):
                if prev.block_type == BlockType.EQUATION and prev.latex:
                    tag = b.raw_text.strip().strip("()")
                    if not prev.latex.endswith(f"\\tag{{{tag}}}"):
                        prev.latex = prev.latex.rstrip() + f"\n\\tag{{{tag}}}"
                    break
        else:
            result.append(b)
    return result
```

This does not require any model changes and fixes the most pervasive defect in the book.

---

DONE #### FX-2 · Register Footnote block type (fixes P6)

**Stage:** S3 (`pipeline/equations.py`)  
**Root cause:** `_LAYOUT_TO_BLOCKTYPE` has no "Footnote" entry; footnote regions fall through to `UNKNOWN`.  
**Fix:** Add the mapping and a new `BlockType`, then add a prompt rule in S4.

```python
# pipeline/models.py
class BlockType(str, Enum):
    ...
    FOOTNOTE = "footnote"  # add this

# pipeline/equations.py
_LAYOUT_TO_BLOCKTYPE = {
    ...
    "Footnote": BlockType.FOOTNOTE,   # add this
}
```

```python
# pipeline/correction.py — in _SYSTEM_PROMPT add:
"- [FOOTNOTE]        → [^1]: <text> (markdown footnote; number incrementally per page)\n"
```

Surya's LayoutPredictor already detects and labels footnote regions. This fix just needs to route them correctly.

---

DONE #### FX-3 · Use Surya's ReadingOrderPredictor (fixes P9)

**Stage:** S3 (`pipeline/equations.py`) and S2 (`pipeline/ocr.py`)  
**Root cause:** `reorder_columns` in `layout.py` uses a manual x-center gap heuristic that fails for complex page layouts (figures mid-column, mixed single/two-column pages, etc.)  
**Fix:** Replace the heuristic with Surya's dedicated `ReadingOrderPredictor`, which uses a trained model to assign correct reading order to detected layout regions. Use the ordered layout bounding boxes to sort OCR blocks.

```python
from surya.ordering import OrderPredictor

# After layout detection in process_equations():
order_predictor = OrderPredictor()
ordered = order_predictor([image], [layout_boxes])
# Sort blocks by their position index in ordered[0].bboxes
```

This generalises correctly to three-column indices, two-column chapters with figure interruptions, and mixed layouts — none of which the current heuristic handles.

---

DONE #### FX-4 · Increase running-header strip margin (fixes P16)

**Stage:** S3 (`pipeline/equations.py`)  
**Root cause:** `_strip_header_footer` uses an 8% top margin. Running headers positioned between 8–12% of page height bleed through.  
**Fix:** Raise the top threshold to 10–12%. For documents with tall headers (some textbooks have chapter-title + section-title running heads stacked), make this configurable via `config.py` rather than hardcoded.

```python
# config.py
HEADER_STRIP_RATIO = 0.10   # was 0.08
FOOTER_STRIP_RATIO = 0.90   # was 0.90 (keep)
```

---

### Tier 2 — Stage 4 prompt and post-correction fixes (no new models)

DONE #### FX-5 · Extend heading rules to full depth (fixes P2)

**Stage:** S4 (`pipeline/correction.py`)  
**Root cause:** The `_SYSTEM_PROMPT` heading rule stops at `N.M.P → ###`. Deeper levels and informal headings are not constrained.  
**Fix:** Replace the heading rule in `_SYSTEM_PROMPT` with:

```
- [HEADING] → heading level by depth:
  'Chapter N' or single integer → ##
  'N.M' (one dot) → ##
  'N.M.P' (two dots) → ###
  'N.M.P.Q' or deeper (three+ dots) → ####
  No number, or bold phrase not matching the above → plain paragraph (**not** a heading)
  NEVER promote a [TEXT] block to a heading regardless of its content.
```

Also add to `_fix_leading_text_heading` a pass that demotes any `####` or deeper heading that matches a section number with three or more dots and was incorrectly elevated.

---

DONE #### FX-6 · Fix figure format enforcement (fixes P4)

**Stage:** S4 (`pipeline/correction.py`)  
**Root cause:** The LLM sporadically puts caption text on the `> [Figure]` line, inverts order, or uses italics/bullets.  
**Fix (two-part):**

Part A — add an explicit example to the prompt:
```
FIGURE FORMAT EXAMPLE (follow exactly):
  Input:  [FIGURE+CAPTION] Fig. 3.1. Electron density as a function of radius.
  Output: > [Figure]
          Fig. 3.1. Electron density as a function of radius.
  (Caption is a plain paragraph immediately below. NEVER on the same line as > [Figure].)
```

Part B — add a post-correction normaliser in Stage 4 after `_fix_figure_hallucination`:

```python
def _fix_figure_format(markdown: str) -> str:
    """Ensure > [Figure] is always followed by caption on the next line, not inline."""
    lines = markdown.splitlines()
    result = []
    for i, line in enumerate(lines):
        m = re.match(r'^(>\s*\[Figure\])\s+(.+)$', line, re.IGNORECASE)
        if m:
            result.append(m.group(1))          # > [Figure] alone
            result.append("")                   # blank line
            result.append(m.group(2).strip("*"))  # caption as plain text
        else:
            # Unwrap italic-only caption lines after a figure
            if result and _RE_FIGURE_LINE.match(result[-1]) and re.match(r'^\*(.+)\*$', line):
                result.append(line.strip("*"))
            else:
                result.append(line)
    return "\n".join(result)
```

---

DONE #### FX-7 · Suppress structural tag leakage (fixes P7a)

**Stage:** S4 (`pipeline/correction.py`)  
**Fix (two-part):**

Part A — add to `_SYSTEM_PROMPT`:
```
CRITICAL: NEVER emit block-type tags ([TEXT], [EQUATION], [HEADING], [FIGURE],
[CAPTION], [TABLE], [LIST_ITEM], [FOOTNOTE], [UNKNOWN]) in your output.
These are INPUT annotations only.
```

Part B — add a post-correction scrubber in Stage 4:
```python
_RE_BLOCK_TAG = re.compile(
    r'^\[(?:TEXT|EQUATION|HEADING|FIGURE|CAPTION|TABLE|LIST_ITEM|FOOTNOTE|UNKNOWN)[^\]]*\]\s*',
    re.MULTILINE
)

def _scrub_block_tags(markdown: str) -> str:
    return _RE_BLOCK_TAG.sub("", markdown)
```

Apply `_scrub_block_tags` in `correct_page` after `_fix_leading_text_heading`.

---

DONE #### FX-8 · Add content coverage check (fixes P8)

**Stage:** S4 (`pipeline/correction.py`)  
**Fix:** After LLM correction, compare the word count of significant input blocks to the output. If the ratio is below a threshold, log a warning and optionally retry with a "you dropped content, reproduce everything" follow-up message.

```python
def _coverage_ok(blocks: list[TextBlock], markdown: str, min_ratio: float = 0.50) -> bool:
    input_words = sum(
        len(b.raw_text.split())
        for b in blocks
        if b.block_type in (BlockType.TEXT, BlockType.HEADING, BlockType.LIST_ITEM)
    )
    output_words = len(markdown.split())
    return input_words == 0 or (output_words / input_words) >= min_ratio
```

If coverage fails, retry once with an appended message: `"WARNING: your previous output was missing content. Reproduce ALL blocks from the input."` Flag uncovered pages in Stage 5 with `<!-- WARNING: page N may be truncated -->`.

---

DONE #### FX-9 · Inline vs. display math rule (fixes P11)

**Stage:** S4 (`pipeline/correction.py`)  
**Fix:** Add to `_SYSTEM_PROMPT`:
```
MATH DISPLAY RULES:
- $$...$$ (display) only for standalone equations on their own line.
- $...$ (inline) for all math embedded within a sentence or paragraph.
- If an expression is surrounded by prose on the same line in the input, it is inline.
- NEVER break a sentence with $$...$$.
```

---

DONE #### FX-10 · Preserve ordered lists (fixes P18)

**Stage:** S3 + S4  
**Fix:** In Stage 3's block serialiser, detect `LIST_ITEM` blocks whose text starts with `\d+\.\s` and emit them as `[ORDERED_ITEM] N. text`. Add prompt rule: `[ORDERED_ITEM] → N. <text>` (numeric list item, not bullet). This preserves bibliography numbering without requiring document-type knowledge.

---

### Tier 3 — Stage 5 post-assembly cleanup (fixes downstream artefacts)

DONE #### FX-11 · Deduplicate content (fixes P14)

**Stage:** S5 (`pipeline/assemble.py`)  
**Fix:** Within each page's content, detect and remove verbatim duplicate blocks (equations, paragraphs). A simple normalise-and-compare pass per page is sufficient:

```python
def _deduplicate(content: str) -> str:
    seen: set[str] = set()
    result: list[str] = []
    for block in re.split(r'\n{2,}', content):
        key = block.strip()
        if key and key not in seen:
            seen.add(key)
            result.append(block)
    return "\n\n".join(result)
```

---

DONE #### FX-12 · Strip spurious blockquotes and code fences (fixes P15)

**Stage:** S5 (`pipeline/assemble.py`) or S4 post-correction  
**Fix:** After assembly, scan for:
1. `>` blockquote lines that do not contain `[Figure]` — unwrap them.
2. Code fences enclosing LaTeX (`$$`) or long prose paragraphs — unwrap them.

```python
def _unwrap_spurious_containers(markdown: str) -> str:
    # Unwrap blockquotes not containing [Figure]
    lines = markdown.splitlines()
    result = []
    for line in lines:
        if re.match(r'^>\s', line) and not re.search(r'\[Figure\]', line, re.IGNORECASE):
            result.append(line[2:])   # strip "> "
        else:
            result.append(line)
    content = "\n".join(result)
    # Unwrap code fences enclosing math or prose
    content = re.sub(
        r'```\w*\n((?:[^`]|\n)*?\$\$(?:[^`]|\n)*?)```',
        r'\1', content
    )
    return content
```

---
DONE

### Tier 4 — Replace equation OCR with UniMERNet (fixes P5, P10, P12, P13, P17)

**Stage:** S3 (`pipeline/equations.py`)  
**Root cause:** Surya's LaTeX OCR (general recognition model with `block_without_boxes` task) is not purpose-trained for mathematical expressions. It misreads superscripts, substitutes visually similar symbols, and struggles with matrix environments.

**Fix:** Replace the equation crop recognition step with [UniMERNet](https://github.com/opendatalab/UniMERNet), a model purpose-trained on ~2.5M mathematical expressions. UniMERNet significantly outperforms general-purpose recognition models on formula recognition (demonstrated in both MinerU2.5 and OmniDocBench CVPR 2025 evaluations).

```python
# pipeline/equations.py — swap the LaTeX OCR phase
from unimernet.common.config import Config
from unimernet.tasks import setup_task

def _load_unimernet():
    cfg = Config.from_file("path/to/unimernet.yaml")
    model = setup_task(cfg).build_model(cfg)
    model.eval()
    return model
```

UniMERNet handles:
- Standard isolated equations (`SPE` task)  
- Scanning noisy equations (`SCE` task)
- Multi-line/aligned environments (`HWE` task)

No pipeline restructuring is needed — only the recognition step inside the equation phase changes. Bra-ket and operator-substitution issues (P10) largely disappear because UniMERNet is trained on physics/math literature.

---

### Tier 5 — Replace table structure recognition with TATR (fixes P3)

**Stage:** S3, new sub-stage between layout detection and block assignment  
**Root cause:** Surya's layout model detects table bounding boxes but returns no internal row/column structure. Individual cells arrive as TEXT blocks and the LLM cannot reliably reconstruct table structure from linearised text fragments.

**Fix:** After Surya identifies a TABLE region, run [Microsoft Table Transformer (TATR)](https://github.com/microsoft/table-transformer) on the cropped table image to extract the row/column grid. Then reconstruct a Markdown table using the OCR text assigned to each cell, rather than asking the LLM to do it.

```python
# new pipeline/table_structure.py
from transformers import TableTransformerForObjectDetection

def extract_table_structure(table_crop: Image.Image, ocr_blocks: list[TextBlock]) -> str:
    """Use TATR to find rows/cols, assign OCR blocks to cells, emit Markdown table."""
    model = TableTransformerForObjectDetection.from_pretrained(
        "microsoft/table-transformer-structure-recognition-v1.1-all"
    )
    # ... cell assignment logic ...
    return markdown_table
```

For multi-page tables, pass the column headers detected on the first table page to subsequent pages as a prior, rather than re-detecting them from scratch each page.

---

### Tier 6 — Upgrade Stage 4 to a vision-language model (fixes P2, P4, P7, P8, P9)

**Stage:** S4 (`pipeline/correction.py`)  
**Root cause:** `qwen2.5vl:3b` is a text-only LLM. It cannot verify its markdown output against the source page. This enables hallucination, content drops, and heading misidentification that a model with visual grounding would catch.

**Fix:** Switch to `qwen2.5vl:3b` (already used in the main `ingest.py` pipeline) and pass both the page image AND the block list to the model. The image provides ground truth that prevents the model from inventing headings or dropping equations.

```python
# pipeline/correction.py
import base64

def _encode_image(png_path: Path) -> str:
    return base64.b64encode(png_path.read_bytes()).decode()

def correct_page(page_num, blocks, model="qwen2.5vl:3b", image_path=None):
    messages = [{"role": "system", "content": _SYSTEM_PROMPT}]
    user_parts = [{"type": "text", "text": build_prompt(blocks)}]
    if image_path and image_path.exists():
        user_parts.insert(0, {
            "type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{_encode_image(image_path)}"}
        })
    messages.append({"role": "user", "content": user_parts})
    ...
```

With visual grounding:
- Heading levels can be inferred from visual font size
- Missing content is visible directly
- Structural tags cannot appear in output that matches the page image
- Figure/caption spatial relationship is unambiguous

This is the single change with the widest fix coverage across patterns.

---

### Tier 7 — Full pipeline replacement with MinerU (all patterns)

For documents where quality must be maximised and pipeline rebuild time is available, consider replacing Stages 1–4 with [MinerU](https://github.com/opendatalab/MinerU). MinerU2.5 was the top performer on OmniDocBench (CVPR 2025), outperforming GPT-4o, Gemini-2.5 Pro, Qwen2.5-VL-72B, Marker, Nougat, and Docling across text, formula, table, and reading-order metrics.

MinerU bundles dedicated models for each component that this pipeline addresses individually:
- **DocLayout-YOLO** — layout detection with footnote, table, formula regions
- **UniMERNet** — formula recognition (see FX Tier 4)
- **TableMaster + StructEqTable** — table structure extraction (see FX Tier 5)
- **PaddleOCR** — text recognition
- **Reading order prediction** built-in (see FX-3)

The MinerU output format is Markdown + JSON, compatible with Stage 5's assembly logic. Stage 5 and the downstream RAG ingestion pipeline remain unchanged.

**Caveat:** MinerU's table recognition still shows row/column errors on the most complex scientific tables (acknowledged in their benchmarks). The TATR approach in Tier 5 can be layered on top of MinerU's table detection if needed.

---

### Fix summary and priority order

| Fix | Patterns addressed | Stage(s) | Cost |
|---|---|---|---|
| FX-1 Preserve `\tag{}` | P1, P13 | S3 | Low — code change, no new model |
| FX-2 Register Footnote block type | P6 | S3, S4 | Low — one mapping + prompt line |
| FX-7 Suppress tag leakage | P7a | S4 | Low — prompt + regex scrubber |
| FX-11 Deduplicate content | P14 | S5 | Low — post-processing only |
| FX-12 Strip spurious containers | P15 | S5 | Low — post-processing only |
| FX-4 Widen header strip | P16 | S3 | Low — config constant |
| FX-5 Extend heading rules | P2 | S4 | Low — prompt edit |
| FX-6 Figure format enforcement | P4 | S4 | Low — prompt + small normaliser |
| FX-9 Inline vs. display math rule | P11 | S4 | Low — prompt edit |
| FX-10 Preserve ordered lists | P18 | S3, S4 | Low — serialiser + prompt |
| FX-8 Coverage check | P8 | S4 | Medium — adds retry logic |
| FX-3 Surya ReadingOrderPredictor | P9 | S3 | Medium — new predictor, same library |
| FX-13 Raw LaTeX wrapper | P19 | S5 | Low — regex post-processor |
| FX-4 (Tier 4) Replace with UniMERNet | P5, P10, P12, P13, P17 | S3 | Medium — new model (~400 MB) |
| FX-5 (Tier 5) Add TATR for tables | P3 | S3 | Medium — new model (~250 MB) |
| FX-6 (Tier 6) Switch S4 to VLM | P2, P4, P7, P8, P9 | S4 | Medium — model swap, API change |
| Tier 7 Full MinerU replacement | All | S1–S4 | High — pipeline rewrite |

**Recommended sequencing:** Execute Tier 1 first (one afternoon, no model downloads, fixes 7 patterns). Then Tier 2 (prompt edits, another afternoon, fixes 5 more patterns). These two tiers together address the majority of MINOR, MODERATE, and many MAJOR issues at near-zero cost. Tier 4 (UniMERNet) provides the highest marginal return for math-heavy documents. Tier 6 (VLM in S4) is the highest-leverage architectural change and should be evaluated before committing to a full Tier 7 rewrite.
