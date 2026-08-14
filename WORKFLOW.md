# WORKFLOW.md — Triggers, Prompts & Actions

> Operating manual for the split layout. Each row is **event → prompt → action**.
> Goal: make re-grounding a reflex. Files referenced:
> `ANCHOR.md` (always loaded) · `CLAUDE.md` (rules) · `STATE.md` (volatile) ·
> `SPEC.md` / `ARCHITECTURE.md` (read by section) · `wiki/INDEX.md` + per-record `wiki/` files (CON-/RUL-/DEC-).

---

## Session lifecycle

### ▶ Session start — *re-ground*
**Event:** opening a new session / new day. The SessionStart hook auto-injects
the Current Focus; run the command to fully re-anchor.
**Prompt:** `/reground`
**Action:** reads ANCHOR + STATE + wiki index, restates north star, binding
constraints, and current task before any code.

### ■ Session end — *handoff*
**Event:** wrapping up.
**Prompt:**
> "Update STATE.md: done / in-progress / next / open questions / blockers / files
> touched / run commands. If any decision became permanent, promote it into
> ANCHOR.md's Locked Decisions list."

**Action:** writes the recovery point the next session (and the hook) reads.

---

## Context-pressure events

### ⟳ Approaching ~60% context — *proactive compaction*
**Event:** context bar near 60%. Don't wait for the auto-trigger — it fires late
(version-dependent, roughly 77–95% utilization). To make proactive compaction
automatic, set `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=60` instead of watching the bar.
**Prompt:**
> "/compact Preserve the North Star, all Locked Decisions, the Current Focus from
> STATE.md, files modified this session, and the test commands. Drop verbose tool
> output and resolved tangents."

**Action:** lossy summary that keeps the foundation, not the noise.

### ⟲ After compaction / context feels off — *re-anchor*
**Event:** just compacted, or Claude contradicts earlier work.
**Prompt:** `/reground`
**Action:** rebuilds alignment from disk instead of degraded memory.

### ✖ Context poisoned — *hard reset*
**Event:** Claude keeps reverting to a wrong assumption despite correction.
**Sequence:** update STATE.md → `/clear` → `/reground`
**Action:** clean slate; CLAUDE.md + `@ANCHOR.md` reload automatically, the hook
re-injects Current Focus, `/reground` restores intent.

---

## Per-document triggers

### ANCHOR.md — *invariant promotion*
**Event:** a decision becomes truly non-negotiable.
**Prompt:**
> "Promote DEC-XXXX to ANCHOR.md's Locked Decisions with a one-line summary + link.
> Keep ANCHOR.md under one screen — detail stays in the DEC file."

### SPEC.md — *scope guard* (read by section)
**Event:** about to add or change a capability.
**Prompt:**
> "Read only the relevant section of SPEC.md. Is <X> in scope or a Non-Goal? If it
> expands scope, stop and flag before implementing."

### ARCHITECTURE.md — *boundary guard* (read by section)
**Event:** a change touches a component boundary, contract, or data flow.
**Prompt:**
> "Read the relevant ARCHITECTURE.md section. Does this alter a boundary or contract?
> If so, propose the ARCHITECTURE.md edit first, then the code."

### wiki/ — *knowledge capture & rejection guard*
**Event A — proposing an approach:**
> "Read wiki/INDEX.md. Has <X> or a near-variant already been Accepted or
> Rejected? Open the specific DEC file before deciding."

**Event B — a decision is made:**
> "We chose <X> over <Y> because <Z>. Create wiki/DEC-XXXX.md from
> wiki/_TEMPLATE-decision.md (status Accepted, include the rejected alternative
> and rationale), then run `python3 wiki/build_index.py`."

**Event C — an idea is killed:**
> "We're rejecting <approach>. Create wiki/DEC-XXXX.md with status Rejected,
> reason, and revisit_if: 'do not revisit', then rebuild the index."

**Event D — a concept needs a canonical definition:**
> "We keep re-explaining <term>. Create wiki/CON-XXXX.md from
> wiki/_TEMPLATE-concept.md (definition, why it matters, boundaries), add the
> term to GLOSSARY.md linking the page, then rebuild the index."

**Event E — a binding rule is established:**
> "From now on, <constraint>. Create wiki/RUL-XXXX.md from
> wiki/_TEMPLATE-rule.md (rule, rationale, exceptions; link the DEC that
> produced it if any), then rebuild the index."

### GLOSSARY.md — *naming guard*
**Event:** introducing or arguing about a name.
**Prompt:**
> "Check GLOSSARY.md for an existing term. If new, add it with a one-line canonical
> definition."

---

## Drift correction (when *you* catch it)

**Event:** Claude proposes something contradicting a decision/spec.
**Prompt:**
> "That contradicts <DEC-XXXX / FR-N>. Stop, re-read that entry, realign. If you
> think the decision itself is wrong, say so explicitly — don't quietly route around it."

---

## Automation in place

- **CLAUDE.md** auto-loads every session and after `/clear`; `@ANCHOR.md` pulls the
  invariants in with it. No action needed.
- **SessionStart hook** (`.claude/settings.json` → `.claude/hooks/session-start.sh`)
  injects STATE.md's Current Focus + git branch at startup. stdout reaches Claude
  directly on exit 0 (silently since Claude Code 2.1.0 — it won't appear in the
  visible transcript, but Claude sees it).
- **/reground** (`.claude/commands/reground.md`) is the one-keystroke re-anchor ritual.
- **wiki/build_index.py** regenerates `wiki/INDEX.md` from frontmatter —
  run it after every add/change (or wire it to a PostToolUse hook later).
  `python3 wiki/build_index.py --test` runs its built-in regression tests.
