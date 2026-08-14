# STATE.md — Current State & Handoff

> Read at the **start** of every session; rewrite at the **end** of every session.
> This is your fast recovery point after `/clear` or compaction. Keep it current
> over comprehensive — stale state is worse than none.

**Last updated:** 2026-08-14
**Active branch / worktree:** fix/pp-doclayout

---

## Current Focus

> The ONE thing in flight right now. One or two sentences. This is the line the
> compaction directives are told to preserve verbatim.

Build the **controlled-mode** layout comparison (DEC-0017): a single render
process with per-provider `RenderSpec` (DEC-0016), one shared label map, and one
threshold applied in one place — then make the layout standardization decision
from that. The previously recorded page-6 numbers are invalid evidence (RUL-0007).

## Done (recent, relevant)

**This session (analysis + decisions only — no code changed):**

- [x] **Full code review of the path up to layout detection** → `docs/layout-path-review.md`
  (implementation-vs-expected mermaid diagrams + a 15-row divergence table C1–C15).
- [x] **Established the page-6 comparison is invalid.** `pipeline.toml` sets
  `threshold=0.5 / formula_threshold=0.6 / img_size=800`, which reach
  PP-DocLayoutV3 and PaddleOCR. Surya sends `{"provider": name}` on `/load` and
  applies **no threshold at all** — its 26 regions are unfiltered against two
  filtered counts. Plus three divergent label maps and a `confidence or 1.0`
  default. → RUL-0007, DEC-0017.
- [x] **Verified PP-DocLayoutV3 resizes every input to 800×800**, bicubic,
  non-aspect-preserving. Ran the processor at 144/192/300/400 DPI → all produce
  `(1,3,800,800)`. Render DPI above ~2× the target long edge is waste for
  PP/Paddle; only Surya (dynamic-resolution VLM) can use more pixels. → CON-0004.
- [x] **Measured the image plumbing** (page 6, 192 DPI): PNG encode 42.1 ms vs
  4.9 ms to rasterize (8.6×); bitmap→PIL 59.8 ms via PNG round-trip vs 1.1 ms via
  `pix.samples` (57×); 55 region crops 991 ms vs 18 ms (56×). A 55-region page
  spends ~1.05 s on plumbing for ~20 ms of real work. → DEC-0016.
- [x] **Two real bugs found** (not yet fixed): provider offload sits outside any
  `try/finally` at both graph sites (`extract.py:1496-1510`, `1591-1593`) —
  DEC-0004 violation, leaks the model on exception. And the client label map is
  **dead code** for service providers (`providers.py:450`, `or` short-circuits).
- [x] **Web scan of the 2026 model landscape.** PP-DocLayoutV3 == RT-DocLayout
  (arXiv 2606.23344, ECCV 2026, 33M params, 92.46%, 132.1 FPS) — i.e. the current
  default *is* current SOTA, and is the layout stage inside both PaddleOCR-VL-1.6
  and GLM-OCR. **DEC-0006 is re-validated, not weakened.** New candidate worth a
  comparison row: Surya's 20M rf-detr/ONNX **CPU** fast-layout model (v0.21.0,
  Jul 2026) — sidesteps RUL-0006's float16 issue, the 417 s cold start, and the
  10.8 GB VRAM. OCR note: OmniDocBench v1.6 leader PaddleOCR-VL-1.6 requires
  CC ≥ 8.0 and is **not deployable on this 2080 Ti**; GLM-OCR (MIT, 0.9B, 95.22)
  is the best fit and already a configured provider name.
- [x] Wiki records created: **CON-0004, DEC-0016, DEC-0017, RUL-0007**; index
  regenerated (28 records: 4 concepts, 7 rules, 12 decisions, 5 rejected).

**Previous sessions (committed in `443f993`, `83273d3`):**

- [x] Fixed P0 `NameError` on `layout_provider_name` in `_run_layout_enabled_pdf_graph`.
- [x] Fixed the PaddleOCR blocker (DEC-0015): `enable_mkldnn=False` + `coordinate` bbox parsing.
- [x] Diagnosed + fixed Surya cold start (RUL-0006): `VLLM_DTYPE=float16`.
- [x] Fixed DEC-0004 violation in Surya offload (handle snapshotted before `manager.stop()`).
- [x] RUL-0002/DEC-0014 fixes in `compat/` (sys.exit → raise).

## In Progress

- [ ] Nothing mid-edit. Next action is the DEC-0016 implementation (below).

## Next

