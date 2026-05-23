"""Document intelligence CLI for the Phase A foundation and compatibility workflows."""
from __future__ import annotations

import sys
from pathlib import Path

import typer

app = typer.Typer(
    help="Pocket Specialist: local-first document intelligence pipeline.",
    no_args_is_help=True,
)


def _doc_slug(pdf_path: Path) -> str:
    from config import document_slug

    return document_slug(pdf_path.resolve())


def _known_documents() -> list[str]:
    from config import CHECKPOINT_DIR
    from pipeline.checkpoint import get_documents, init_db

    reserved_roots = {"rendered", "ocr", "equations", "crops", "corrected", "__pycache__"}
    docs = set()
    if CHECKPOINT_DIR.exists():
        for path in CHECKPOINT_DIR.iterdir():
            if not path.is_dir() or path.name in reserved_roots:
                continue
            if any(child.is_file() for child in path.rglob("*")):
                docs.add(path.name)
    init_db()
    docs.update(get_documents())
    docs.discard("__legacy__")
    return sorted(docs)


def _resolve_document(pdf: Path | None = None, doc: str | None = None) -> str:
    if pdf is not None:
        return _doc_slug(pdf)
    if doc:
        return doc
    raise typer.BadParameter("Specify either --pdf or --doc.")


def _resume_state(document: str) -> tuple[str | None, int | None]:
    from pipeline.checkpoint import STAGES, get_done_pages, init_db

    init_db()
    for stage in reversed(STAGES):
        pages = get_done_pages(stage, document)
        if pages:
            return stage, max(pages)
    return None, None


# ── Corpus Intake ──────────────────────────────────────────────────────────────

@app.command(name="rename-corpus")
def rename_corpus_cmd(
    corpus_dir: Path = typer.Argument(None, help="Directory containing corpus PDFs (default: RAG-corpus)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print proposed renames without touching files."),
    manifest: Path = typer.Option(None, "--manifest", help="Override manifest output path."),
) -> None:
    """Normalize corpus filenames and write a metadata manifest."""
    from config import CHECKPOINT_DIR, CORPUS_DIR
    from pipeline.rename import rename_corpus

    target = (corpus_dir or CORPUS_DIR).resolve()
    manifest_path = manifest or CHECKPOINT_DIR / "rename_manifest.json"
    records = rename_corpus(target, manifest_path=manifest_path, dry_run=dry_run)

    renamed = sum(1 for r in records if r["status"] == "renamed")
    unchanged = sum(1 for r in records if r["status"] == "unchanged")
    dry = sum(1 for r in records if r["status"] == "dry_run")

    typer.echo(
        f"\nTotal: {len(records)} files — {renamed} renamed, {unchanged} unchanged"
        + (f", {dry} dry-run (not applied)" if dry else "")
    )
    if not dry_run:
        typer.echo(f"Manifest: {manifest_path}")


# ── Rasterization ──────────────────────────────────────────────────────────────

@app.command()
def render(
    pdf: Path = typer.Argument(..., help="Source PDF file."),
    zoom: float = typer.Option(2.0, help="Render scale factor (2.0 ≈ 150 DPI)."),
    start_page: int = typer.Option(1, "--start-page", help="First page (1-indexed)."),
    end_page: int = typer.Option(None, "--end-page", help="Last page inclusive. Default: last page."),
    output_dir: Path = typer.Option(None, "--output-dir", help="Override document-scoped PNG output directory."),
) -> None:
    """Render PDF pages to document-scoped PNG images."""
    from config import render_dir_for
    from pipeline.render import render_pdf

    pdf_path = pdf.resolve()
    out = output_dir or render_dir_for(_doc_slug(pdf_path))
    render_pdf(pdf_path, output_dir=out, zoom=zoom, start_page=start_page, end_page=end_page)


# ── OCR Extraction ─────────────────────────────────────────────────────────────

@app.command()
def ocr(
    pdf: Path = typer.Argument(..., help="Source PDF file."),
    start_page: int = typer.Option(None, "--start-page", help="First page to process (default: all)."),
    end_page: int = typer.Option(None, "--end-page", help="Last page inclusive (default: all)."),
    render_dir: Path = typer.Option(None, "--render-dir", help="Override document-scoped rendered PNG directory."),
    output_dir: Path = typer.Option(None, "--output-dir", help="Override document-scoped OCR JSON output directory."),
) -> None:
    """Extract OCR blocks from rendered page images into structured JSON."""
    from config import ocr_dir_for, render_dir_for
    from pipeline.ocr import ocr_pages

    pdf_path = pdf.resolve()
    document = _doc_slug(pdf_path)
    done, failed = ocr_pages(
        document=document,
        render_dir=render_dir or render_dir_for(document),
        ocr_dir=output_dir or ocr_dir_for(document),
        start_page=start_page,
        end_page=end_page,
    )
    if failed:
        raise typer.Exit(1)


