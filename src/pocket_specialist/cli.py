"""Document intelligence CLI for the Phase A foundation and compatibility workflows."""
from __future__ import annotations

import sys
import json
import time
from datetime import datetime
from pathlib import Path

import typer

app = typer.Typer(
    help="Pocket Specialist: local-first document intelligence pipeline.",
    no_args_is_help=True,
)


def _doc_slug(pdf_path: Path) -> str:
    from pocket_specialist.core.config import document_slug

    return document_slug(pdf_path.resolve())


def _known_documents() -> list[str]:
    from pocket_specialist.core.config import CHECKPOINT_DIR
    from pocket_specialist.storage.checkpoint import get_documents, init_db

    reserved_roots = {"rendered", "ocr", "layout", "equations", "crops", "corrected", "structured", "__pycache__"}
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
    from pocket_specialist.storage.checkpoint import STAGES, get_done_pages, init_db

    init_db()
    for stage in reversed(STAGES):
        pages = get_done_pages(stage, document)
        if pages:
            return stage, max(pages)
    return None, None

def _format_duration(elapsed_seconds: float) -> str:
    total_seconds = int(elapsed_seconds)
    h, rem = divmod(total_seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def _benchmark_stamp(started_at: float) -> str:
    finished_at = datetime.now().isoformat(timespec="seconds")
    return f"elapsed={_format_duration(time.monotonic() - started_at)} finished_at={finished_at}"



def _provider_slug(provider_name: str) -> str:
    return provider_name.strip().lower().replace(" ", "-").replace("_", "-")


def _provider_scoped_dir(base_dir: Path, provider_name: str | None) -> Path:
    if provider_name is None:
        return base_dir
    return base_dir / _provider_slug(provider_name)


def _run_layout_detection_command(pdf: Path, output_dir: Path | None, *, provider_name: str | None = None) -> None:
    from pocket_specialist.core.config import layout_dir_for
    from pocket_specialist.phases.extract import detect_layout_document

    started_at = time.monotonic()
    pdf_path = pdf.resolve()
    document = _doc_slug(pdf_path)
    base_output_dir = output_dir or layout_dir_for(document)
    target_dir = _provider_scoped_dir(base_output_dir, provider_name)
    done, failed, _ = detect_layout_document(pdf_path, layout_output_dir=target_dir, layout_provider_name=provider_name)
    typer.echo(f"Layout detection complete: {done} done, {failed} failed. Output: {target_dir} {_benchmark_stamp(started_at)}")
    if failed:
        raise typer.Exit(1)


def _run_extract_structured_command(
    source: Path,
    output_dir: Path | None,
    layout_output_dir: Path | None,
    *,
    provider_name: str | None = None,
) -> None:
    from pocket_specialist.core.config import layout_dir_for, structured_dir_for
    from pocket_specialist.handlers.intake import classify_document
    from pocket_specialist.phases.extract import extract_structured_document

    started_at = time.monotonic()
    source_path = source.resolve()
    profile = classify_document(source_path)
    base_structured_target = output_dir or structured_dir_for(profile.doc_id)
    base_layout_target = layout_output_dir or layout_dir_for(profile.doc_id)
    structured_target = _provider_scoped_dir(base_structured_target, provider_name)
    layout_target = _provider_scoped_dir(base_layout_target, provider_name)
    done, failed, cif = extract_structured_document(
        source_path,
        structured_output_dir=structured_target,
        layout_output_dir=layout_target,
        layout_provider_name=provider_name,
    )
    typer.echo(
        f"Structured extraction complete: {done} done, {failed} failed. "
        f"Blocks: {len(cif.blocks)}. Output: {structured_target / 'document.json'} "
        f"Layout: {layout_target} {_benchmark_stamp(started_at)}"
    )
    if failed:
        raise typer.Exit(1)


# ── Corpus Intake ──────────────────────────────────────────────────────────────

@app.command(name="rename-corpus")
def rename_corpus_cmd(
    corpus_dir: Path = typer.Argument(None, help="Directory containing corpus PDFs (default: RAG-corpus)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print proposed renames without touching files."),
    manifest: Path = typer.Option(None, "--manifest", help="Override manifest output path."),
) -> None:
    """Normalize corpus filenames and write a metadata manifest."""
    from pocket_specialist.core.config import CHECKPOINT_DIR, CORPUS_DIR
    from pocket_specialist.compat.rename import rename_corpus

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
    zoom: float = typer.Option(2.0, help="Render scale factor (2.0 ≈ 144 DPI). Ignored when --dpi is provided."),
    dpi: int | None = typer.Option(None, "--dpi", min=72, help="Requested render DPI before max_dpi clamping."),
    start_page: int = typer.Option(1, "--start-page", help="First page (1-indexed)."),
    end_page: int = typer.Option(None, "--end-page", help="Last page inclusive. Default: last page."),
    output_dir: Path = typer.Option(None, "--output-dir", help="Override document-scoped PNG output directory."),
) -> None:
    """Render PDF pages to document-scoped PNG images."""
    from pocket_specialist.core.config import render_dir_for
    from pocket_specialist.compat.render import render_pdf

    pdf_path = pdf.resolve()
    out = output_dir or render_dir_for(_doc_slug(pdf_path))
    render_pdf(pdf_path, output_dir=out, zoom=zoom, dpi=dpi, start_page=start_page, end_page=end_page)


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
    from pocket_specialist.core.config import ocr_dir_for, render_dir_for
    from pocket_specialist.compat.ocr import ocr_pages

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


