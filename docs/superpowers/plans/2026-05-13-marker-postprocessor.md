# Marker Post-Processor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create `src/marker_postprocess.py` that cleans five Marker artifact classes from raw PDF-to-markdown output, and wire it into `ingest.py` replacing the existing ad-hoc inline cleaning.

**Architecture:** A single `clean(text: str) -> str` function runs a fixed-order pipeline of private fixers. The two existing cleaning functions (`_strip_ocr_artifacts`, `_strip_page_artifacts`) move from `ingest.py` into the new module; `ingest.py` imports them back so callers are unaffected. The integration point in `_extract_pdf()` shrinks to one `clean(raw)` call.

**Tech Stack:** Python 3.12, `re` stdlib only, pytest for tests.

---

## File Map

| File | Action | Responsibility |
|------|--------|---------------|
| `src/marker_postprocess.py` | **Create** | All Marker output cleaning logic |
| `src/ingest.py` | **Modify** | Remove inline cleaning block + `_strip_ocr_artifacts` + `_strip_page_artifacts`; import from postprocessor |
| `tests/test_marker_postprocess.py` | **Create** | Unit tests for every fixer + integration |

---

## Task 1: Scaffold `marker_postprocess.py` with the `clean()` stub

**Files:**
- Create: `src/marker_postprocess.py`
- Create: `tests/test_marker_postprocess.py`

- [ ] **Step 1: Write a failing import test**

Create `tests/test_marker_postprocess.py`:

```python
"""Unit tests for src/marker_postprocess.py."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from marker_postprocess import clean


def test_clean_is_callable():
    assert callable(clean)


def test_clean_passthrough():
    text = "hello world"
    assert clean(text) == text
```

- [ ] **Step 2: Run to confirm it fails**

```bash
PYTHONPATH=src python -m pytest tests/test_marker_postprocess.py -v
```

Expected: `ModuleNotFoundError: No module named 'marker_postprocess'`

- [ ] **Step 3: Create the stub module**

Create `src/marker_postprocess.py`:

```python
"""Post-processor for raw Marker PDF-to-markdown output.

Applies a fixed-order pipeline of fixers to clean artifact classes
that Marker introduces for any PDF document.
"""
import re


def clean(text: str) -> str:
    """Clean raw Marker markdown output. Returns cleaned markdown."""
    return text
```

- [ ] **Step 4: Run tests — expect pass**

```bash
PYTHONPATH=src python -m pytest tests/test_marker_postprocess.py -v
```

Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add src/marker_postprocess.py tests/test_marker_postprocess.py
git commit -m "feat(postprocess): scaffold marker_postprocess module with clean() stub"
```

---

## Task 2: Fixer 1 — strip `<span>` tags

**Files:**
- Modify: `src/marker_postprocess.py`
- Modify: `tests/test_marker_postprocess.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_marker_postprocess.py`:

```python
def test_strip_spans_simple():
    assert clean('<span class="x">hello</span> world') == 'hello world'

def test_strip_spans_multiline():
    text = 'before\n<span style="color:red">\ninner\n</span>\nafter'
    assert clean(text) == 'before\n\ninner\n\nafter'

def test_strip_spans_no_spans():
    assert clean('no spans here') == 'no spans here'
```

- [ ] **Step 2: Run to confirm failure**

```bash
PYTHONPATH=src python -m pytest tests/test_marker_postprocess.py::test_strip_spans_simple -v
```

Expected: FAILED (clean returns input unchanged)

- [ ] **Step 3: Implement `_strip_spans` and wire into `clean()`**

Replace `src/marker_postprocess.py` content:

```python
"""Post-processor for raw Marker PDF-to-markdown output."""
import re

_SPAN_RE = re.compile(r'<span[^>]*>.*?</span>', re.DOTALL | re.IGNORECASE)


def _strip_spans(text: str) -> str:
    return _SPAN_RE.sub('', text)


def clean(text: str) -> str:
    """Clean raw Marker markdown output. Returns cleaned markdown."""
    text = _strip_spans(text)
    return text