# ── Layout And Formula Enrichment ──────────────────────────────────────────────

@app.command()
def equations(
    pdf: Path = typer.Argument(..., help="Source PDF file."),
    start_page: int = typer.Option(None, "--start-page"),
    end_page: int = typer.Option(None, "--end-page"),
    eq_threshold: float = typer.Option(0.5, "--eq-threshold"),
    render_dir: Path = typer.Option(None, "--render-dir"),
    output_dir: Path = typer.Option(None, "--output-dir", help="Override document-scoped equations JSON output directory."),
    crops_dir: Path = typer.Option(None, "--crops-dir", help="Override document-scoped equation crop directory."),
) -> None:
    """Enrich OCR pages with layout classes, equation crops, and LaTeX extraction."""
    from config import crops_dir_for, equations_dir_for, ocr_dir_for, render_dir_for
    from pipeline.enrichment import enrich_document

    pdf_path = pdf.resolve()
    document = _doc_slug(pdf_path)
    done, failed = enrich_document(
        document=document,
        render_dir=render_dir or render_dir_for(document),
        ocr_dir=ocr_dir_for(document),
        equations_dir=output_dir or equations_dir_for(document),
        crops_dir=crops_dir or crops_dir_for(document),
        start_page=start_page,
        end_page=end_page,
        eq_threshold=eq_threshold,
    )
    if failed:
        raise typer.Exit(1)


# ── Structured Correction Export ───────────────────────────────────────────────

@app.command()
def correct(
    pdf: Path = typer.Argument(..., help="Source PDF file."),
    start_page: int = typer.Option(None, "--start-page"),
    end_page: int = typer.Option(None, "--end-page"),
    ollama_model: str = typer.Option("qwen2.5vl:3b", "--ollama-model", help="Ollama model name."),
    parallel_pages: int = typer.Option(1, "--parallel-pages", min=1, help="Max pages to process concurrently in correction/export."),
    equations_dir: Path = typer.Option(None, "--equations-dir", help="Override document-scoped equations JSON directory."),
    crops_dir: Path = typer.Option(None, "--crops-dir", help="Override document-scoped equation crop directory."),
    output_dir: Path = typer.Option(None, "--output-dir", help="Override document-scoped correction export directory."),
) -> None:
    """Run the optional LLM-backed correction/export pass."""
    from config import correction_dir_for, crops_dir_for, equations_dir_for
    from pipeline.export import correct_pages

    pdf_path = pdf.resolve()
    document = _doc_slug(pdf_path)
    done, failed = correct_pages(
        document=document,
        equations_dir=equations_dir or equations_dir_for(document),
        correction_dir=output_dir or correction_dir_for(document),
        model=ollama_model,
        start_page=start_page,
        end_page=end_page,
        max_parallel=parallel_pages,
        crops_dir=crops_dir or crops_dir_for(document),
    )
    if failed:
        raise typer.Exit(1)


# ── Assembly ───────────────────────────────────────────────────────────────────

@app.command()
def assemble(
    pdf: Path = typer.Argument(..., help="Source PDF file (used to name outputs)."),
    output_dir: Path = typer.Option(None, "--output-dir", help="Output directory (default: output/<doc>/)."),
) -> None:
    """Assemble per-page exports into final document outputs."""
    from config import correction_dir_for, equations_dir_for, output_dir_for
    from pipeline.serialization import assemble_document as _assemble

    pdf_path = pdf.resolve()
    document = _doc_slug(pdf_path)
    _assemble(
        corrected_dir=correction_dir_for(document),
        output_dir=output_dir or output_dir_for(document),
        source_pdf=pdf_path,
        equations_dir=equations_dir_for(document),
    )


# ── End-To-End Run ─────────────────────────────────────────────────────────────

