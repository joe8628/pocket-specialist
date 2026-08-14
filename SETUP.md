# SETUP.md — Bootstrapping the System

> How to install and start filling this anti-drift system. Steps without a label
> apply to **both** new and existing projects as-is. Where they differ, the step
> splits into **New —** and **Existing —** branches.
>
> Fill in priority order, not all at once: ANCHOR (step 3) and STATE (step 8) are
> what actually fight drift — get those right first. SPEC, ARCHITECTURE, and
> GLOSSARY can be backfilled incrementally and don't need to be perfect on day one.

---

## 1. Place the scaffold at the repo root
Copy every file, preserving the `wiki/` and `.claude/` subfolders:

```
CLAUDE.md                  # rules + `@ANCHOR.md` import; auto-loads, survives /clear
ANCHOR.md                  # north star + locked decisions ONLY (≤ 1 screen)
STATE.md                   # volatile session state; read whole; hook injects Current Focus
SPEC.md                    # the what/why; read by section (has a TOC)
ARCHITECTURE.md            # the how; read by section (has a TOC)
GLOSSARY.md                # canonical one-line terms (deep ones link to wiki/CON-XXXX.md)
WORKFLOW.md                # event → prompt → action trigger manual
SETUP.md                   # this file
wiki/
  INDEX.md                 # AUTO-GENERATED, grouped: Concepts / Rules / Decisions / Rejected Ideas
  _TEMPLATE-concept.md     # copy to start a concept (CON-XXXX)
  _TEMPLATE-rule.md        # copy to start a binding rule (RUL-XXXX)
  _TEMPLATE-decision.md    # copy to start a decision (DEC-XXXX; status Rejected = rejected idea)
  CON-0001.md              # example concept
  RUL-0001.md              # example rule
  DEC-0001.md              # example decision: Accepted
  DEC-0002.md              # example rejected idea (decision, status Rejected)
  build_index.py           # regenerates INDEX.md from frontmatter (no deps)
.claude/
  settings.json            # registers the SessionStart hook
  hooks/session-start.sh   # injects Current Focus + branch at startup (silent)
  commands/reground.md     # /reground re-anchor command
```

- **New —** drop it into the empty repo; nothing to reconcile.
- **Existing —** drop it alongside your code. If you already have a `CLAUDE.md` or
  `.claude/settings.json`, **merge, don't overwrite**: fold your existing rules into
  the new `CLAUDE.md`, and add the `SessionStart` block into your existing settings'
  `hooks` object.

## 2. Make scripts executable and check the toolchain
```bash
chmod +x .claude/hooks/session-start.sh wiki/build_index.py
python3 --version && bash --version          # confirm both exist
python3 wiki/build_index.py                   # should regenerate wiki/INDEX.md
python3 wiki/build_index.py --test            # built-in regression tests → "self-test OK"
bash .claude/hooks/session-start.sh          # should print Current Focus + branch
```
Confirm both scripts run cleanly before relying on them.

## 3. Write ANCHOR.md — the north star and anti-scope
Highest-leverage fill; do it first.

- **New —** write the one-sentence purpose and the "This is NOT" list straight from
  your intent.
- **Existing —** reverse-engineer it from the code that exists. Ask Claude to read the
  repo and propose a north star + anti-scope, then correct it against what you
  *actually* intend. The mismatches you find are signal — they mark where the project
  already drifted.

## 4. Capture Locked Decisions and seed the first DEC files
- **New —** add the few commitments already made (language, a core constraint); the
  log grows as you decide things.
- **Existing —** harvest the *implicit* knowledge already baked into the code — the
  stack, the patterns, the boundaries — plus any approaches you remember rejecting.
  Backfill a `wiki/DEC-XXXX.md` for each load-bearing one (copy
  `_TEMPLATE-decision.md`); give recurring concepts a `CON-XXXX.md` and standing
  constraints a `RUL-XXXX.md`. Then run
  `python3 wiki/build_index.py`.

Either way: don't document everything. Capture only what's expensive to reverse or
what an agent might unknowingly violate. Promote the truly non-negotiable ones into
ANCHOR.md's Locked Decisions list.

## 5. Fill SPEC.md
- **New —** write functional requirements, NFR targets, and non-goals from your plan.
- **Existing —** derive the functional requirements from what the system already does
  (Claude can draft these from the code). The genuinely valuable parts to write
  yourself are the **Non-Goals** and the **NFR targets** — those are almost never
  written down and are exactly what drift erodes.

## 6. Fill ARCHITECTURE.md
- **New —** sketch the intended components and data flow; expect it to evolve.
- **Existing —** document the *actual* current architecture. Have Claude draft the
  components, contracts, and the Mermaid data-flow from the codebase, then fix it —
  it will get boundaries subtly wrong in ways you'll catch on review.

## 7. Fill GLOSSARY.md
Add the handful of terms that are overloaded or ambiguous. For an existing project,
prioritize terms already used inconsistently across code and comments (the
"chunk vs segment vs passage" problem).

## 8. Initialize STATE.md's Current Focus
- **New —** the first task you're about to start.
- **Existing —** whatever you're working on right now.

## 9. Verify the automation end to end
Start a fresh Claude Code session and confirm:
- the SessionStart hook injected the Current Focus and branch. **The injection is
  silent since Claude Code 2.1.0** — you won't see it in the transcript. Verify by
  asking Claude "what is the current focus and branch?" *without* letting it read
  any files; it should answer from the injected context. (`claude --debug` also
  shows hook execution if you need to troubleshoot.)
- `@ANCHOR.md` resolves — ask Claude to recite the north star without pasting it;
- `/reground` reads ANCHOR → STATE → wiki and restates the constraints.

## 10. Commit the scaffold
Commit as a discrete commit so the system itself is versioned and recoverable.
`.claude/settings.json` is safe to commit — just keep secrets out of any hook script.

## 11. Run one full loop to build muscle memory
Start a session → `/reground` → make one trivial decision → create its wiki/DEC
file and rebuild the index → end session → update STATE.md. Once that cycle feels automatic,
the system is live.

---

## Two notes that save grief
- **Priority order over completeness.** ANCHOR (3) and STATE (8) fight drift; the rest
  is backfill. A half-filled system that's anchored beats a fully-filled one that isn't.
- **For existing projects: let Claude draft from the code, but always verify.**
  Reverse-engineered docs that quietly contradict reality are worse than no docs — the
  agent will trust them and drift *toward* the error.
