"""TextbookOCR pipeline CLI — entry point for all stages."""
from __future__ import annotations
import sys
from pathlib import Path

import typer

app = typer.Typer(
    help="TextbookOCR: GPU-accelerated PDF → structured Markdown pipeline.",
    no_args_is_help=True,
)


# ── Stage 0: Rename corpus ────────────────────────────────────────────────────

@app.command(name="rename-corpus")
def rename_corpus_cmd(
    corpus_dir: Path = typer.Argument(None, help="Directory containing corpus PDFs (default: RAG-corpus)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print proposed renames without touching files."),
    manifest: Path = typer.Option(None, "--manifest", help="Override manifest output path."),
) -> None:
    """Stage 0: Standardize corpus filenames and write a metadata manifest."""
    from config import CHECKPOINT_DIR, CORPUS_DIR
    from pipeline.rename import rename_corpus

    target = (corpus_dir or CORPUS_DIR).resolve()
    manifest_path = manifest or CHECKPOINT_DIR / "rename_manifest.json"
    records = rename_corpus(target, manifest_path=manifest_path, dry_run=dry_run)

    renamed   = sum(1 for r in records if r['status'] == 'renamed')
    unchanged = sum(1 for r in records if r['status'] == 'unchanged')
    dry       = sum(1 for r in records if r['status'] == 'dry_run')

    typer.echo(f"\nTotal: {len(records)} files — "
               f"{renamed} renamed, {unchanged} unchanged"
               + (f", {dry} dry-run (not applied)" if dry else ""))
    if not dry_run:
        typer.echo(f"Manifest: {manifest_path}")


# ── Stage 1 ───────────────────────────────────────────────────────────────────

@app.command()
def render(
    pdf: Path = typer.Argument(..., help="Source PDF file."),
    zoom: float = typer.Option(2.0, help="Render scale factor (2.0 ≈ 150 DPI)."),
    start_page: int = typer.Option(1, "--start-page", help="First page (1-indexed)."),
    end_page: int = typer.Option(None, "--end-page", help="Last page inclusive. Default: last page."),
    output_dir: Path = typer.Option(None, "--output-dir", help="Override default PNG output directory."),
) -> None:
    """Stage 1: Render PDF pages to PNG images."""
    from config import RENDER_DIR
    from pipeline.render import render_pdf

    out = output_dir or RENDER_DIR
    render_pdf(pdf.resolve(), output_dir=out, zoom=zoom,
               start_page=start_page, end_page=end_page)


# ── Stage 2 ───────────────────────────────────────────────────────────────────

@app.command()
def ocr(
    start_page: int = typer.Option(None, "--start-page", help="First page to process (default: all)."),
    end_page: int = typer.Option(None, "--end-page", help="Last page inclusive (default: all)."),
    render_dir: Path = typer.Option(None, "--render-dir", help="Override rendered PNG directory."),
    output_dir: Path = typer.Option(None, "--output-dir", help="Override OCR JSON output directory."),
) -> None:
    """Stage 2: Run Surya OCR on rendered page images, write per-page JSON."""
    from config import RENDER_DIR, OCR_DIR
    from pipeline.ocr import ocr_pages

    done, failed = ocr_pages(
        render_dir=render_dir or RENDER_DIR,
        ocr_dir=output_dir or OCR_DIR,
        start_page=start_page,
        end_page=end_page,
    )
    if failed:
        raise typer.Exit(1)


# ── Stage 3 ───────────────────────────────────────────────────────────────────

