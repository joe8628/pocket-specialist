# ANCHOR.md — Always in Context

> The only project content loaded by default (pulled into CLAUDE.md via `@ANCHOR.md`).
> **Hard cap: one screen.** If it grows, push detail into SPEC / ARCHITECTURE /
> decisions and leave a one-line pointer here. Volatile state (current task) lives
> in STATE.md and is injected by the SessionStart hook — keep it out of this file.

## North Star

**Purpose (one sentence):**
A local-first document-intelligence pipeline that turns a heterogeneous corpus of
technical documents into typed, provenance-bearing structured extractions (CIF) —
the durable substrate for a personal, retrieval-grounded "specialist."

**This is NOT:**  *(anti-scope — drift violates these first)*
- a model-training / fine-tuning project — we consume providers, never train them
- a distributed or multi-tenant service — single-user, local-first (Phase F is deferred)
- a markdown generator — markdown is export-only, never intermediate state (DEC-0003)
- a chatbot/agent framework — it builds the substrate an agent consumes, not the agent

> **Drift watch:** the *value* is the specialist (retrieval/Phase D), but the *code*
> currently lives in the ingestion substrate (Phase A–C). Substrate-first is allowed;
> letting it become the permanent destination is the drift. See SPEC "Known Debt."

## Locked Decisions (Invariants)

> Non-negotiable. Do not contradict or re-open without an explicit instruction to
> do so. Each links to its full rationale in `wiki/<id>.md`.

- [LOCKED] Inter-stage state is the versioned, typed CIF — no raw dicts across boundaries — DEC-0001
- [LOCKED] Markdown is an export/render layer only, never intermediate state — DEC-0003
- [LOCKED] GPU residency is owned explicitly and offloaded deterministically — DEC-0004