@app.command(name="serve-surya-layout")
def serve_surya_layout(
    host: str = typer.Option("127.0.0.1", "--host", help="Host interface for the Surya layout microservice."),
    port: int = typer.Option(8002, "--port", help="TCP port for the Surya layout microservice."),
) -> None:
    """Run the isolated Surya layout microservice."""
    from pocket_specialist.layout.service import serve

    serve(host=host, port=port)


@app.command(name="serve-paddleocr-layout")
def serve_paddleocr_layout(
    host: str = typer.Option("127.0.0.1", "--host", help="Host interface for the PaddleOCR layout microservice."),
    port: int = typer.Option(8003, "--port", help="TCP port for the PaddleOCR layout microservice."),
) -> None:
    """Run the isolated PaddleOCR layout microservice."""
    from pocket_specialist.layout.paddle_service import serve

    serve(host=host, port=port)


@app.command(name="layout")
def layout_detect(
    pdf: Path = typer.Argument(..., help="Source PDF file."),
    output_dir: Path = typer.Option(None, "--output-dir", help="Override document-scoped layout JSON output directory."),
) -> None:
    """Detect page layout regions and write per-page layout JSON."""
    _run_layout_detection_command(pdf, output_dir)


@app.command(name="layout-surya")
def layout_detect_surya(
    pdf: Path = typer.Argument(..., help="Source PDF file."),
    output_dir: Path = typer.Option(None, "--output-dir", help="Override the base layout output directory. Results are written under a surya-layout-service subfolder by default."),
) -> None:
    """Detect layout with the Surya layout service without changing global config."""
    _run_layout_detection_command(pdf, output_dir, provider_name="surya-layout-service")


@app.command(name="layout-pp-doclayout-v3")
def layout_detect_pp_doclayout_v3(
    pdf: Path = typer.Argument(..., help="Source PDF file."),
    output_dir: Path = typer.Option(None, "--output-dir", help="Override the base layout output directory. Results are written under a pp-doclayout-v3 subfolder by default."),
) -> None:
    """Detect layout with PP-DocLayoutV3 without changing global config."""
    _run_layout_detection_command(pdf, output_dir, provider_name="pp-doclayout-v3")


@app.command(name="compare-layout-page")
def compare_layout_page_cmd(
    pdf: Path = typer.Argument(..., help="Source PDF file."),
    page: int = typer.Option(..., "--page", min=1, help="1-indexed PDF page to compare."),
    dpi: int = typer.Option(192, "--dpi", min=72, help="Render DPI used for both providers."),
    output_dir: Path = typer.Option(Path("/tmp/layout_compare"), "--output-dir", help="Directory for side-by-side provider artifacts."),
) -> None:
    """Compare page-level layout artifacts side by side for the available providers."""
    from pocket_specialist.layout.compare import compare_layout_page

    started_at = time.monotonic()
    summary = compare_layout_page(pdf.resolve(), page_num=page, dpi=dpi, output_dir=output_dir.resolve())
    typer.echo(json.dumps(summary, indent=2))
    typer.echo(_benchmark_stamp(started_at))


@app.command(name="extract-structured")
def extract_structured(
    source: Path = typer.Argument(..., help="Source document. Supports PDF and HTML."),
    output_dir: Path = typer.Option(None, "--output-dir", help="Override document-scoped structured JSON output directory."),
    layout_output_dir: Path = typer.Option(None, "--layout-output-dir", help="Override document-scoped layout JSON output directory."),
) -> None:
    """Run the Phase B structured extraction path for PDF or HTML input."""
    _run_extract_structured_command(source, output_dir, layout_output_dir)