@app.command()
def equations(
    start_page: int = typer.Option(None, "--start-page"),
    end_page: int = typer.Option(None, "--end-page"),
    eq_threshold: float = typer.Option(0.5, "--eq-threshold"),
    render_dir: Path = typer.Option(None, "--render-dir"),
    output_dir: Path = typer.Option(None, "--output-dir", help="Override equations JSON output directory."),
) -> None:
    """Stage 3: Surya layout detection + Surya LaTeX OCR equation extraction."""
    from config import RENDER_DIR, OCR_DIR, EQUATIONS_DIR, CROPS_DIR
    from pipeline.equations import process_equations

    done, failed = process_equations(
        render_dir=render_dir or RENDER_DIR,
        ocr_dir=OCR_DIR,
        equations_dir=output_dir or EQUATIONS_DIR,
        crops_dir=CROPS_DIR,
        start_page=start_page,
        end_page=end_page,
        eq_threshold=eq_threshold,
    )
    if failed:
        raise typer.Exit(1)


# ── Stage 4 ───────────────────────────────────────────────────────────────────

@app.command()
def correct(
    start_page: int = typer.Option(None, "--start-page"),
    end_page: int = typer.Option(None, "--end-page"),
    ollama_model: str = typer.Option("qwen2.5vl:7b", "--ollama-model", help="Ollama model name."),
    parallel_pages: int = typer.Option(2, "--parallel-pages", min=1, help="Max Stage 4 pages to process concurrently."),
    crops_dir: Path = typer.Option(None, "--crops-dir", help="Override equation crop PNG directory for multimodal Stage 4."),
) -> None:
    """Stage 4: Ollama LLM Markdown correction pass."""
    from config import CROPS_DIR, EQUATIONS_DIR, CORRECTION_DIR
    from pipeline.correction import correct_pages

    done, failed = correct_pages(
        equations_dir=EQUATIONS_DIR,
        correction_dir=CORRECTION_DIR,
        model=ollama_model,
        start_page=start_page,
        end_page=end_page,
        max_parallel=parallel_pages,
        crops_dir=crops_dir or CROPS_DIR,
    )
    if failed:
        raise typer.Exit(1)


# ── Stage 5 ───────────────────────────────────────────────────────────────────

@app.command()
def assemble(
    pdf: Path = typer.Argument(..., help="Source PDF file (used to name outputs)."),
    output_dir: Path = typer.Option(None, "--output-dir", help="Output directory (default: output/)."),
) -> None:
    """Stage 5: Concatenate corrected pages into final .md and .json."""
    from config import CORRECTION_DIR, EQUATIONS_DIR, OUTPUT_DIR
    from pipeline.assemble import assemble as _assemble

    _assemble(
        corrected_dir=CORRECTION_DIR,
        output_dir=output_dir or OUTPUT_DIR,
        source_pdf=pdf.resolve(),
        equations_dir=EQUATIONS_DIR,
    )


# ── Full pipeline ─────────────────────────────────────────────────────────────

def _run_pipeline(
    pdf_path: Path,
    start_page: int | None,
    end_page: int | None,
    zoom: float,
    ollama_model: str = "qwen2.5vl:7b",
    no_llm: bool = False,
    output_dir: Path | None = None,
    parallel_pages: int = 2,
) -> None:
    """Run stages 1–5 for a single PDF."""
    import time
    from config import RENDER_DIR, OCR_DIR, EQUATIONS_DIR, CROPS_DIR, CORRECTION_DIR, OUTPUT_DIR
    from pipeline.render import render_pdf
    from pipeline.ocr import ocr_pages
    from pipeline.equations import process_equations
    from pipeline.correction import correct_pages
    from pipeline.assemble import assemble

    t0 = time.monotonic()

    typer.echo(f"\n  Stage 1: Render  ({pdf_path.name})")
    render_pdf(pdf_path, start_page=start_page or 1, end_page=end_page, zoom=zoom)

    typer.echo(f"\n  Stage 2: OCR  ({pdf_path.name})")
    ocr_pages(render_dir=RENDER_DIR, ocr_dir=OCR_DIR,
              start_page=start_page, end_page=end_page)

    typer.echo(f"\n  Stage 3: Equations  ({pdf_path.name})")
    process_equations(render_dir=RENDER_DIR, ocr_dir=OCR_DIR,
                      equations_dir=EQUATIONS_DIR, crops_dir=CROPS_DIR,
                      start_page=start_page, end_page=end_page)

    if not no_llm:
        typer.echo(f"\n  Stage 4: Correction  ({pdf_path.name})")
        correct_pages(
            equations_dir=EQUATIONS_DIR,
            correction_dir=CORRECTION_DIR,
            model=ollama_model,
            start_page=start_page,
            end_page=end_page,
            max_parallel=parallel_pages,
            crops_dir=CROPS_DIR,
        )

    typer.echo(f"\n  Stage 5: Assemble  ({pdf_path.name})")
    assemble(
        corrected_dir=CORRECTION_DIR,
        output_dir=output_dir or OUTPUT_DIR,
        source_pdf=pdf_path,
        equations_dir=EQUATIONS_DIR,
    )

    elapsed = time.monotonic() - t0
    h, rem = divmod(int(elapsed), 3600)
    m, s = divmod(rem, 60)
    duration = f"{h}h {m}m {s}s" if h else f"{m}m {s}s" if m else f"{s}s"
    typer.echo(f"\nTotal time: {duration}  ({pdf_path.name})")