```

- [ ] **Step 4: Run all tests — expect pass**

```bash
PYTHONPATH=src python -m pytest tests/test_marker_postprocess.py -v
```

Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add src/marker_postprocess.py tests/test_marker_postprocess.py
git commit -m "feat(postprocess): fixer 1 — strip span tags"
```

---

## Task 3: Fixer 2+3 — double-encoded footnote sups and residual `lt;sup>` fragments

Background: Marker emits `<sup>&</sup>lt;sup>N</sup>` for footnote anchors that were already rendered as HTML in the PDF. The existing `_SUP_RE` removes `<sup>&</sup>` (the outer tag containing `&`) but leaves the literal text `lt;sup>N</sup>` behind. Both halves must be cleaned together.

**Files:**
- Modify: `src/marker_postprocess.py`
- Modify: `tests/test_marker_postprocess.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_marker_postprocess.py`:

```python
def test_fix_double_sup_removes_marker():
    # Full double-encoded pattern: <sup>&</sup>lt;sup>3</sup>
    inp = 'text<sup>&</sup>lt;sup>3</sup>more'
    assert clean(inp) == 'textmore'

def test_fix_double_sup_mid_sentence():
    inp = 'See footnote<sup>&</sup>lt;sup>12</sup> for details.'
    assert clean(inp) == 'See footnote for details.'

def test_fix_residual_lt_sup():
    # After outer <sup>&</sup> is stripped, the remainder lt;sup>N</sup> must also go
    inp = 'textlt;sup>5</sup>more'
    assert clean(inp) == 'textmore'

def test_fix_residual_lt_sup_no_false_positive():
    # Normal text with "lt" in it should not be touched
    assert clean('the result lt x is valid') == 'the result lt x is valid'
```

- [ ] **Step 2: Run to confirm failure**

```bash
PYTHONPATH=src python -m pytest tests/test_marker_postprocess.py::test_fix_double_sup_removes_marker -v
```

Expected: FAILED

- [ ] **Step 3: Implement fixers 2 and 3**

Update `src/marker_postprocess.py`:

```python
"""Post-processor for raw Marker PDF-to-markdown output."""
import re

_SPAN_RE = re.compile(r'<span[^>]*>.*?</span>', re.DOTALL | re.IGNORECASE)

# Fixer 2: double-encoded footnote sup — <sup>&</sup>lt;sup>N</sup>
# Marker wraps a pre-rendered HTML footnote anchor in another <sup> tag.
# The outer <sup>&</sup> gets removed by fixer 5, but we target the full
# double pattern here first so the lt; residue doesn't need its own pass.
_DOUBLE_SUP_RE = re.compile(r'<sup>&</sup>lt;sup>\d+</sup>', re.IGNORECASE)

# Fixer 3: residual lt;sup>N</sup> text left after outer <sup>&</sup> removal
_RESIDUAL_LT_SUP_RE = re.compile(r'lt;sup>\d+</sup>', re.IGNORECASE)


def _strip_spans(text: str) -> str:
    return _SPAN_RE.sub('', text)


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
```

- [ ] **Step 4: Run all tests — expect pass**

```bash
PYTHONPATH=src python -m pytest tests/test_marker_postprocess.py -v
```

Expected: 9 passed

- [ ] **Step 5: Commit**

```bash
git add src/marker_postprocess.py tests/test_marker_postprocess.py
git commit -m "feat(postprocess): fixers 2+3 — double-encoded and residual lt;sup footnote artifacts"
```

---

## Task 4: Fixer 4 — convert math-variable `<sup>`/`<sub>` to inline LaTeX

Background: Marker uses `<sup>X</sup>` and `<sub>X</sub>` for superscripts/subscripts in prose (e.g. `Q<sup>T</sup>`, `w<sub>i</sub>`). Content is 1–10 non-digit characters. These must become `$^{X}$` / `$_{X}$`. Digit-only content is a footnote number — leave it for fixer 5.

