# Marker Post-Processor — Design Spec

**Date:** 2026-05-13  
**Status:** Approved  
**Scope:** New `src/marker_postprocess.py` module; minimal edit to `src/ingest.py`

---

## Problem

Marker's PDF-to-markdown conversion introduces five recurring artifact classes that degrade embedding quality for any document. These are currently handled by a mix of ad-hoc regexes inside `_strip_ocr_artifacts()` in `ingest.py`, which is incomplete and hard to extend.

### Confirmed Artifact Patterns (from Scherer audit, generalise to all PDFs)

| # | Pattern | Example | Count (Scherer) |
|---|---------|---------|-----------------|
| 1 | Double-encoded footnote sup | `<sup>&</sup>lt;sup>1</sup>` | 85 |
| 2 | Residual `lt;sup>` text after sup stripping | `lt;sup>1</sup>www.example.com` | 85 (same root) |
| 3 | HTML sup/sub used for math variables in prose | `*Q<sup>T</sup> Q*`, `w*<sup>i</sup>*` | 66 |
| 4 | Dangling image refs with no content | `![](_page_27_Figure_1.jpeg)` | 255 |
| 5 | Simple footnote-number sups not stripped | `<sup>1</sup>` | 150 |

**Root cause:** Marker uses mixed-mode rendering — LaTeX inside display blocks but HTML tags for inline superscripts, footnotes, and figures. There is no single Marker config switch that eliminates these; they must be cleaned post-hoc.

---

## Solution

### New module: `src/marker_postprocess.py`

Single public entry point:

```python
def clean(text: str) -> str:
    """Apply all fixers to raw Marker markdown output in pipeline order."""
```

Private fixers run in a fixed order. Each fixer has one responsibility and receives/returns the full document string.

### Pipeline Order

Order is significant — each fixer sees the output of the previous.

```
1. _strip_spans          Strip <span ...>…</span> tags (inline today at ingest.py:562)
2. _fix_double_sup       <sup>&</sup>lt;sup>N</sup> → "" (drop footnote marker entirely)
3. _fix_residual_lt_sup  Remaining literal "lt;sup>N</sup>" text fragments → ""
4. _fix_math_sups        <sup>X</sup>/<sub>X</sub> in prose → $^X$ / $_X$ (inline LaTeX)
5. _strip_footnote_sups  Remaining <sup>N</sup> (simple digit footnotes) → ""
6. _strip_image_refs     ![](_page_N_…jpeg/png) → "" (caption text is kept)
7. _strip_ocr_artifacts  Invisible unicode, <br>, non-breaking spaces (existing fn)
8. _strip_page_artifacts Running headers/footers, bare page numbers (existing fn)
```

Steps 7 and 8 delegate to the existing functions in `ingest.py` (imported or inlined). This avoids duplicating logic and keeps the diff to `ingest.py` minimal.

### Fixer Specifications

**`_strip_spans`**  
Pattern: `<span[^>]*>.*?</span>` (DOTALL). Replace with empty string.  
Generalises to: any Marker output — spans appear when Marker preserves font/style metadata.

**`_fix_double_sup`**  
Pattern: `<sup>&</sup>lt;sup>(\d+)</sup>`  
Replace with: `""` (footnote markers carry no retrieval value; the footnote body is usually boilerplate).  
Generalises to: any PDF where Marker OCRs pre-rendered HTML footnote anchors.

**`_fix_residual_lt_sup`**  
Pattern: `lt;sup>\d+</sup>`  
This is the text fragment left after `_fix_double_sup` removes `<sup>&</sup>`. Replace with `""`.  
Generalises to: same root cause as above; applies any time `_fix_double_sup` fires.

**`_fix_math_sups`**  
Pattern: `<sup>([^<]{1,10})</sup>` and `<sub>([^<]{1,10})</sub>` where the content is not purely digits.  
Replace sup with `$^{content}$`, sub with `$_{content}$`.  
Guard: digit-only content is a footnote number, handled by `_strip_footnote_sups` instead — skip here.  
Generalises to: any math-heavy document where Marker uses HTML for inline superscripts.

**`_strip_footnote_sups`**  
Pattern: `<sup>\d+</sup>`  
Replace with `""`.  
Must run after `_fix_math_sups` so digit-only sups are not accidentally LaTeX-converted.  
Generalises to: any document with numbered footnotes.

**`_strip_image_refs`**  
Pattern: `!\[\]\([^)]+\.(?:jpe?g|png|gif|webp|svg|tiff?)[^)]*\)` (case-insensitive)  
Replace with `""`. The surrounding paragraph text (including figure captions like `Fig. 1.1 …`) is preserved.  
Generalises to: any PDF where Marker extracts figures as separate image files.

### Integration point in `ingest.py`

Replace lines 562–564:
```python
# before
text = re.sub(r'<span[^>]*>.*?</span>', '', raw, flags=re.DOTALL)
text = _strip_ocr_artifacts(text)
return _strip_page_artifacts(text)
```

With:
```python
# after
from marker_postprocess import clean as _marker_clean
return _marker_clean(raw)
```

`_marker_clean` calls `_strip_ocr_artifacts` and `_strip_page_artifacts` internally (imported from `ingest.py` or duplicated — see implementation note below).

### Implementation note: avoiding circular imports

`_strip_ocr_artifacts` and `_strip_page_artifacts` are defined in `ingest.py`. To avoid a circular import (`marker_postprocess` ↔ `ingest`), move both functions into `marker_postprocess.py` and import them back in `ingest.py`. They have no dependencies on the rest of `ingest.py`.

---

## Tests

New test file: `tests/test_marker_postprocess.py`

One test per fixer, plus one end-to-end test with a realistic multi-pattern string. Tests are pure string → string, no mocking needed.

| Test | Input | Expected output |
|------|-------|-----------------|
| `test_fix_double_sup` | `text<sup>&</sup>lt;sup>3</sup>more` | `textmore` |
| `test_fix_residual_lt_sup` | `textlt;sup>3</sup>more` | `textmore` |
| `test_fix_math_sups_sup` | `Q<sup>T</sup> Q` | `Q$^{T}$ Q` |
| `test_fix_math_sups_sub` | `w<sub>i</sub>` | `w$_{i}$` |
| `test_fix_math_sups_skips_digits` | `word<sup>2</sup>` | `word<sup>2</sup>` (unchanged, handled by next fixer) |
| `test_strip_footnote_sups` | `word<sup>42</sup>` | `word` |
| `test_strip_image_refs` | `![](_page_3_Figure_1.jpeg)\nFig. 1.1 caption` | `\nFig. 1.1 caption` |
| `test_strip_spans` | `<span class="x">hello</span>` | `hello` |
| `test_end_to_end` | Combined string with all patterns | Fully clean output |

---

## Files Changed

| File | Change |
|------|--------|
| `src/marker_postprocess.py` | **New** — full post-processor module |
| `src/ingest.py` | Move `_strip_ocr_artifacts` + `_strip_page_artifacts` to postprocessor; replace 3-line cleaning block with `_marker_clean(raw)` |
| `tests/test_marker_postprocess.py` | **New** — unit tests for all fixers |

---

## Out of Scope

- Recovering figure image content (would require a separate vision model pass)
- Fixing cross-reference orphaning at the chunking stage (separate chunker concern)
- Adding `"section"` / `"chapter"` metadata to chunks (separate metadata enrichment task)
