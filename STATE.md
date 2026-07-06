# STATE.md — Current State & Handoff

> Read at the **start** of every session; rewrite at the **end** of every session.
> This is your fast recovery point after `/clear` or compaction. Keep it current
> over comprehensive — stale state is worse than none.

**Last updated:** 2026-07-06
**Active branch / worktree:** fix/pp-doclayout

---

## Current Focus

> The ONE thing in flight right now. One or two sentences. This is the line the
> compaction directives are told to preserve verbatim.

Decide which layout model to standardize on. All three providers are now
verified working end-to-end on this machine (PP-DocLayoutV3 in-process,
PaddleOCR service after the oneDNN fix, Surya v2 service after the float16 fix)
— run the page comparisons and record the decision as a `wiki/DEC-XXXX.md`.

## Done (recent, relevant)

- [x] Deep-dive review of `src/` against `docs/document_intelligence_pipeline_spec_v_0_5_1_beta_complete.md`:
  §5/§6/§8/§9/§10/§11/§14.3/§17 contracts conform (often richer than spec).
  Gaps: Phase D absent (known), §15 telemetry absent (Phase E), §16 MIME
  sniffing only for PDF/HTML magic, `scripts/ingest.py` absent, no
  cross-provider OCR fallback escalation in the Ollama providers.
- [x] **Fixed P0**: `extract.py` `_run_layout_enabled_pdf_graph` referenced an
  undeclared `layout_provider_name` (introduced in 424463f) → NameError on every
  layout-enabled PDF page; structured extraction produced 0 blocks. Parameter now
  threaded from `extract_structured_document`. Suite: 5 failed → 3 failed.
- [x] **Fixed the PaddleOCR blocker** (DEC-0015): `enable_mkldnn=False` on CPU
  restores `/detect` (Paddle 3.3 PIR/oneDNN bug), plus bbox parsing of PaddleX
  `DetResult.coordinate` (boxes were silently dropped → 0 regions). Verified:
  51 regions on mpphys page 6 @192 DPI, distribution ≈ PP-DocLayout.
- [x] **Diagnosed + fixed Surya cold start** (RUL-0006): vLLM backend defaults to
  bfloat16, rejected by RTX 2080 Ti (CC 7.5); `--rm` container died instantly and
  the spawner polled a ghost for 600 s. With `VLLM_DTYPE=float16`: cold 417 s,
  warm 3.3 s, 26 regions on page 6.
- [x] **Fixed DEC-0004 violation in Surya offload**: `manager.stop()` nulls
  `backend.handle` before the service read it, so the vLLM container (10.8 GB
  VRAM) survived `/offload`. Handle is now snapshotted first; the unit-test fake
  now mirrors Surya's real `stop()` so this regression is actually covered.
- [x] 3-provider runtime verification on mpphys page 6 @192 DPI:
  PP-DocLayoutV3 55 regions (29 formula, conf 0.81, ~7 s incl. load);
  PaddleOCR 51 regions (25 formula, conf 0.86); Surya 26 regions (4 formula).
- [x] Wiki records RUL-0006 + DEC-0015 created; index regenerated (24 records).
- [x] Previous session: RUL-0002/DEC-0014 fixes in `compat/` (sys.exit → raise).

## In Progress

- [ ] Layout provider comparison across real pages — all 3 providers now usable;
  only page 6 compared so far.

## Next

- [ ] **Commit the pending work** (everything below is verified but uncommitted):
  suggest two commits — (1) `compat/` RUL-0002 sys.exit fixes, (2) this session's
  layout/extract fixes + hardened test + wiki records + STATE.md.
- [ ] **Decide the default layout model** (currently `pp-doclayout-v3`, DEC-0006)
  from multi-page comparisons; record as `wiki/DEC-XXXX.md`. Key signal so far:
  Paddle-family sees 25–29 formula regions/page, Surya sees 4.