**Files:**
- Modify: `src/marker_postprocess.py`
- Modify: `tests/test_marker_postprocess.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_marker_postprocess.py`:

```python
def test_fix_math_sup_letter():
    assert clean('Q<sup>T</sup> Q') == 'Q$^{T}$ Q'

def test_fix_math_sup_symbol():
    assert clean('order *<sup>N</sup>*') == 'order *$^{N}$*'

def test_fix_math_sup_minus():
    assert clean('*p*(*x*) of order *<sup>−</sup> 1') == '*p*(*x*) of order *$^{−}$ 1'

def test_fix_math_sub_letter():
    assert clean('w<sub>i</sub>') == 'w$_{i}$'

def test_fix_math_sup_skips_digits():
    # Digit-only content is a footnote — must NOT be converted here
    inp = 'word<sup>2</sup>'
    assert clean(inp) == 'word'  # fixer 5 strips it

def test_fix_math_sup_multi_char():
    assert clean('<sup>-1</sup>') == '$^{-1}$'

def test_fix_math_sup_greek():
    assert clean('<sup>λ</sup>') == '$^{λ}$'
```

- [ ] **Step 2: Run to confirm failure**

```bash
PYTHONPATH=src python -m pytest tests/test_marker_postprocess.py::test_fix_math_sup_letter -v
```

Expected: FAILED (currently returns `Q<sup>T</sup> Q` unchanged)

- [ ] **Step 3: Implement fixer 4**

Update `src/marker_postprocess.py` — add the new constants and function, and insert the call in `clean()` before `_strip_footnote_sups` (which will be added in Task 5):

```python
"""Post-processor for raw Marker PDF-to-markdown output."""
import re

_SPAN_RE = re.compile(r'<span[^>]*>.*?</span>', re.DOTALL | re.IGNORECASE)
_DOUBLE_SUP_RE = re.compile(r'<sup>&</sup>lt;sup>\d+</sup>', re.IGNORECASE)
_RESIDUAL_LT_SUP_RE = re.compile(r'lt;sup>\d+</sup>', re.IGNORECASE)

# Fixer 4: math-variable sups/subs — non-digit content, 1-10 chars
# Digit-only content handled by fixer 5 (footnote stripping).
_MATH_SUP_RE = re.compile(r'<sup>(?!\d+</sup>)([^<]{1,10})</sup>', re.IGNORECASE)
_MATH_SUB_RE = re.compile(r'<sub>(?!\d+</sub>)([^<]{1,10})</sub>', re.IGNORECASE)


def _strip_spans(text: str) -> str:
    return _SPAN_RE.sub('', text)


def _fix_double_sup(text: str) -> str:
    return _DOUBLE_SUP_RE.sub('', text)


def _fix_residual_lt_sup(text: str) -> str:
    return _RESIDUAL_LT_SUP_RE.sub('', text)


def _fix_math_sups(text: str) -> str:
    text = _MATH_SUP_RE.sub(lambda m: f'$^{{{m.group(1)}}}$', text)
    text = _MATH_SUB_RE.sub(lambda m: f'$_{{{m.group(1)}}}$', text)
    return text


def clean(text: str) -> str:
    """Clean raw Marker markdown output. Returns cleaned markdown."""
    text = _strip_spans(text)
    text = _fix_double_sup(text)
    text = _fix_residual_lt_sup(text)
    text = _fix_math_sups(text)
    return text
```

- [ ] **Step 4: Run all tests — expect pass**

```bash
PYTHONPATH=src python -m pytest tests/test_marker_postprocess.py -v
```

Expected: 16 passed

- [ ] **Step 5: Commit**

```bash
git add src/marker_postprocess.py tests/test_marker_postprocess.py
git commit -m "feat(postprocess): fixer 4 — math-variable sup/sub to inline LaTeX"
```

---

## Task 5: Fixer 5 — strip remaining footnote-number `<sup>N</sup>` tags

Background: After fixer 4 converted non-digit sups to LaTeX, any remaining `<sup>N</sup>` (digit-only) are footnote markers. Strip them entirely — the footnote body is typically boilerplate and is not embedded.

