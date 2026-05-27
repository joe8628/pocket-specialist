"""Typed runtime configuration for the document intelligence pipeline."""

from __future__ import annotations

import os
import re
import tomllib
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")
_DEFAULT_SCANNED_ZOOM = 300.0 / 72.0


def slugify_document_name(name: str) -> str:
    normalized = unicodedata.normalize("NFKD", name)
    ascii_text = normalized.encode("ascii", errors="ignore").decode().lower()
    slug = _NON_ALNUM_RE.sub("-", ascii_text).strip("-")
    return slug or "document"


def _resolve_path(root: Path, value: str | Path) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = root / path
    return path.resolve()


def _nested_get(mapping: dict[str, Any], *keys: str, default: Any = None) -> Any:
    current: Any = mapping
    for key in keys:
        if not isinstance(current, dict):
            return default
        current = current.get(key)
        if current is None:
            return default
    return current


def _default_project_root() -> Path:
    start = Path(__file__).resolve()
    for candidate in start.parents:
        if (candidate / "pyproject.toml").exists() and (candidate / "src" / "pocket_specialist").is_dir():
            return candidate
    return start.parents[3]


@dataclass(frozen=True)
class PipelinePaths:
    project_root: Path
    corpus_dir: Path
    checkpoint_dir: Path
    output_dir: Path
    models_dir: Path
    database_path: Path
    artifact_path: Path
    chroma_path: Path
    config_path: Path | None = None

    def document_slug(self, pdf_path: Path) -> str:
        return slugify_document_name(pdf_path.stem)

    def document_checkpoint_dir(self, document: str) -> Path:
        return self.checkpoint_dir / document

    def render_dir_for(self, document: str) -> Path:
        return self.document_checkpoint_dir(document) / "rendered"

    def ocr_dir_for(self, document: str) -> Path:
        return self.document_checkpoint_dir(document) / "ocr"

    def layout_dir_for(self, document: str) -> Path:
        return self.document_checkpoint_dir(document) / "layout"

    def structured_dir_for(self, document: str) -> Path:
        return self.document_checkpoint_dir(document) / "structured"

    def equations_dir_for(self, document: str) -> Path:
        return self.document_checkpoint_dir(document) / "equations"

    def crops_dir_for(self, document: str) -> Path:
        return self.document_checkpoint_dir(document) / "crops"

    def correction_dir_for(self, document: str) -> Path:
        return self.document_checkpoint_dir(document) / "corrected"

    def output_dir_for(self, document: str, base_output_dir: Path | None = None) -> Path:
        return (base_output_dir or self.output_dir) / document


@dataclass(frozen=True)
class RenderingSettings:
    zoom: float = 2.0
    scanned_pdf_zoom: float = _DEFAULT_SCANNED_ZOOM


@dataclass(frozen=True)
class OCRProviderSettings:
    provider: str = "glm-ocr"
    fallback_provider: str = "deepseek-ocr"
    language: str = "en"
    batch_size: int = 4
    ollama_base_url: str = "http://localhost:11434"
    timeout_seconds: int = 60


@dataclass(frozen=True)
class LayoutSettings:
    provider: str = "pp-doclayout-v3"
    enabled: bool = True


@dataclass(frozen=True)
class FormulaSettings:
    enabled: bool = True
    base_url: str = "http://localhost:8001"
    model_size: str = "base"
    fallback_to_ocr: bool = True
    timeout_seconds: int = 60


@dataclass(frozen=True)
class EmbeddingSettings:
    provider: str = "nomic-embed-text"
    ollama_base_url: str = "http://localhost:11434"


@dataclass(frozen=True)
class GPUSettings:
    serialized_execution: bool = True
    max_gpu_workers: int = 1


@dataclass(frozen=True)
class ChunkingSettings:
    max_tokens: int = 512
    overlap_tokens: int = 64


@dataclass(frozen=True)
class BatchSettings:
    structured_corpus_enabled: bool = False


@dataclass(frozen=True)
class StorageSettings:
    sqlite_path: Path
    artifact_path: Path
    chroma_path: Path


@dataclass(frozen=True)
class EquationSettings:
    confidence_threshold: float = 0.5
    header_strip_ratio: float = 0.10
    footer_strip_ratio: float = 0.90


@dataclass(frozen=True)
class OllamaSettings:
    model: str = "qwen2.5vl:3b"
    base_url: str = "http://localhost:11434"


@dataclass(frozen=True)
class RuntimeSettings:
    max_retries: int = 3
    gpu_serialization_timeout_s: float = 600.0
    gpu_enabled: bool = True