def _run_pipeline(
    pdf_path: Path,
    start_page: int | None,
    end_page: int | None,
    zoom: float,
    ollama_model: str = "qwen2.5vl:3b",
    no_llm: bool = False,
    output_dir: Path | None = None,
    parallel_pages: int = 1,
) -> None:
    """Run the current compatibility pipeline for a single PDF."""
    import time

    from config import correction_dir_for, crops_dir_for, equations_dir_for, ocr_dir_for, output_dir_for, render_dir_for
    from pipeline.serialization import assemble_document
    from pipeline.export import correct_pages
    from pipeline.enrichment import enrich_document
    from pipeline.ocr import ocr_pages
    from pipeline.render import render_pdf

    document = _doc_slug(pdf_path)
    render_dir = render_dir_for(document)
    ocr_dir = ocr_dir_for(document)
    equations_dir = equations_dir_for(document)
    crops_dir = crops_dir_for(document)
    correction_dir = correction_dir_for(document)
    final_output_dir = output_dir or output_dir_for(document)

    stage, page = _resume_state(document)
    if stage and page:
        typer.echo(f"Resume state: {document} at step {stage} page {page}.")

    t0 = time.monotonic()

    typer.echo(f"\n  Render: rasterization  ({pdf_path.name})")
    render_pdf(pdf_path, output_dir=render_dir, start_page=start_page or 1, end_page=end_page, zoom=zoom)

    typer.echo(f"\n  OCR: structured extraction  ({pdf_path.name})")
    ocr_pages(document=document, render_dir=render_dir, ocr_dir=ocr_dir, start_page=start_page, end_page=end_page)

    typer.echo(f"\n  Enrichment: layout + formulas  ({pdf_path.name})")
    enrich_document(
        document=document,
        render_dir=render_dir,
        ocr_dir=ocr_dir,
        equations_dir=equations_dir,
        crops_dir=crops_dir,
        start_page=start_page,
        end_page=end_page,
    )

    if not no_llm:
        typer.echo(f"\n  Correction: optional export normalization  ({pdf_path.name})")
        correct_pages(
            document=document,
            equations_dir=equations_dir,
            correction_dir=correction_dir,
            model=ollama_model,
            start_page=start_page,
            end_page=end_page,
            max_parallel=parallel_pages,
            crops_dir=crops_dir,
        )

    typer.echo(f"\n  Assemble: final outputs  ({pdf_path.name})")
    assemble_document(
        corrected_dir=correction_dir,
        output_dir=final_output_dir,
        source_pdf=pdf_path,
        equations_dir=equations_dir,
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
    no_llm: bool = typer.Option(False, "--no-llm", help="Skip the optional LLM correction/export pass."),
    ollama_model: str = typer.Option("qwen2.5vl:3b", "--ollama-model", help="Ollama model for the correction/export pass."),
    output_dir: Path = typer.Option(None, "--output-dir", help="Output directory (default: output/<doc>/)."),
    parallel_pages: int = typer.Option(1, "--parallel-pages", min=1, help="Max pages to process concurrently in correction/export."),
) -> None:
    """Run the current end-to-end compatibility pipeline on a single PDF."""
    from config import CHECKPOINT_DIR
    from pipeline.rename import rename_corpus

    pdf_path = pdf.resolve()
    typer.echo("=== Corpus intake: normalize filenames ===")
    records = rename_corpus(pdf_path.parent, manifest_path=CHECKPOINT_DIR / "rename_manifest.json")

    if not pdf_path.exists():
        remapped = next((r for r in records if r.get("original") == pdf_path.name), None)
        if remapped:
            candidate = pdf_path.parent / remapped["new_name"]
            if candidate.exists():
                pdf_path = candidate

    _run_pipeline(
        pdf_path,
        start_page,
        end_page,
        zoom,
        ollama_model=ollama_model,
        no_llm=no_llm,
        output_dir=output_dir,
        parallel_pages=parallel_pages,
    )


@app.command(name="run-all")
def run_all(
    corpus_dir: Path = typer.Argument(None, help="Corpus directory (default: RAG-corpus)."),
    start_page: int = typer.Option(None, "--start-page", help="First page per PDF (default: 1)."),
    end_page: int = typer.Option(None, "--end-page", help="Last page per PDF (default: last)."),
    zoom: float = typer.Option(2.0, "--zoom"),
    no_llm: bool = typer.Option(False, "--no-llm", help="Skip the optional LLM correction/export pass."),
    ollama_model: str = typer.Option("qwen2.5vl:3b", "--ollama-model", help="Ollama model for the correction/export pass."),
    output_dir: Path = typer.Option(None, "--output-dir", help="Output directory root (default: output/<doc>/)."),
    parallel_pages: int = typer.Option(1, "--parallel-pages", min=1, help="Max pages to process concurrently in correction/export."),
) -> None:
    """Run the current end-to-end compatibility pipeline on every PDF in the corpus."""
    from config import CHECKPOINT_DIR, CORPUS_DIR
    from pipeline.rename import rename_corpus

    target = (corpus_dir or CORPUS_DIR).resolve()
    pdfs = sorted(target.glob("*.pdf"))
    if not pdfs:
        typer.echo(f"No PDFs found in {target}.", err=True)
        raise typer.Exit(1)

    typer.echo(f"=== Corpus intake: normalize filenames ({len(pdfs)} files) ===")
    rename_corpus(target, manifest_path=CHECKPOINT_DIR / "rename_manifest.json")
    pdfs = sorted(p for p in target.glob("*.pdf") if p.exists())

    for i, pdf_path in enumerate(pdfs, 1):
        typer.echo(f"\n{'─' * 60}")
        typer.echo(f"  [{i}/{len(pdfs)}] {pdf_path.name}")
        typer.echo(f"{'─' * 60}")
        doc_output_dir = output_dir / _doc_slug(pdf_path) if output_dir else None
        _run_pipeline(
            pdf_path,
            start_page,
            end_page,
            zoom,
            ollama_model=ollama_model,
            no_llm=no_llm,
            output_dir=doc_output_dir,
            parallel_pages=parallel_pages,
        )

    typer.echo(f"\nDone. Processed {len(pdfs)} PDFs.")


