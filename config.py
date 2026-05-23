"""Compatibility configuration facade backed by typed pipeline settings."""

from __future__ import annotations

from pathlib import Path

from pipeline.foundation.config import get_settings, slugify_document_name

SETTINGS = get_settings()

PROJECT_ROOT = SETTINGS.paths.project_root
CORPUS_DIR = SETTINGS.paths.corpus_dir

CHECKPOINT_DIR = SETTINGS.paths.checkpoint_dir
OUTPUT_DIR = SETTINGS.paths.output_dir
MODELS_DIR = SETTINGS.paths.models_dir

RENDER_ZOOM = SETTINGS.rendering.zoom
OCR_LANG = SETTINGS.ocr.language
EQUATION_CONF_THRESHOLD = SETTINGS.equations.confidence_threshold
HEADER_STRIP_RATIO = SETTINGS.equations.header_strip_ratio
FOOTER_STRIP_RATIO = SETTINGS.equations.footer_strip_ratio
OLLAMA_MODEL = SETTINGS.ollama.model
OLLAMA_BASE = SETTINGS.ollama.base_url
MAX_RETRIES = SETTINGS.runtime.max_retries
DB_PATH = SETTINGS.paths.database_path


def document_slug(pdf_path: Path) -> str:
    return SETTINGS.paths.document_slug(pdf_path)


def document_checkpoint_dir(document: str) -> Path:
    return SETTINGS.paths.document_checkpoint_dir(document)


def render_dir_for(document: str) -> Path:
    return SETTINGS.paths.render_dir_for(document)


def ocr_dir_for(document: str) -> Path:
    return SETTINGS.paths.ocr_dir_for(document)


def equations_dir_for(document: str) -> Path:
    return SETTINGS.paths.equations_dir_for(document)


def crops_dir_for(document: str) -> Path:
    return SETTINGS.paths.crops_dir_for(document)


def correction_dir_for(document: str) -> Path:
    return SETTINGS.paths.correction_dir_for(document)


def output_dir_for(document: str, base_output_dir: Path | None = None) -> Path:
    return SETTINGS.paths.output_dir_for(document, base_output_dir=base_output_dir)


__all__ = [
    "CHECKPOINT_DIR",
    "CORPUS_DIR",
    "DB_PATH",
    "EQUATION_CONF_THRESHOLD",
    "FOOTER_STRIP_RATIO",
    "HEADER_STRIP_RATIO",
    "MAX_RETRIES",
    "MODELS_DIR",
    "OCR_LANG",
    "OLLAMA_BASE",
    "OLLAMA_MODEL",
    "OUTPUT_DIR",
    "PROJECT_ROOT",
    "RENDER_ZOOM",
    "SETTINGS",
    "correction_dir_for",
    "crops_dir_for",
    "document_checkpoint_dir",
    "document_slug",
    "equations_dir_for",
    "ocr_dir_for",
    "output_dir_for",
    "render_dir_for",
    "slugify_document_name",
]