@app.command(name="extract-structured-surya")
def extract_structured_surya(
    source: Path = typer.Argument(..., help="Source document. Supports PDF and HTML."),
    output_dir: Path = typer.Option(None, "--output-dir", help="Override the base structured output directory. Results are written under a surya-layout-service subfolder by default."),
    layout_output_dir: Path = typer.Option(None, "--layout-output-dir", help="Override the base layout output directory. Results are written under a surya-layout-service subfolder by default."),
) -> None:
    """Run structured extraction with the Surya layout service without changing global config."""
    _run_extract_structured_command(source, output_dir, layout_output_dir, provider_name="surya-layout-service")


@app.command(name="extract-structured-pp-doclayout-v3")
def extract_structured_pp_doclayout_v3(
    source: Path = typer.Argument(..., help="Source document. Supports PDF and HTML."),
    output_dir: Path = typer.Option(None, "--output-dir", help="Override the base structured output directory. Results are written under a pp-doclayout-v3 subfolder by default."),
    layout_output_dir: Path = typer.Option(None, "--layout-output-dir", help="Override the base layout output directory. Results are written under a pp-doclayout-v3 subfolder by default."),
) -> None:
    """Run structured extraction with PP-DocLayoutV3 without changing global config."""
    _run_extract_structured_command(source, output_dir, layout_output_dir, provider_name="pp-doclayout-v3")


@app.command(name="extract-structured-corpus")
def extract_structured_corpus(
    corpus_dir: Path = typer.Option(None, "--corpus-dir", help="Corpus directory to scan (default: RAG-corpus)."),
    output_root: Path = typer.Option(None, "--output-root", help="Root for structured JSON outputs. Defaults to checkpoints/<doc>/structured."),
    layout_output_root: Path = typer.Option(None, "--layout-output-root", help="Root for layout JSON outputs. Defaults to checkpoints/<doc>/layout."),
    enabled: bool = typer.Option(False, "--enabled", help="Enable this gated batch subroutine for the current run."),
    dry_run: bool = typer.Option(False, "--dry-run", help="List matching files without running extraction."),
    recursive: bool = typer.Option(True, "--recursive/--flat", help="Scan corpus subdirectories recursively."),
    limit: int = typer.Option(None, "--limit", min=1, help="Maximum number of files to process."),
    stop_on_error: bool = typer.Option(False, "--stop-on-error", help="Stop at the first failed document."),
) -> None:
    """Run extract-structured for supported files found in the corpus directory."""
    from pocket_specialist.core.config import get_settings, layout_dir_for, structured_dir_for
    from pocket_specialist.handlers.intake import classify_document
    from pocket_specialist.phases.extract import extract_structured_document

    settings = get_settings()
    if not enabled and not settings.batch.structured_corpus_enabled:
        typer.echo(
            "Structured corpus extraction is disabled. Use --enabled, "
            "PIPELINE_STRUCTURED_CORPUS_ENABLED=1, or [batch].structured_corpus_enabled=true."
        )
        raise typer.Exit()

    root = (corpus_dir or settings.paths.corpus_dir).resolve()
    if not root.exists() or not root.is_dir():
        typer.echo(f"Corpus directory does not exist: {root}", err=True)
        raise typer.Exit(1)

    supported_suffixes = {
        ".pdf", ".html", ".htm", ".txt", ".log", ".md", ".markdown",
        ".csv", ".tsv", ".png", ".jpg", ".jpeg", ".tif", ".tiff",
        ".docx", ".odt", ".xlsx", ".epub",
    }
    iterator = root.rglob("*") if recursive else root.glob("*")
    candidates = sorted(path for path in iterator if path.is_file() and path.suffix.lower() in supported_suffixes)
    if limit is not None:
        candidates = candidates[:limit]

    if not candidates:
        typer.echo(f"No supported files found in {root}.")
        raise typer.Exit()

    started_at = time.monotonic()
    typer.echo(f"Structured corpus extraction: {len(candidates)} file(s) from {root}")
    if dry_run:
        for source_path in candidates:
            typer.echo(f"  DRY-RUN {source_path}")
        raise typer.Exit()

    completed = 0
    failed_docs = 0
    for index, source_path in enumerate(candidates, 1):
        typer.echo(f"\n[{index}/{len(candidates)}] {source_path.name}")
        try:
            profile = classify_document(source_path)
            structured_target = (output_root / profile.doc_id) if output_root is not None else structured_dir_for(profile.doc_id)
            layout_target = (layout_output_root / profile.doc_id) if layout_output_root is not None else layout_dir_for(profile.doc_id)
            done, failed, cif = extract_structured_document(
                source_path,
                structured_output_dir=structured_target,
                layout_output_dir=layout_target,
            )
            typer.echo(
                f"  done={done} failed={failed} blocks={len(cif.blocks)} "
                f"structured={structured_target / 'document.json'} layout={layout_target}"
            )
            if failed:
                failed_docs += 1
                if stop_on_error:
                    raise typer.Exit(1)
            else:
                completed += 1
        except Exception as exc:
            failed_docs += 1
            typer.echo(f"  failed: {exc}", err=True)
            if stop_on_error:
                raise typer.Exit(1) from exc

    typer.echo(
        f"\nStructured corpus extraction complete: {completed} succeeded, {failed_docs} failed. "
        f"{_benchmark_stamp(started_at)}"
    )
    if failed_docs:
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
    from pocket_specialist.core.config import crops_dir_for, equations_dir_for, ocr_dir_for, render_dir_for
    from pocket_specialist.compat.enrichment import enrich_document

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
    from pocket_specialist.core.config import correction_dir_for, crops_dir_for, equations_dir_for
    from pocket_specialist.compat.export import correct_pages

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
    from pocket_specialist.core.config import correction_dir_for, equations_dir_for, output_dir_for
    from pocket_specialist.serializers.markdown import assemble_document as _assemble

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
    from pocket_specialist.core.config import correction_dir_for, crops_dir_for, equations_dir_for, ocr_dir_for, output_dir_for, render_dir_for
    from pocket_specialist.serializers.markdown import assemble_document
    from pocket_specialist.compat.export import correct_pages
    from pocket_specialist.compat.enrichment import enrich_document
    from pocket_specialist.compat.ocr import ocr_pages
    from pocket_specialist.compat.render import render_pdf

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

    typer.echo(f"\nTotal time: {_benchmark_stamp(t0)}  ({pdf_path.name})")


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
    from pocket_specialist.core.config import CHECKPOINT_DIR
    from pocket_specialist.compat.rename import rename_corpus

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
    from pocket_specialist.core.config import CHECKPOINT_DIR, CORPUS_DIR
    from pocket_specialist.compat.rename import rename_corpus

    target = (corpus_dir or CORPUS_DIR).resolve()
    pdfs = sorted(target.glob("*.pdf"))
    if not pdfs:
        typer.echo(f"No PDFs found in {target}.", err=True)
        raise typer.Exit(1)

    run_all_started_at = time.monotonic()
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

    typer.echo(f"\nDone. Processed {len(pdfs)} PDFs. {_benchmark_stamp(run_all_started_at)}")