@dataclass(frozen=True)
class PipelineSettings:
    paths: PipelinePaths
    pipeline_version: str = "0.5.1-beta"
    rendering: RenderingSettings = field(default_factory=RenderingSettings)
    ocr: OCRProviderSettings = field(default_factory=OCRProviderSettings)
    layout: LayoutSettings = field(default_factory=LayoutSettings)
    formula: FormulaSettings = field(default_factory=FormulaSettings)
    embedding: EmbeddingSettings = field(default_factory=EmbeddingSettings)
    gpu: GPUSettings = field(default_factory=GPUSettings)
    chunking: ChunkingSettings = field(default_factory=ChunkingSettings)
    batch: BatchSettings = field(default_factory=BatchSettings)
    storage: StorageSettings = field(default_factory=lambda: StorageSettings(Path("pipeline.db"), Path("artifacts"), Path("chroma")))
    equations: EquationSettings = field(default_factory=EquationSettings)
    ollama: OllamaSettings = field(default_factory=OllamaSettings)
    runtime: RuntimeSettings = field(default_factory=RuntimeSettings)

    @classmethod
    def from_env(cls, project_root: Path | None = None) -> "PipelineSettings":
        root = (project_root or _default_project_root()).resolve()
        config_path_env = os.getenv("PIPELINE_CONFIG")
        config_path = _resolve_path(root, config_path_env) if config_path_env else root / "pipeline.toml"
        config_data: dict[str, Any] = {}
        if config_path.exists():
            config_data = tomllib.loads(config_path.read_text(encoding="utf-8"))

        checkpoint_dir = _resolve_path(root, os.getenv("PIPELINE_CHECKPOINT_DIR", root / "checkpoints"))
        output_dir = _resolve_path(root, os.getenv("PIPELINE_OUTPUT_DIR", root / "output"))
        models_dir = _resolve_path(root, os.getenv("PIPELINE_MODELS_DIR", root / "models"))
        corpus_dir = _resolve_path(root, os.getenv("PIPELINE_CORPUS_DIR", root / "RAG-corpus"))

        sqlite_path = _resolve_path(root, os.getenv("PIPELINE_DB_PATH", _nested_get(config_data, "storage", "sqlite_path", default="./data/pipeline.db")))
        artifact_path = _resolve_path(root, _nested_get(config_data, "storage", "artifact_path", default="./data/artifacts"))
        chroma_path = _resolve_path(root, _nested_get(config_data, "storage", "chroma_path", default="./data/chroma"))

        paths = PipelinePaths(
            project_root=root,
            corpus_dir=corpus_dir,
            checkpoint_dir=checkpoint_dir,
            output_dir=output_dir,
            models_dir=models_dir,
            database_path=sqlite_path,
            artifact_path=artifact_path,
            chroma_path=chroma_path,
            config_path=config_path if config_path.exists() else None,
        )
        return cls(
            paths=paths,
            pipeline_version=str(_nested_get(config_data, "pipeline", "version", default="0.5.1-beta")),
            rendering=RenderingSettings(
                zoom=float(os.getenv("PIPELINE_RENDER_ZOOM", str(_nested_get(config_data, "rendering", "zoom", default=2.0)))),
                scanned_pdf_zoom=float(os.getenv("PIPELINE_SCANNED_RENDER_ZOOM", str(_nested_get(config_data, "rendering", "scanned_pdf_zoom", default=_DEFAULT_SCANNED_ZOOM)))),
            ),
            ocr=OCRProviderSettings(
                provider=os.getenv("PIPELINE_OCR_PROVIDER", str(_nested_get(config_data, "ocr", "provider", default="glm-ocr"))),
                fallback_provider=os.getenv("PIPELINE_OCR_FALLBACK_PROVIDER", str(_nested_get(config_data, "ocr", "fallback_provider", default="deepseek-ocr"))),
                language=os.getenv("PIPELINE_OCR_LANG", str(_nested_get(config_data, "ocr", "language", default="en"))),
                batch_size=int(os.getenv("PIPELINE_OCR_BATCH_SIZE", str(_nested_get(config_data, "ocr", "batch_size", default=4)))),
                ollama_base_url=os.getenv("PIPELINE_OCR_OLLAMA_BASE", str(_nested_get(config_data, "ocr", "ollama_base_url", default="http://localhost:11434"))),
                timeout_seconds=int(os.getenv("PIPELINE_OCR_TIMEOUT_SECONDS", str(_nested_get(config_data, "ocr", "timeout_seconds", default=60)))),
            ),
            layout=LayoutSettings(
                provider=os.getenv("PIPELINE_LAYOUT_PROVIDER", str(_nested_get(config_data, "layout", "provider", default="pp-doclayout-v3"))),
                enabled=os.getenv("PIPELINE_LAYOUT_ENABLED", str(_nested_get(config_data, "layout", "enabled", default=True))).lower() not in {"0", "false", "no"},
            ),
            formula=FormulaSettings(
                enabled=os.getenv("PIPELINE_FORMULA_ENABLED", str(_nested_get(config_data, "formula", "enabled", default=True))).lower() not in {"0", "false", "no"},
                base_url=os.getenv("PIPELINE_FORMULA_BASE_URL", str(_nested_get(config_data, "formula", "base_url", default="http://localhost:8001"))),
                model_size=os.getenv("PIPELINE_FORMULA_MODEL_SIZE", str(_nested_get(config_data, "formula", "model_size", default="base"))),
                fallback_to_ocr=os.getenv("PIPELINE_FORMULA_FALLBACK_TO_OCR", str(_nested_get(config_data, "formula", "fallback_to_ocr", default=True))).lower() not in {"0", "false", "no"},
                timeout_seconds=int(os.getenv("PIPELINE_FORMULA_TIMEOUT_SECONDS", str(_nested_get(config_data, "formula", "timeout_seconds", default=60)))),
            ),
            embedding=EmbeddingSettings(
                provider=os.getenv("PIPELINE_EMBEDDING_PROVIDER", str(_nested_get(config_data, "embedding", "provider", default="nomic-embed-text"))),
                ollama_base_url=os.getenv("PIPELINE_EMBEDDING_OLLAMA_BASE", str(_nested_get(config_data, "embedding", "ollama_base_url", default="http://localhost:11434"))),
            ),
            gpu=GPUSettings(
                serialized_execution=os.getenv("PIPELINE_GPU_SERIALIZED_EXECUTION", str(_nested_get(config_data, "gpu", "serialized_execution", default=True))).lower() not in {"0", "false", "no"},
                max_gpu_workers=int(os.getenv("PIPELINE_GPU_MAX_WORKERS", str(_nested_get(config_data, "gpu", "max_gpu_workers", default=1)))),
            ),
            chunking=ChunkingSettings(
                max_tokens=int(os.getenv("PIPELINE_CHUNK_MAX_TOKENS", str(_nested_get(config_data, "chunking", "max_tokens", default=512)))),
                overlap_tokens=int(os.getenv("PIPELINE_CHUNK_OVERLAP_TOKENS", str(_nested_get(config_data, "chunking", "overlap_tokens", default=64)))),
            ),
            batch=BatchSettings(
                structured_corpus_enabled=os.getenv(
                    "PIPELINE_STRUCTURED_CORPUS_ENABLED",
                    str(_nested_get(config_data, "batch", "structured_corpus_enabled", default=False)),
                ).lower()
                not in {"0", "false", "no"},
            ),
            storage=StorageSettings(sqlite_path=sqlite_path, artifact_path=artifact_path, chroma_path=chroma_path),
            equations=EquationSettings(
                confidence_threshold=float(os.getenv("PIPELINE_EQUATION_CONF_THRESHOLD", "0.5")),
                header_strip_ratio=float(os.getenv("PIPELINE_HEADER_STRIP_RATIO", "0.10")),
                footer_strip_ratio=float(os.getenv("PIPELINE_FOOTER_STRIP_RATIO", "0.90")),
            ),
            ollama=OllamaSettings(
                model=os.getenv("PIPELINE_OLLAMA_MODEL", "qwen2.5vl:3b"),
                base_url=os.getenv("PIPELINE_OLLAMA_BASE", os.getenv("PIPELINE_OCR_OLLAMA_BASE", str(_nested_get(config_data, "ocr", "ollama_base_url", default="http://localhost:11434")))),
            ),
            runtime=RuntimeSettings(
                max_retries=int(os.getenv("PIPELINE_MAX_RETRIES", "3")),
                gpu_serialization_timeout_s=float(os.getenv("PIPELINE_GPU_TIMEOUT_S", "600.0")),
                gpu_enabled=os.getenv("PIPELINE_GPU_ENABLED", "1").lower() not in {"0", "false", "no"},
            ),
        )


@lru_cache(maxsize=1)
def get_settings() -> PipelineSettings:
    return PipelineSettings.from_env()

# Compatibility constants and path helpers for modules that have not yet moved to
# direct settings injection. These live in core config, not a wrapper module.
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


def layout_dir_for(document: str) -> Path:
    return SETTINGS.paths.layout_dir_for(document)


def structured_dir_for(document: str) -> Path:
    return SETTINGS.paths.structured_dir_for(document)


def equations_dir_for(document: str) -> Path:
    return SETTINGS.paths.equations_dir_for(document)


def crops_dir_for(document: str) -> Path:
    return SETTINGS.paths.crops_dir_for(document)


def correction_dir_for(document: str) -> Path:
    return SETTINGS.paths.correction_dir_for(document)


def output_dir_for(document: str, base_output_dir: Path | None = None) -> Path:
    return SETTINGS.paths.output_dir_for(document, base_output_dir=base_output_dir)