@app.command()
def run(
    pdf: Path = typer.Argument(..., help="Source PDF file to process."),
    start_page: int = typer.Option(None, "--start-page", help="First page (default: 1)."),
    end_page: int = typer.Option(None, "--end-page", help="Last page inclusive (default: last)."),
    zoom: float = typer.Option(2.0, "--zoom"),
    no_llm: bool = typer.Option(False, "--no-llm", help="Skip Stage 4 LLM correction."),
    ollama_model: str = typer.Option("qwen2.5vl:7b", "--ollama-model", help="Ollama model for Stage 4."),
    output_dir: Path = typer.Option(None, "--output-dir", help="Output directory (default: output/)."),
    parallel_pages: int = typer.Option(2, "--parallel-pages", min=1, help="Max Stage 4 pages to process concurrently."),
) -> None:
    """Run all pipeline stages (1–5) on a single PDF."""
    from config import CHECKPOINT_DIR
    from pipeline.rename import rename_corpus

    pdf_path = pdf.resolve()
    typer.echo("=== Stage 0: Rename corpus ===")
    records = rename_corpus(pdf_path.parent, manifest_path=CHECKPOINT_DIR / "rename_manifest.json")

    if not pdf_path.exists():
        remapped = next((r for r in records if r.get("original") == pdf_path.name), None)
        if remapped:
            candidate = pdf_path.parent / remapped["new_name"]
            if candidate.exists():
                pdf_path = candidate

    _run_pipeline(pdf_path, start_page, end_page, zoom,
                  ollama_model=ollama_model, no_llm=no_llm, output_dir=output_dir,
                  parallel_pages=parallel_pages)