# ── Status + Reset ────────────────────────────────────────────────────────────

@app.command()
def status(
    pdf: Path = typer.Option(None, "--pdf", help="Show status for one source PDF."),
    doc: str = typer.Option(None, "--doc", help="Show status for one document slug."),
) -> None:
    """Show per-step progress from the checkpoint database."""
    from pipeline.checkpoint import STAGES, get_failed_pages, get_summary, init_db

    init_db()
    documents = [_resolve_document(pdf, doc)] if (pdf or doc) else _known_documents()
    if not documents:
        typer.echo("No checkpoint state found.")
        raise typer.Exit()

    width = max(len(s) for s in STAGES)
    for document in documents:
        typer.echo(f"Document: {document}")
        for stage in STAGES:
            summary = get_summary(stage, document)
            done = summary.get("done", 0)
            failed = summary.get("failed", 0)
            failed_pages = get_failed_pages(stage, document)
            fail_detail = ""
            if failed_pages:
                shown = failed_pages[:5]
                more = len(failed_pages) - len(shown)
                fail_detail = "  failed pages: " + ", ".join(map(str, shown))
                if more:
                    fail_detail += f" (+{more} more)"
            label = stage.capitalize().ljust(width)
            typer.echo(f"{label}: {done:>4} done  {failed:>3} failed{fail_detail}")
        resume_stage, resume_page = _resume_state(document)
        if resume_stage and resume_page:
            typer.echo(f"Resume point: step {resume_stage} page {resume_page}")
        typer.echo("")


@app.command()
def reset(
    stage: str = typer.Option(
        None,
        "--stage",
        help="Pipeline step to reset: render, ocr, equations, correction. Omit to reset all.",
    ),
    pdf: Path = typer.Option(None, "--pdf", help="Reset one source PDF's checkpoint/output state."),
    doc: str = typer.Option(None, "--doc", help="Reset one document slug's checkpoint/output state."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt."),
) -> None:
    """Reset document-scoped pipeline outputs and checkpoint records."""
    from config import correction_dir_for, crops_dir_for, document_checkpoint_dir, equations_dir_for, ocr_dir_for, render_dir_for
    from pipeline.checkpoint import STAGES, init_db, reset_stage

    stage_order = list(STAGES)

    def _stage_dirs(document: str) -> dict[str, list[Path]]:
        return {
            "render": [render_dir_for(document)],
            "ocr": [ocr_dir_for(document)],
            "equations": [equations_dir_for(document), crops_dir_for(document)],
            "correction": [correction_dir_for(document)],
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
        typer.echo(f"Unknown pipeline step '{stage}'. Choose from: {', '.join(sorted(valid))}.", err=True)
        raise typer.Exit(1)

    target_docs = [_resolve_document(pdf, doc)] if (pdf or doc) else _known_documents()
    if not target_docs:
        typer.echo("No document-scoped checkpoint state found.")
        raise typer.Exit()

    targets = stage_order if stage is None else stage_order[stage_order.index(stage):]

    if not yes:
        typer.confirm(
            f"Reset outputs and checkpoints for {', '.join(target_docs)}: {', '.join(targets)}? This cannot be undone.",
            abort=True,
        )

    init_db()
    for document in target_docs:
        typer.echo(f"Document: {document}")
        stage_dirs = _stage_dirs(document)
        for s in targets:
            removed = 0
            for out_dir in stage_dirs.get(s, []):
                removed += _clear_dir(out_dir)
                if out_dir.exists() and not any(out_dir.iterdir()):
                    out_dir.rmdir()
            reset_stage(s, document)
            typer.echo(f"  Reset: {s}  ({removed} files removed)")
        doc_dir = document_checkpoint_dir(document)
        if doc_dir.exists() and not any(doc_dir.iterdir()):
            doc_dir.rmdir()


if __name__ == "__main__":
    app()