**Files:**
- Modify: `src/marker_postprocess.py`
- Modify: `tests/test_marker_postprocess.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_marker_postprocess.py`:

```python
def test_strip_footnote_sup_single_digit():
    assert clean('word<sup>1</sup>') == 'word'

def test_strip_footnote_sup_two_digits():
    assert clean('word<sup>42</sup>') == 'word'

def test_strip_footnote_sup_preserves_surrounding_text():
    assert clean('before<sup>7</sup> after') == 'before after'
```

- [ ] **Step 2: Run to confirm failure**

```bash
PYTHONPATH=src python -m pytest tests/test_marker_postprocess.py::test_strip_footnote_sup_single_digit -v
```

Expected: FAILED (digit sups not yet stripped)

- [ ] **Step 3: Implement fixer 5**

Add to `src/marker_postprocess.py` after `_MATH_SUB_RE`:

```python
# Fixer 5: remaining footnote-number sups (digit-only, after fixer 4 ran)
_FOOTNOTE_SUP_RE = re.compile(r'<sup>\d+</sup>', re.IGNORECASE)
```

Add new function:

```python
def _strip_footnote_sups(text: str) -> str:
    return _FOOTNOTE_SUP_RE.sub('', text)
```

Update `clean()`:

```python
def clean(text: str) -> str:
    """Clean raw Marker markdown output. Returns cleaned markdown."""
    text = _strip_spans(text)
    text = _fix_double_sup(text)
    text = _fix_residual_lt_sup(text)
    text = _fix_math_sups(text)
    text = _strip_footnote_sups(text)
    return text
```

- [ ] **Step 4: Run all tests — expect pass**

```bash
PYTHONPATH=src python -m pytest tests/test_marker_postprocess.py -v
```

Expected: 19 passed

- [ ] **Step 5: Commit**

```bash
git add src/marker_postprocess.py tests/test_marker_postprocess.py
git commit -m "feat(postprocess): fixer 5 — strip digit-only footnote sup tags"
```

---

## Task 6: Fixer 6 — strip dangling image refs, preserve captions

Background: Marker writes `![](_page_N_Figure_N.jpeg)` for figures extracted as separate image files. These files are not in the pipeline, so the refs carry zero retrieval signal. The figure caption on the same or adjacent line (e.g. `Fig. 1.1 caption text`) must be preserved.

**Files:**
- Modify: `src/marker_postprocess.py`
- Modify: `tests/test_marker_postprocess.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_marker_postprocess.py`:

```python
def test_strip_image_ref_jpeg():
    assert clean('![](_page_3_Figure_1.jpeg)') == ''

def test_strip_image_ref_png():
    assert clean('![](_page_5_Picture_0.png)') == ''

def test_strip_image_ref_preserves_caption():
    inp = '![](_page_27_Figure_1.jpeg)\nFig. 1.1 Normalized machine numbers.'
    assert clean(inp) == '\nFig. 1.1 Normalized machine numbers.'

def test_strip_image_ref_case_insensitive():
    assert clean('![](_page_1_Figure_1.JPEG)') == ''

def test_strip_image_ref_no_false_positive():
    # A real external image with alt text should NOT be stripped
    result = clean('![diagram](https://example.com/img.png)')
    assert result == '![diagram](https://example.com/img.png)'
```

- [ ] **Step 2: Run to confirm failure**

```bash
PYTHONPATH=src python -m pytest tests/test_marker_postprocess.py::test_strip_image_ref_jpeg -v
```

Expected: FAILED

- [ ] **Step 3: Implement fixer 6**

Add constant after `_FOOTNOTE_SUP_RE`:

```python
# Fixer 6: dangling Marker image refs — empty alt text, local _page_N_ path
# Only matches Marker-generated local paths (start with _page_); preserves
# real external image links with alt text or non-Marker paths.
_MARKER_IMG_RE = re.compile(
    r'!\[\]\(_page_\d+_[^)]+\.(?:jpe?g|png|gif|webp|svg|tiff?)\)',
    re.IGNORECASE,
)
```