# ── Status + Reset ────────────────────────────────────────────────────────────

@app.command()
def status(
    pdf: Path = typer.Option(None, "--pdf", help="Show status for one source PDF."),
    doc: str = typer.Option(None, "--doc", help="Show status for one document slug."),
) -> None:
    """Show per-step progress from the checkpoint database."""
    from pocket_specialist.storage.checkpoint import STAGES, get_failed_pages, get_node_summary, get_resume_state, get_summary, init_db

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
        node_summary = get_node_summary(document)
        if node_summary:
            typer.echo("DAG nodes:")
            for node in sorted(node_summary):
                counts = node_summary[node]
                detail = "  ".join(f"{status}: {count}" for status, count in sorted(counts.items()))
                resume = get_resume_state(document, node)
                resume_detail = f"  resume page: {resume.next_page}" if resume.next_page else ""
                typer.echo(f"  {node.ljust(width)}: {detail}{resume_detail}")
        resume_stage, resume_page = _resume_state(document)
        if resume_stage and resume_page:
            typer.echo(f"Resume point: step {resume_stage} page {resume_page}")
        typer.echo("")


@app.command()
def reset(
    stage: str = typer.Option(
        None,
        "--stage",
        help="Pipeline step to reset: render, ocr, layout, equations, correction, structured. Omit to reset all.",
    ),
    pdf: Path = typer.Option(None, "--pdf", help="Reset one source PDF's checkpoint/output state."),
    doc: str = typer.Option(None, "--doc", help="Reset one document slug's checkpoint/output state."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt."),
) -> None:
    """Reset document-scoped pipeline outputs and checkpoint records."""
    from pocket_specialist.core.config import correction_dir_for, crops_dir_for, document_checkpoint_dir, equations_dir_for, layout_dir_for, ocr_dir_for, render_dir_for, structured_dir_for
    from pocket_specialist.storage.checkpoint import STAGES, init_db, reset_stage

    stage_order = list(STAGES)

    def _stage_dirs(document: str) -> dict[str, list[Path]]:
        return {
            "render": [render_dir_for(document)],
            "ocr": [ocr_dir_for(document)],
            "layout": [layout_dir_for(document)],
            "equations": [equations_dir_for(document), crops_dir_for(document)],
            "correction": [correction_dir_for(document)],
            "structured": [structured_dir_for(document)],
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


def main() -> None:
    app()


if __name__ == "__main__":
    main()