- [ ] **Decide formula-fallback semantics (spec §8.5 / RUL-0003)**: the
  ocr/formula split left `_formula_page_blocks` re-raising on extractor failure
  (`extract.py` ~line 617 — makes the fallback contract dead code), takes no OCR
  provider, and emits `FormulaFallback` with empty `raw_formula_text`;
  `formula.fallback_to_ocr` config is not honored there. Decide intended
  behavior, then repair the 3 remaining stale Phase C tests
  (they still call `_ocr_page_blocks` with a formula extractor positionally).
- [ ] Isolate tests from the real `data/pipeline.db`: `storage/checkpoint.py`
  binds `DB_PATH` at import; Phase C tests patch `extract.get_settings` only, so
  the suite writes `document='doc'` rows into the production state store
  (cleaned manually this session). Add a conftest fixture or settings injection.
- [ ] Add `--providers` to `compare-layout-page` (default set omits Surya, so the
  decision bundle can't include it) or include Surya when its service is up.
- [ ] Fix the Surya cold-start timeout mismatch: client `_LAYOUT_SERVICE_TIMEOUT_SECONDS`
  is 60 s vs ~417 s observed cold start — raise it, or pre-warm on service start.
- [ ] PP-DocLayout formula false positives (29/page on page 6 even at
  `formula_threshold=0.6`) — threshold tuning vs model behavior → may become a DEC.

## Open Questions

- [ ] Which layout model becomes the standard? (Formula-region counts differ 6–7×
  between Paddle-family and Surya; which is closer to ground truth?)
- [ ] Was dropping the OCR fallback from the formula path intentional, or
  refactor fallout? (Blocks the 3 remaining test failures.)

## Blockers

- None. (PaddleOCR `/detect` and Surya vLLM spawn both fixed this session;
  Surya still requires Docker + ~7 min cold start per RUL-0006.)

## Files touched this session

- `src/pocket_specialist/phases/extract.py` — P0 NameError fix
  (`layout_provider_name` threaded into `_run_layout_enabled_pdf_graph`).
- `src/pocket_specialist/layout/paddle_service.py` — `enable_mkldnn=False` on
  CPU; `coordinate` bbox parsing in `_bbox_from_payload`.
- `src/pocket_specialist/layout/service.py` — snapshot backend handle before
  `manager.stop()` in `_stop_loaded_backend`.
- `tests/test_surya_layout_service.py` — offload fake now nulls the handle in
  `stop()` (true regression test for the offload fix).
- `wiki/RUL-0006.md`, `wiki/DEC-0015.md`, `wiki/INDEX.md` — new records + regen.
- `STATE.md` — updated.
- Carried over, still uncommitted: `compat/enrichment.py`, `compat/render.py`,
  `compat/export.py` (RUL-0002/DEC-0014 sys.exit fixes).

## Run / test commands

```bash
# Full suite (3 known stale failures in test_phase_c_formula.py, see Next)
PYTHONPATH=src .venv/bin/python -m pytest -q
PYTHONPATH=src .venv/bin/python -m pytest tests/test_surya_layout_service.py -q

# PP-DocLayout (in-process, no service needed)
PYTHONPATH=src .venv/bin/python -m pocket_specialist.cli layout-pp-doclayout-v3 <pdf>

# PaddleOCR layout service (port 8003)
PYTHONPATH=src services/paddleocr_layout_service/.venv/bin/python \
  -m pocket_specialist.layout.paddle_service --host 127.0.0.1 --port 8003

# Surya v2 layout service (port 8004; Docker + vLLM; float16 REQUIRED, RUL-0006)
VLLM_DTYPE=float16 PYTHONPATH=src services/surya_layout_service/.venv/bin/python \
  -m pocket_specialist.layout.service --host 127.0.0.1 --port 8004

# Side-by-side comparison for a single page (Paddle service must be up;
# point base_url at the right port)
PIPELINE_LAYOUT_BASE_URL=http://127.0.0.1:8003 \
  PYTHONPATH=src .venv/bin/python -m pocket_specialist.cli \
  compare-layout-page <pdf> --page 6 --dpi 192 --output-dir /tmp/layout_compare
```
