# Ingestion Audit Prompt — RAG Pipeline

## Context

You are auditing the ingestion pipeline for a RAG system. You will be given three files representing the same document at three stages of the pipeline:

1. **Original source file** — the raw input (PDF, HTML, DOCX, etc.)
2. **Marker markdown file** — the output of Marker's document conversion
3. **Vectorized chunks file** — the final chunked content that was embedded and stored in the vector store

Your job is to evaluate fidelity and quality at each stage transition and produce a structured audit report.

---

## Files

- Original: `<ORIGINAL_FILE>`
- Marker output: `<MARKER_FILE>`
- Vectorized chunks: `<CHUNKS_FILE>`

---

## Audit Instructions

Work through each section below in order. Be precise and evidence-based — quote specific lines or values when reporting issues. Do not infer or assume correctness; flag uncertainty explicitly.

---

### Stage 1 — Original → Marker Markdown (Conversion Fidelity)

#### 1.1 Content Completeness
- [ ] Are all sections and headings from the original present in the markdown?
- [ ] Are named entities (names, dates, IDs, codes, identifiers) preserved exactly?
- [ ] Are all numerical values, measurements, formulas, and percentages unaltered?
- [ ] Are tables and lists present and correctly structured in markdown syntax?

For each failure: quote the original value and what appears in the markdown.

#### 1.2 Structural Fidelity
- [ ] Does the heading hierarchy (H1/H2/H3) match the logical structure of the original?
- [ ] Do paragraphs stay coherent — no mid-sentence or mid-concept splits?
- [ ] Are internal cross-references (e.g. "see Section 3.2", "as defined in Table 1") still meaningful after conversion?

#### 1.3 Noise and Artifact Introduction
- [ ] Is there any content in the markdown that does not exist in the original (hallucinated text, OCR artifacts)?
- [ ] Are there encoding issues — garbled characters, broken unicode, ligature failures (e.g. `ﬁ` instead of `fi`)?
- [ ] Is there stray markdown syntax from misinterpreted source formatting (e.g. spurious `*`, `#`, `---`)?

---

### Stage 2 — Marker Markdown → Vectorized Chunks (Chunking Quality)

#### 2.1 Chunk Coherence
- [ ] Does each chunk contain a complete, self-contained unit of meaning?
- [ ] Are there chunks split mid-sentence or mid-concept?
- [ ] Are there chunks so short they carry no standalone semantic value (e.g. a single heading with no body)?

#### 2.2 Context Orphaning
- [ ] Are there chunks that reference context not included in the chunk itself?
  - Examples: "as shown above", "the following table", "see previous section", pronouns with no antecedent
- [ ] Are there tables or lists that were split across multiple chunks, breaking their structure?

#### 2.3 Embedding Readiness
- [ ] Can each chunk be understood and answered in isolation, without requiring adjacent chunks?
- [ ] Does the chunk contain enough domain signal (entities, keywords, relationships) to be retrieved for relevant queries?
- [ ] Are there chunks that are pure boilerplate (headers, footers, page numbers, disclaimers) with no retrieval value?

---

### Stage 3 — End-to-End Signal

#### 3.1 Generate Test Pairs
For each substantive chunk that passed the checks above, generate one test pair:

```
Question: <a natural language question answerable from this chunk>
Context: <the exact chunk text>
Reference Answer: <the correct answer, grounded strictly in the chunk>
Question Type: <single-hop-specific | single-hop-abstract | multi-hop-specific | multi-hop-abstract>
```

Only generate test pairs for chunks that are coherent, self-contained, and free of artifacts. Skip boilerplate and orphaned chunks.

#### 3.2 Failure Summary
List all issues found, grouped by stage:

```
## Conversion Issues (Original → Marker)
- [COMPLETENESS] ...
- [STRUCTURE] ...
- [NOISE] ...

## Chunking Issues (Marker → Vector Store)
- [COHERENCE] ...
- [ORPHAN] ...
- [READINESS] ...
```

#### 3.3 Scores
Rate each stage 1–5 and provide a one-line justification:

| Stage | Score | Notes |
|---|---|---|
| Conversion fidelity (Original → Marker) | /5 | |
| Chunking quality (Marker → Chunks) | /5 | |
| Overall embedding readiness | /5 | |

---

## Output Format

Return the full audit report in markdown with all sections filled. Where a check passes cleanly, a single `✓ Pass` is sufficient. Where issues are found, provide specific evidence.