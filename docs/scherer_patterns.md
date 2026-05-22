# OCR Pipeline — Defect Patterns (Scherer Textbook, 640 pages)

Derived from systematic page-by-page visual audit comparing Stage 5 corrected markdown against the original rendered PNG images. Each pattern is a root cause that produces issues; severity depends on context and is noted for every pattern.

---

## Reading guide

- **Affected pages** = number of audit files where the pattern appears (some pages have multiple patterns).
- Severity ratings span the range this pattern produces in practice; the same root cause can be MINOR in a straightforward context and CRITICAL when it compounds with structure loss.
- Patterns are ordered roughly by impact (prevalence × worst-case severity).

---

## P1 — Equation numbers stripped universally

**Affected pages:** ~579 (every page with displayed equations)  
**Severity range:** MINOR (universal baseline)

Every numbered displayed equation (`(N.M)` in the source) loses its label in the markdown output. The equations themselves are present, but the parenthesised number that identifies them for cross-reference is absent. Because this affects nearly every content page it is the most pervasive single defect.

**When it escalates:** When an equation is referred to by number in surrounding prose (e.g. "see Eq. 5.14") the reference becomes a dangling pointer. The defect stays MINOR only when references are purely contextual ("the equation above").

---

## P2 — Heading level inflation

**Affected pages:** ~506  
**Severity range:** MAJOR (routine) → CRITICAL (when combined with false structure)

The pipeline assigns headings one or two levels higher than the source hierarchy:

| Source element | Expected level | Typical output |
|---|---|---|
| Chapter N | `##` | `#` |
| Section N.M | `##` | `#` or `##` (correct) |
| Subsection N.M.P | `###` | `##` or `#` |
| Sub-subsection N.M.P.Q | `####` | `##` or `###` |
| "Problems" section | `##` | `#` or `**Problems**` (bold non-heading) |
| Individual problem N.M | `###` | `#` or `##` |
| Appendix heading | `##` | `#` |
| Index / References heading | `##` | `#` |

**Sub-pattern P2a — Informal/unnumbered run-in headings inflated (~37 pages, MAJOR):** Bold phrases that act as paragraph labels in the source (e.g. "Dressed Exciton", "Solitonic Solution", "Delocalized Soliton Ansatz") are promoted to `##` or `#` headings. These have no heading markup in the source.

**Sub-pattern P2b — False headings invented by the LLM (~12 pages, MAJOR–CRITICAL):** The pipeline fabricates heading text from prose fragments that are not headings in the source (e.g. "Given Derivative Values", "Angles"). This creates structure that does not exist.

**Sub-pattern P2c — Duplicate headings (~7 pages, MAJOR–CRITICAL):** The same heading appears twice — once from the pipeline's structure detection and once from the LLM correction pass.

---

## P3 — Table structure failure

**Affected pages:** ~57 (27 CRITICAL, 14 MAJOR, 16 MODERATE)  
**Severity range:** MODERATE → CRITICAL

Tables are the single most failure-prone element. Several distinct failure modes occur, often together:

| Failure mode | Typical severity | Pages |
|---|---|---|
| Column headers swapped or promoted to data cells | CRITICAL | ~20 |
| Entire table content collapsed into a single pipe-delimited row or cell | CRITICAL | ~12 |
| Columns absent (e.g. "Pages" column missing throughout Appendix B) | CRITICAL | ~10 |
| Rows merged; multiple entries concatenated into one cell | MAJOR | ~14 |
| Extra spurious columns created from data values | MAJOR | ~8 |
| Partial rows lost; table truncated | MODERATE | ~16 |

Multi-page tables (those spanning several consecutive pages) degrade further: each page reconstructs the column structure independently, so the schema shifts page-to-page. The Appendix B methods table (pages 0616–0623, 8 consecutive pages) is completely unrecoverable across all 8 pages.

---

## P4 — Figure/caption format failures

**Affected pages:** ~220 (0 CRITICAL, 16 MAJOR, 114 MODERATE)  
**Severity range:** MODERATE (typical) → MAJOR (when figure content is entirely absent)