1. [ ] **Controlled-mode prerequisites** (blocking the layout decision, in order):
   - [ ] **Per-provider `base_url`.** `LayoutSettings.base_url` is one scalar
     (`config.py:172`) read by *both* the Surya (`providers.py:307`) and PaddleOCR
     (`:368`) providers. Surya is on :8004, Paddle on :8003 → **a single process
     cannot reach both**, so `compare_layout_page` structurally cannot compare them.
   - [ ] **One shared label map.** Delete the two service-side maps
     (`service.py:30`, `paddle_service.py:36`); services emit raw `provider_label`;
     normalize once client-side. Fixes the dead-code path at `providers.py:450`.
   - [ ] **One threshold, one place.** Prefer capturing regions *unfiltered* and
     sweeping the threshold at analysis time → precision/recall curves instead of
     one arbitrary operating point.
   - [ ] **`render/raster.py`** — `RenderSpec` / `PageRaster` / `PageRenderer`
     (DEC-0016). Controlled mode pins one spec across all providers.
2. [ ] **Then** run the multi-page comparison and record the layout DEC.
3. [ ] Fix the DEC-0004 offload leak (`try/finally` at both graph sites).
4. [ ] Add Surya CPU fast-layout (v0.21.0) as a fourth comparison row.
5. [ ] Raise `_LAYOUT_SERVICE_TIMEOUT_SECONDS` (60 s vs 417 s cold start) or split
   `/load` (long) from `/detect` (short) — `providers.py:84`.
6. [ ] **Decide formula-fallback semantics (spec §8.5 / RUL-0003)**: `_formula_page_blocks`
   re-raises on extractor failure (`extract.py` ~617), takes no OCR provider, and
   emits `FormulaFallback` with empty `raw_formula_text`; `formula.fallback_to_ocr`
   is not honored there. Blocks the 3 stale Phase C tests.
7. [ ] Isolate tests from the real `data/pipeline.db` (`storage/checkpoint.py` binds
   `DB_PATH` at import; Phase C tests patch only `extract.get_settings`).
8. [ ] Validate `region_type` against the SPEC §5.4 closed set — unmapped labels
   currently pass through verbatim.

## Open Questions

- [ ] Which layout model becomes the standard? **Cannot be answered until
  controlled mode exists** (RUL-0007). Prior signal is void.
- [ ] PP-DocLayoutV3's `id2label` has duplicate names — `formula` at both id 5 and
  id 15, `footer` at 8/9, `header` at 12/13, `text` at 22/23 — which
  `normalize_layout_label` collapses by name. Are two distinct formula subtypes
  being flattened? Relevant to the 29-formula-regions-per-page question.
- [ ] Was dropping the OCR fallback from the formula path intentional, or refactor
  fallout? (Blocks the 3 remaining test failures.)
- [ ] Should DEC-0016 be promoted to an ANCHOR.md Locked Decision? Deliberately
  **not** promoted yet — it is accepted but unimplemented.
- [ ] Is the CC 7.5 / no-bfloat16 constraint worth generalizing from RUL-0006 into
  its own rule? It has now bitten twice (Surya vLLM, PaddleOCR-VL) and is the
  strongest filter on provider selection. Proposed, not created.

## Blockers

- None technical. All three providers run end-to-end on this machine. The layout
  *decision* is blocked on controlled mode, which is work, not a blocker.

## Files touched this session

- `docs/layout-path-review.md` — **new**, full implementation-vs-expected review.
- `wiki/CON-0004.md`, `wiki/DEC-0016.md`, `wiki/DEC-0017.md`, `wiki/RUL-0007.md` — new.
- `wiki/INDEX.md` — regenerated (28 records).
- `STATE.md` — this file.
- **No source files were modified this session.**

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

# Side-by-side comparison. NOTE: one base_url is shared by both service
# providers, so today only ONE service can be reached per run — see Next #2.
PIPELINE_LAYOUT_BASE_URL=http://127.0.0.1:8003 \
  PYTHONPATH=src .venv/bin/python -m pocket_specialist.cli \
  compare-layout-page <pdf> --page 6 --dpi 192 --output-dir /tmp/layout_compare

# Verify what actually reaches PP-DocLayoutV3 (any DPI -> 800x800)
PYTHONPATH=src .venv/bin/python -c "
from transformers import AutoImageProcessor
p = AutoImageProcessor.from_pretrained('PaddlePaddle/PP-DocLayoutV3_safetensors')
print(p.size, p.do_resize)"
```