@app.command(name="run-all")
def run_all(
    corpus_dir: Path = typer.Argument(None, help="Corpus directory (default: RAG-corpus)."),
    start_page: int = typer.Option(None, "--start-page", help="First page per PDF (default: 1)."),
    end_page: int = typer.Option(None, "--end-page", help="Last page per PDF (default: last)."),
    zoom: float = typer.Option(2.0, "--zoom"),
    no_llm: bool = typer.Option(False, "--no-llm", help="Skip Stage 4 LLM correction."),
    ollama_model: str = typer.Option("qwen2.5vl:7b", "--ollama-model", help="Ollama model for Stage 4."),
    output_dir: Path = typer.Option(None, "--output-dir", help="Output directory (default: output/)."),
    parallel_pages: int = typer.Option(2, "--parallel-pages", min=1, help="Max Stage 4 pages to process concurrently."),
) -> None:
    """Run all pipeline stages (1–5) on every PDF in the corpus."""
    from config import CHECKPOINT_DIR, CORPUS_DIR
    from pipeline.rename import rename_corpus

    target = (corpus_dir or CORPUS_DIR).resolve()
    pdfs = sorted(target.glob("*.pdf"))
    if not pdfs:
        typer.echo(f"No PDFs found in {target}.", err=True)
        raise typer.Exit(1)

    typer.echo(f"=== Stage 0: Rename corpus ({len(pdfs)} files) ===")
    rename_corpus(target, manifest_path=CHECKPOINT_DIR / "rename_manifest.json")

    # Re-scan after renaming so we only process files that currently exist.
    pdfs = sorted(p for p in target.glob("*.pdf") if p.exists())

    for i, pdf_path in enumerate(pdfs, 1):
        typer.echo(f"\n{'─' * 60}")
        typer.echo(f"  [{i}/{len(pdfs)}] {pdf_path.name}")
        typer.echo(f"{'─' * 60}")
        _run_pipeline(pdf_path, start_page, end_page, zoom,
                      ollama_model=ollama_model, no_llm=no_llm, output_dir=output_dir,
                      parallel_pages=parallel_pages)

    typer.echo(f"\nDone. Processed {len(pdfs)} PDFs.")


# ── Status + Reset ────────────────────────────────────────────────────────────

@app.command()
def status() -> None:
    """Show per-stage progress from the checkpoint database."""
    from config import DB_PATH
    from pipeline.checkpoint import init_db, get_summary, get_failed_pages

    if not DB_PATH.exists():
        typer.echo("No checkpoint database found. Run a stage first.")
        raise typer.Exit()

    from pipeline.checkpoint import STAGES
    init_db()

    width = max(len(s) for s in STAGES)
    for stage in STAGES:
        summary      = get_summary(stage)
        done         = summary.get("done", 0)
        failed       = summary.get("failed", 0)
        failed_pages = get_failed_pages(stage)
        fail_detail  = ""
        if failed_pages:
            shown = failed_pages[:5]
            more  = len(failed_pages) - len(shown)
            fail_detail = "  failed pages: " + ", ".join(map(str, shown))
            if more:
                fail_detail += f" (+{more} more)"
        label = stage.capitalize().ljust(width)
        typer.echo(f"{label}: {done:>4} done  {failed:>3} failed{fail_detail}")


@app.command()
def reset(
    stage: str = typer.Option(
        None, "--stage",
        help="Stage to reset: render, ocr, equations, correction. Omit to reset all.",
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt."),
) -> None:
    """Reset stage outputs and checkpoint records for the selected stage and downstream stages."""
    from config import CORRECTION_DIR, CROPS_DIR, EQUATIONS_DIR, OCR_DIR, RENDER_DIR
    from pipeline.checkpoint import init_db, reset_stage, STAGES

    stage_order = list(STAGES)
    stage_dirs = {
        "render": [RENDER_DIR],
        "ocr": [OCR_DIR],
        "equations": [EQUATIONS_DIR, CROPS_DIR],
        "correction": [CORRECTION_DIR],
    }

    def _clear_dir(path: Path) -> int:
        if not path.exists():
            return 0
        removed = 0
        for child in path.iterdir():
            if child.is_file() or child.is_symlink():
                child.unlink()
                removed += 1
        return removed

    valid = set(stage_order)
    if stage and stage not in valid:
        typer.echo(f"Unknown stage '{stage}'. Choose from: {', '.join(sorted(valid))}.", err=True)
        raise typer.Exit(1)

    targets = stage_order if stage is None else stage_order[stage_order.index(stage):]

    if not yes:
        typer.confirm(
            f"Reset outputs and checkpoints for: {', '.join(targets)}? This cannot be undone.",
            abort=True,
        )

    init_db()
    for s in targets:
        removed = 0
        for out_dir in stage_dirs.get(s, []):
            removed += _clear_dir(out_dir)
        reset_stage(s)
        typer.echo(f"  Reset: {s}  ({removed} files removed)")


if __name__ == "__main__":
    app()
