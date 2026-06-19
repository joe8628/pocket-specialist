# STATE.md — Current State & Handoff

> Read at the **start** of every session; rewrite at the **end** of every session.
> This is your fast recovery point after `/clear` or compaction. Keep it current
> over comprehensive — stale state is worse than none.

**Last updated:** 2026-06-19 01:38
**Active branch / worktree:** fix/pp-doclayout

---

## Current Focus

> The ONE thing in flight right now. One or two sentences. This is the line the
> compaction directives are told to preserve verbatim.

Finish testing the layout module and decide which model to standardize on for
layout detection. Three providers work to differing degrees — PP-DocLayoutV3
(in-process default), Surya v2 (isolated service), PaddleOCR (isolated service) —
and choosing the default + closing the test gaps is the open item on `fix/pp-doclayout`.

## Done (recent, relevant)

- [x] PP-DocLayoutV3 in-process provider working via Transformers; preserves native
  reading order (`order_seq`) and polygons. Page-6 @192 DPI: 59 regions, 59 with
  polygons, 44 native blocks matched (DEC-0006, DEC-0013).
- [x] Surya v2 isolated service working when warm (page-6: 26 regions, 4.37s);
  `offload()` now stops the `surya-vllm-<port>` container (DEC-0004/0008).
- [x] `compare-layout-page` CLI + `layout/compare.py` produce side-by-side artifact
  bundles (base render, full overlay, formula-only overlay, crops, per-provider JSON).
- [x] `tests/test_surya_layout_service.py` (5 passed) covers Surya HTTP normalization
  + offload/container-stop.
- [x] Governance docs initialized this session: ANCHOR / SPEC / ARCHITECTURE /
  GLOSSARY + 22 wiki records (DEC-0003..0014, RUL-0002..0005, CON-0002..0003).

## In Progress

- [ ] Layout provider comparison across real pages — PP-DocLayout artifacts clean;
  Surya available when warm; PaddleOCR side still blocked (see Blockers).
- [ ] Broaden layout test coverage beyond Surya HTTP — PP-DocLayout adapter and the
  comparison path are under-tested.

## Next

- [ ] **Decide the default layout model** (currently `pp-doclayout-v3`, DEC-0006)
  from the page comparisons; record the decision as a `wiki/DEC-XXXX.md`.
- [ ] Fix the Surya cold-start timeout mismatch: raise the client detect timeout
  (`_LAYOUT_SERVICE_TIMEOUT_SECONDS`, currently 60s) or pre-warm the service so the
  first page does not fail (~294s cold start observed).
- [ ] Resolve the PaddleOCR `/detect` runtime incompatibility, or drop PaddleOCR
  from the decision set.
- [ ] Address PP-DocLayout formula false positives (32–34/page across a 96–300 DPI
  sweep) — threshold tuning (`layout.formula_threshold`) vs accept as model behavior.

## Open Questions

- [ ] Which layout model becomes the standard? (SPEC Open Questions: "layout model
  replacement — abstraction exists; benchmarking needed".)
- [ ] Are the PP-DocLayout formula false positives a thresholding fix or inherent
  model behavior? → may become a DEC.

## Blockers

- **PaddleOCR `/detect` fails on this machine** — service loads and weights cache
  under `~/.paddlex/official_models/PP-DocLayout_plus-L`, but inference errors
  (Paddle runtime incompatibility, `ConvertPirAttribute2RuntimeAttribute`). Health
  + `/load` pass; `/detect` does not.
- **Surya v2 needs Docker + vLLM** — auto-selects a Dockerized vLLM backend on
  NVIDIA GPUs; cold start (~294s) exceeds the 60s client detect timeout, so the
  first page fails until the backend is warm. Requires Docker available on the host.

## Files touched this session

- `ANCHOR.md`, `SPEC.md`, `ARCHITECTURE.md`, `GLOSSARY.md`, `STATE.md` — filled from templates.
- `wiki/DEC-0003..0014.md`, `wiki/RUL-0002..0005.md`, `wiki/CON-0002..0003.md`,
  `wiki/INDEX.md` — created / regenerated (`python3 wiki/build_index.py`).
- No `src/` changes this session.

## Run / test commands

```bash
# Layout-focused tests
PYTHONPATH=src .venv/bin/python -m pytest tests/test_surya_layout_service.py -q
PYTHONPATH=src .venv/bin/python -m pytest          # full suite

# PP-DocLayout (in-process, no service needed)
PYTHONPATH=src .venv/bin/python -m pocket_specialist.cli layout-pp-doclayout-v3 <pdf>

# Surya v2 (start isolated service first; needs Docker + vLLM)
PYTHONPATH=src services/surya_layout_service/.venv/bin/python \
  -m pocket_specialist.layout.service --host 127.0.0.1 --port 8004
PIPELINE_LAYOUT_BASE_URL=http://127.0.0.1:8004 \
  PYTHONPATH=src .venv/bin/python -m pocket_specialist.cli layout-surya <pdf>

# Side-by-side comparison for a single page
PYTHONPATH=src .venv/bin/python -m pocket_specialist.cli \
  compare-layout-page <pdf> --page 6 --dpi 192 --output-dir /tmp/layout_compare
```