The expected figure format is:
```
> [Figure]

Caption text as a plain paragraph below.
```

Five distinct failure modes are observed:

| Failure mode | Severity | Pages |
|---|---|---|
| No `> [Figure]` placeholder at all; caption appears as plain prose | MAJOR | ~106 |
| Caption placed on the same line as `> [Figure]` (inline) | MODERATE | ~26 |
| Caption placed *before* the `> [Figure]` placeholder (inverted) | MODERATE | ~25 |
| Caption rendered as markdown list items (`*` or `-`) | MODERATE | ~30 |
| Caption in italic markdown (`*caption*`) | MODERATE | ~63 |
| Figure label (e.g. "Fig. 3.1") absent from caption | MODERATE | ~15 |

These modes compound: a figure can simultaneously be missing its placeholder, have an italic caption rendered as a list item, and have its label omitted.

---

## P5 — Superscript/subscript garbling in LaTeX

**Affected pages:** ~121 (8 CRITICAL, 84 MAJOR, 80 MODERATE)  
**Severity range:** MODERATE → CRITICAL

The pipeline misreads or mistranscribes exponent and index notation:

- **Exponent confusion:** `²` rendered as `2` in plain text; `³` rendered as `3 \quad` (with spacing command) instead of `\sqrt[3]{…}`.
- **Subscript collision:** when a subscript and a nearby summation variable share the same letter, one index overwrites the other (e.g. `\sum_{i=0}^{N}` where the bound `i` replaces the summation variable `i`).
- **Nested super/subscripts dropped:** `κr_{1a}` becomes `\kappa_{I_1a}` (spurious capital I inserted, structure changed).
- **Fraction–radical confusion:** cube root rendered as `3 \quad \overline{…}` (number + overline) rather than `\sqrt[3]{…}`.

Severity escalates to CRITICAL when the garbling produces a mathematically wrong formula (wrong exponent in denominator, wrong index on a sum) rather than merely a rendering defect.

---

## P6 — Footnote handling failures

**Affected pages:** ~92 (1 CRITICAL, ~40 MAJOR, ~43 MODERATE)  
**Severity range:** MODERATE → MAJOR

Three distinct modes:

| Mode | Severity | Pages |
|---|---|---|
| Footnote text absorbed inline into body paragraph, losing its footnote identity | MAJOR | ~64 |
| Footnote entirely omitted (text and marker both absent) | MAJOR | ~27 |
| Superscript footnote call-out (`¹`) carried into body text as a stray character | MODERATE | ~47 |

The absorbed-inline case is the most damaging for retrieval: footnote content (often a bibliographic citation or a clarifying exception) becomes indistinguishable from the body sentence it was appended to.

---

## P7 — Spurious structural prefix tags and stray text

**Affected pages:** ~90 (11 CRITICAL, 51 MAJOR, 64 MODERATE)  
**Severity range:** MODERATE → CRITICAL

Two sub-patterns with a shared root cause (the LLM correction pass emitting debugging/structural annotations):

**P7a — `[TEXT]`, `[EQUATION]`, `[HEADING]` prefix tags (~11 CRITICAL pages):** One or more lines, or occasionally an entire page, has every line prefixed with a structural annotation tag. These appear to be intermediate LLM reasoning tokens that leaked into the output. When applied page-wide this makes the entire output unparseable.

**P7b — Stray axis labels, figure numbers, and metadata in prose (~51 MAJOR):** Short strings that were isolated elements in the source (axis tick labels, figure identifiers, algorithm step numbers) appear as freestanding sentences in the body text.

---

## P8 — Content missing (partial or total page loss)

**Affected pages:** ~47 (18 CRITICAL, 22 MAJOR, remaining MODERATE)  
**Severity range:** MAJOR → CRITICAL

Equations, paragraphs, or entire sections are silently absent from the output with no indication of loss:

- **Entire equation runs absent:** sequences of 3–6 consecutive equations missing from the output with no placeholder (e.g. Eqs. 5.32–5.35, 6.53–6.58, 7.4–7.7, 12.26–12.30).
- **Partial page loss:** ~75% or majority of page content missing.
- **Figure completely absent:** both the `> [Figure]` placeholder and all caption text missing (distinct from P4 where some element is present).

---

## P9 — Content reordering

**Affected pages:** ~62 (9 CRITICAL, 14 MAJOR, ~39 MODERATE)  
**Severity range:** MODERATE → CRITICAL

The pipeline reads page elements out of their source order and places them in a different sequence in the output:

- **Block-level reorder (CRITICAL):** An entire section or equation group from the bottom of the page appears at the top of the output, inverting reading order.
- **Figure displacement (MAJOR):** A figure and caption that appear at the top of the source page are placed at the bottom of the output (or vice versa).
- **Equation reorder within a derivation (MAJOR):** Individual equations within a derivation sequence appear in a different order, breaking the logical flow.
- **Equation vs. prose interleaving (MODERATE):** A prose sentence that is a continuation of a derivation is placed between two equations rather than after them.

---

## P10 — Mathematical operator substitution

**Affected pages:** ~60+ (2 CRITICAL, 7–17 MAJOR, 12–23 MODERATE per sub-pattern)  
**Severity range:** MODERATE → CRITICAL

The pipeline substitutes a visually similar or contextually nearby symbol for the correct one:

| Substitution | Context | Severity |
|---|---|---|
| `∑` (sum) → `∫` (integral) | Fourier/spectral derivations | CRITICAL |
| `∑` → `\quad` (whitespace) | Dense display math | CRITICAL |
| `∏` (product) → single fraction | Product notation | CRITICAL |
| `∇` components → duplicated wrong-axis terms | Laplacian stencil | CRITICAL |
| `>` / `<` → `\rangle` / `\langle` (bra-ket) | Quantum mechanics sections | MAJOR |
| `h` → `\hbar` (or vice versa) | Quantum chapters | MAJOR |
| `&` (column separator) → `α` or other character | Matrix environments | MODERATE |
| Comparison operators (`>`, `<`, `≤`, `≥`) stripped | Algorithm pseudocode | CRITICAL |

Bra-ket substitution (`>` / `<` used instead of `\rangle` / `\langle`) affects ~35 pages in the quantum-mechanics chapters, producing MAJOR–MODERATE issues.

---

## P11 — Inline vs. display math confusion

**Affected pages:** ~77 (2 CRITICAL, 11 MAJOR, 11 MODERATE)  
**Severity range:** MODERATE → MAJOR

- **Display math mid-sentence (MAJOR):** An expression that is part of a prose sentence is wrapped in `$$…$$`, creating a paragraph break inside a sentence.
- **Inline math for displayed equations (MODERATE):** A numbered displayed equation is rendered as `$…$` inline, losing its visual prominence and block formatting.
- **Plain text instead of any math (MODERATE):** A formula like `O(h²)` or `\varepsilon` is written as unformatted text without delimiters.

---

## P12 — Matrix/alignment environment failures

**Affected pages:** ~37 (3 CRITICAL, 23 MAJOR, 24 MODERATE)  
**Severity range:** MODERATE → CRITICAL

- **Wrong alignment characters:** `\ ` (backslash-space) used instead of `\\` (row break), or `&` used in wrong column positions.
- **Matrix split into disconnected blocks:** a single matrix rendered as two separate `$$` blocks (e.g. the upper half and lower half become independent equations).
- **Matrix completely garbled:** entries replaced by garbled text, phantom characters, or raw HTML (`</math>`).
- **Nested fractions instead of matrix:** a block matrix rendered as a compound fraction structure.

---

## P13 — Equation label truncation

**Affected pages:** ~50 (4 CRITICAL, 24 MAJOR, 32 MODERATE)  
**Severity range:** MODERATE → MAJOR

Equation numbers that are present (not stripped per P1) arrive truncated:
- `(24.74)` → `(24.7)` (last digit dropped)
- `(24.)` (number cut mid-number)
- `\tag{2}` instead of `\tag{24.2}` (chapter prefix lost)