Add function:

```python
def _strip_image_refs(text: str) -> str:
    return _MARKER_IMG_RE.sub('', text)
```

Update `clean()`:

```python
def clean(text: str) -> str:
    """Clean raw Marker markdown output. Returns cleaned markdown."""
    text = _strip_spans(text)
    text = _fix_double_sup(text)
    text = _fix_residual_lt_sup(text)
    text = _fix_math_sups(text)
    text = _strip_footnote_sups(text)
    text = _strip_image_refs(text)
    return text
```

- [ ] **Step 4: Run all tests — expect pass**

```bash
PYTHONPATH=src python -m pytest tests/test_marker_postprocess.py -v
```

Expected: 24 passed

- [ ] **Step 5: Commit**

```bash
git add src/marker_postprocess.py tests/test_marker_postprocess.py
git commit -m "feat(postprocess): fixer 6 — strip Marker image refs, preserve captions"
```

---

## Task 7: Migrate `_strip_ocr_artifacts` and `_strip_page_artifacts` into the module

Background: These two functions currently live in `ingest.py`. Moving them into `marker_postprocess.py` makes the full cleaning pipeline self-contained. `ingest.py` imports them back, so no other behaviour changes.

**Files:**
- Modify: `src/marker_postprocess.py`
- Modify: `src/ingest.py`
- Modify: `tests/test_marker_postprocess.py`

- [ ] **Step 1: Write tests for the migrated functions**

Append to `tests/test_marker_postprocess.py`:

```python
def test_strip_br_tag():
    assert clean('cell<br/>content') == 'cell content'

def test_strip_nonbreaking_space():
    assert clean('word word') == 'word word'

def test_strip_invisible_unicode():
    # soft hyphen U+00AD
    assert clean('word­word') == 'wordword'
```

- [ ] **Step 2: Run to confirm all pass already (these are handled by existing code — migration only)**

```bash
PYTHONPATH=src python -m pytest tests/test_marker_postprocess.py -v
```

Expected: these 3 tests FAIL (clean() doesn't call `_strip_ocr_artifacts` yet)

- [ ] **Step 3: Move `_strip_ocr_artifacts` and `_strip_page_artifacts` into `marker_postprocess.py`**

Copy the two functions verbatim from `ingest.py` lines 414–466 and 483–496 into `marker_postprocess.py`, adding the one extra import they need (`collections.Counter`). Then wire them into `clean()`.

The full `src/marker_postprocess.py` after this task:

```python
"""Post-processor for raw Marker PDF-to-markdown output.

Applies a fixed-order pipeline of fixers to clean artifact classes
that Marker introduces for any PDF document.
"""
import re
from collections import Counter

# ── Fixer 1: span tags ────────────────────────────────────────────────────────
_SPAN_RE = re.compile(r'<span[^>]*>.*?</span>', re.DOTALL | re.IGNORECASE)

# ── Fixer 2: double-encoded footnote sups ─────────────────────────────────────
_DOUBLE_SUP_RE = re.compile(r'<sup>&</sup>lt;sup>\d+</sup>', re.IGNORECASE)

# ── Fixer 3: residual lt;sup> text fragments ──────────────────────────────────
_RESIDUAL_LT_SUP_RE = re.compile(r'lt;sup>\d+</sup>', re.IGNORECASE)

# ── Fixer 4: math-variable sups/subs ─────────────────────────────────────────
_MATH_SUP_RE = re.compile(r'<sup>(?!\d+</sup>)([^<]{1,10})</sup>', re.IGNORECASE)
_MATH_SUB_RE = re.compile(r'<sub>(?!\d+</sub>)([^<]{1,10})</sub>', re.IGNORECASE)

# ── Fixer 5: footnote-number sups ────────────────────────────────────────────
_FOOTNOTE_SUP_RE = re.compile(r'<sup>\d+</sup>', re.IGNORECASE)

# ── Fixer 6: dangling Marker image refs ──────────────────────────────────────
_MARKER_IMG_RE = re.compile(
    r'!\[\]\(_page_\d+_[^)]+\.(?:jpe?g|png|gif|webp|svg|tiff?)\)',
    re.IGNORECASE,
)

# ── Fixer 7a: OCR artifacts (migrated from ingest._strip_ocr_artifacts) ───────
_BR_RE = re.compile(r'<br\s*/?>', re.IGNORECASE)
_SUP_REMNANT_RE = re.compile(r'<sup>(?:&lt;sup&gt;.*?&lt;/sup&gt;|.*?)</sup>', re.IGNORECASE | re.DOTALL)
_INVISIBLE_RE = re.compile(
    r'[\xad'        # soft hyphen U+00AD
    r'​'       # zero-width space
    r'‌'       # zero-width non-joiner
    r'‍'       # zero-width joiner
    r'﻿'       # BOM / zero-width no-break space
    r']'
)

# ── Fixer 7b: page artifacts (migrated from ingest._strip_page_artifacts) ──────
_PAGE_NUMBER_RE = re.compile(r'^\s*(?:Page\s+)?\d{1,4}(?:\s+of\s+\d{1,4})?\s*$', re.IGNORECASE)
_CODE_LINE_RE = re.compile(r'^\s*\d{2,5}\s+[A-Za-z\'`]')


