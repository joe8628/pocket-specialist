"""Typed runtime configuration for the document intelligence pipeline."""

from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def slugify_document_name(name: str) -> str:
    normalized = unicodedata.normalize("NFKD", name)
    ascii_text = normalized.encode("ascii", errors="ignore").decode().lower()
    slug = _NON_ALNUM_RE.sub("-", ascii_text).strip("-")
    return slug or "document"


@dataclass(frozen=True)
class PipelinePaths:
    project_root: Path
    corpus_dir: Path
    checkpoint_dir: Path
    output_dir: Path
    models_dir: Path
    database_path: Path

    def document_slug(self, pdf_path: Path) -> str:
        return slugify_document_name(pdf_path.stem)

    def document_checkpoint_dir(self, document: str) -> Path:
        return self.checkpoint_dir / document

    def render_dir_for(self, document: str) -> Path:
        return self.document_checkpoint_dir(document) / "rendered"

    def ocr_dir_for(self, document: str) -> Path:
        return self.document_checkpoint_dir(document) / "ocr"

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


@dataclass(frozen=True)
class OCRProviderSettings:
    provider: str = "surya"
    language: str = "en"
    batch_size: int = 1


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
    rendering: RenderingSettings = field(default_factory=RenderingSettings)
    ocr: OCRProviderSettings = field(default_factory=OCRProviderSettings)
    equations: EquationSettings = field(default_factory=EquationSettings)
    ollama: OllamaSettings = field(default_factory=OllamaSettings)
    runtime: RuntimeSettings = field(default_factory=RuntimeSettings)

    @classmethod
    def from_env(cls, project_root: Path | None = None) -> "PipelineSettings":
        root = project_root or Path(__file__).resolve().parents[2]
        checkpoint_dir = Path(os.getenv("PIPELINE_CHECKPOINT_DIR", root / "checkpoints")).resolve()
        output_dir = Path(os.getenv("PIPELINE_OUTPUT_DIR", root / "output")).resolve()
        models_dir = Path(os.getenv("PIPELINE_MODELS_DIR", root / "models")).resolve()
        corpus_dir = Path(os.getenv("PIPELINE_CORPUS_DIR", root / "RAG-corpus")).resolve()
        database_path = Path(os.getenv("PIPELINE_DB_PATH", checkpoint_dir / "pipeline.db")).resolve()

        paths = PipelinePaths(
            project_root=root.resolve(),
            corpus_dir=corpus_dir,
            checkpoint_dir=checkpoint_dir,
            output_dir=output_dir,
            models_dir=models_dir,
            database_path=database_path,
        )
        return cls(
            paths=paths,
            rendering=RenderingSettings(zoom=float(os.getenv("PIPELINE_RENDER_ZOOM", "2.0"))),
            ocr=OCRProviderSettings(
                provider=os.getenv("PIPELINE_OCR_PROVIDER", "surya"),
                language=os.getenv("PIPELINE_OCR_LANG", "en"),
                batch_size=int(os.getenv("PIPELINE_OCR_BATCH_SIZE", "1")),
            ),
            equations=EquationSettings(
                confidence_threshold=float(os.getenv("PIPELINE_EQUATION_CONF_THRESHOLD", "0.5")),
                header_strip_ratio=float(os.getenv("PIPELINE_HEADER_STRIP_RATIO", "0.10")),
                footer_strip_ratio=float(os.getenv("PIPELINE_FOOTER_STRIP_RATIO", "0.90")),
            ),
            ollama=OllamaSettings(
                model=os.getenv("PIPELINE_OLLAMA_MODEL", "qwen2.5vl:3b"),
                base_url=os.getenv("PIPELINE_OLLAMA_BASE", "http://localhost:11434"),
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