These occur alongside P1: most equation numbers are stripped entirely; the small fraction that survive are often additionally truncated.

---

## P14 — Content duplication

**Affected pages:** ~24 (4 CRITICAL, 16 MAJOR, 16 MODERATE)  
**Severity range:** MODERATE → CRITICAL

Equations, prose paragraphs, or entire derivations appear twice in the output. The duplication is verbatim. Likely caused by the LLM correction pass re-emitting content it already output in a prior step.

---

## P15 — Markdown container misuse (blockquote and code fence wrapping)

**Affected pages:** ~24 combined (3 CRITICAL, 15 MAJOR, 11 MODERATE)  
**Severity range:** MODERATE → CRITICAL

**P15a — Spurious blockquote wrapping (~17 pages):** Body prose, equations, or entire page content is wrapped in `>` blockquote syntax. This is a contamination from the figure format (where `> [Figure]` is the correct placeholder), applied incorrectly to non-figure content.

**P15b — Code fence wrapping (~7 pages):** An entire section or page is enclosed in triple-backtick fences. This suppresses all markdown rendering and makes the content unsearchable as structured text.

---

## P16 — Running header / page number bleed into body

**Affected pages:** ~27 (MINOR typical, occasional MODERATE)  
**Severity range:** MINOR → MODERATE

The pipeline strips running headers and folio numbers from most pages. On ~27 pages the strip fails partially:
- The chapter/section title from the running header appears as the first line of body text.
- A bare page number appears as a standalone number in the body.
- The chapter number appears as a spurious heading.

When the leaked header is a chapter title it can be mistaken for a new section heading, elevating severity to MODERATE.

---

## P17 — Sign errors in equations

**Affected pages:** ~39 (2 CRITICAL, 7 MAJOR, 12 MODERATE)  
**Severity range:** MODERATE → CRITICAL

Plus/minus signs are flipped, or sign terms are dropped entirely:
- `−` (minus) rendered as `+` in a potential or energy expression.
- A negation sign dropped from an exponent.
- `±` replaced by `+` only.

These are hard to detect visually at audit time and constitute semantic errors rather than formatting issues.

---

## P18 — Bibliography/reference list formatting

**Affected pages:** ~21 (all MINOR)  
**Severity range:** MINOR

The numbered bibliography is rendered as an unordered bullet list (`-`) without the reference numbers. In the first ~620 pages (chapters, appendices) the reference list is dropped entirely by the chunking filter (expected behaviour). In the References section (pages 0624–0633), the list survives but:
- Numbers are absent in most entries.
- Some entries wrap citation text in spurious square brackets.
- Number retention is erratic across pages (some entries on pages 0630–0632 keep their number, others on the same page lose it).

---

## P19 — Raw LaTeX macros without math delimiters

**Affected pages:** ~17 (all MINOR)  
**Severity range:** MINOR

Isolated Greek letters or math symbols that appear outside equation environments in the source (e.g. "Variable ε" in the index) are emitted as raw LaTeX macros (`\varepsilon`, `\mathbf{z}`) without `$…$` wrappers. They render as literal text.

---

## Summary table

| ID | Pattern | Pages affected | Severity range |
|---|---|---|---|
| P1 | Equation numbers stripped | ~579 | MINOR |
| P2 | Heading level inflation | ~506 | MAJOR → CRITICAL |
| P3 | Table structure failure | ~57 | MODERATE → CRITICAL |
| P4 | Figure/caption format | ~220 | MODERATE → MAJOR |
| P5 | Superscript/subscript garbling | ~121 | MODERATE → CRITICAL |
| P6 | Footnote handling failures | ~92 | MODERATE → MAJOR |
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
| P17 | Sign errors in equations | ~39 | MODERATE → CRITICAL |
| P18 | Bibliography list formatting | ~21 | MINOR |
| P19 | Raw LaTeX without delimiters | ~17 | MINOR |