def _strip_spans(text: str) -> str:
    return _SPAN_RE.sub('', text)


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


def _strip_ocr_artifacts(text: str) -> str:
    text = _SUP_REMNANT_RE.sub('', text)
    text = _BR_RE.sub(' ', text)
    text = _INVISIBLE_RE.sub('', text)
    text = text.replace(' ', ' ')   # non-breaking space → regular space
    return text


def _strip_page_artifacts(text: str) -> str:
    lines = text.splitlines()
    total = len(lines)

    freq: Counter[str] = Counter()
    for line in lines:
        s = line.strip()
        if s and len(s) < 72 and not _CODE_LINE_RE.match(s):
            freq[s] += 1

    rh_threshold = max(3, total // 30)
    running_headers: set[str] = set()
    for line_text, count in freq.items():
        if count >= rh_threshold:
            if re.search(r'[\\|`${}]', line_text):
                continue
            running_headers.add(line_text)

    cleaned: list[str] = []
    for line in lines:
        s = line.strip()
        if _PAGE_NUMBER_RE.match(s):
            continue
        if s in running_headers:
            continue
        cleaned.append(line)

    return '\n'.join(cleaned)


def clean(text: str) -> str:
    """Clean raw Marker markdown output. Returns cleaned markdown."""
    text = _strip_spans(text)
    text = _fix_double_sup(text)
    text = _fix_residual_lt_sup(text)
    text = _fix_math_sups(text)
    text = _strip_footnote_sups(text)
    text = _strip_image_refs(text)
    text = _strip_ocr_artifacts(text)
    text = _strip_page_artifacts(text)
    return text
```

- [ ] **Step 4: Update `ingest.py` — remove migrated functions, add import, update `_extract_pdf`**

In `src/ingest.py`:

a) Add import near the top (after the existing imports, before `_marker_converter = None`):

```python
from marker_postprocess import clean as _marker_clean
from marker_postprocess import _strip_ocr_artifacts, _strip_page_artifacts
```

b) Delete the bodies of `_strip_ocr_artifacts` and `_strip_page_artifacts` from `ingest.py` (lines ~469–496 and ~420–466). Keep the module-level compiled regexes they used only if referenced elsewhere — check with grep first:

```bash
grep -n '_BR_RE\|_SUP_RE\|_INVISIBLE_RE\|_PAGE_NUMBER_RE' src/ingest.py
```

If only referenced inside those two functions, delete their constants too.

c) Replace the three-line cleaning block in `_extract_pdf()` (currently at the end of the function body):

```python
# REMOVE these three lines:
text = re.sub(r'<span[^>]*>.*?</span>', '', raw, flags=re.DOTALL)
text = _strip_ocr_artifacts(text)
return _strip_page_artifacts(text)

# REPLACE WITH:
return _marker_clean(raw)
```

- [ ] **Step 5: Run all tests — expect pass**

```bash
PYTHONPATH=src python -m pytest tests/test_marker_postprocess.py -v
```

Expected: 27 passed

- [ ] **Step 6: Commit**

```bash
git add src/marker_postprocess.py src/ingest.py tests/test_marker_postprocess.py
git commit -m "refactor(ingest): migrate cleaning fns to marker_postprocess; wire clean() into _extract_pdf"
```

---

## Task 8: End-to-end integration test

Write one test that sends a realistic multi-pattern Marker string through `clean()` and verifies all fixers fire correctly together.

**Files:**
- Modify: `tests/test_marker_postprocess.py`

- [ ] **Step 1: Write the end-to-end test**

Append to `tests/test_marker_postprocess.py`:

```python
def test_end_to_end_realistic_marker_output():
    """Simulate a realistic Marker output chunk with all five artifact classes."""
    raw = (
        # span tag (fixer 1)
        '<span class="bold">Introduction</span>\n\n'
        # double-encoded footnote (fixers 2+3)
        'See netbeans<sup>&</sup>lt;sup>1</sup>www.netbeans.org.\n\n'
        # math-variable sup (fixer 4)
        'The matrix *Q<sup>T</sup>* satisfies *Q<sub>i</sub>* = 1.\n\n'
        # footnote-number sup (fixer 5)
        'As shown elsewhere<sup>42</sup>, this holds.\n\n'
        # image ref (fixer 6)
        '![](_page_12_Figure_3.jpeg)\nFig. 2.1 Normalized machine numbers.\n\n'
        # br tag (fixer 7a via _strip_ocr_artifacts)
        'cell one<br/>cell two\n'
    )
    result = clean(raw)

    # span stripped, content preserved
    assert '<span' not in result
    assert 'Introduction' in result

    # double-encoded footnote gone entirely
    assert 'lt;sup>' not in result
    assert '<sup>&</sup>' not in result

    # math-variable sups converted to inline LaTeX
    assert '$^{T}$' in result
    assert '$_{i}$' in result
    assert '<sup>T</sup>' not in result

    # footnote number stripped
    assert '<sup>42</sup>' not in result

    # image ref stripped, caption kept
    assert '![](' not in result
    assert 'Fig. 2.1 Normalized machine numbers.' in result

    # br converted to space
    assert '<br' not in result
    assert 'cell one cell two' in result
```

- [ ] **Step 2: Run to confirm pass**

```bash
PYTHONPATH=src python -m pytest tests/test_marker_postprocess.py -v
```

Expected: 28 passed

- [ ] **Step 3: Commit**

```bash
git add tests/test_marker_postprocess.py
git commit -m "test(postprocess): end-to-end integration test for all five artifact classes"
```

---

## Self-Review

**Spec coverage check:**

| Spec requirement | Task |
|---|---|
| New `src/marker_postprocess.py` with `clean()` | Task 1 |
| Fixer 1: strip `<span>` tags | Task 2 |
| Fixer 2: double-encoded footnote sups | Task 3 |
| Fixer 3: residual `lt;sup>N</sup>` | Task 3 |
| Fixer 4: math-variable sup/sub → inline LaTeX | Task 4 |
| Fixer 5: footnote-number sups stripped | Task 5 |
| Fixer 6: image refs stripped, captions preserved | Task 6 |
| Fixers 7a+7b: OCR + page artifacts | Task 7 |
| Migrate `_strip_ocr_artifacts` + `_strip_page_artifacts` | Task 7 |
| Wire `_extract_pdf()` to use `clean()` | Task 7 |
| Unit test per fixer | Tasks 2–7 |
| End-to-end test | Task 8 |

All spec requirements covered. No placeholders. Type signatures consistent across all tasks (`str → str` throughout).
