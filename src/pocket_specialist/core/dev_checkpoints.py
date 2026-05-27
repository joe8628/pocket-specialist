"""Development-only stage checkpoints for ingestion debugging.

These checkpoints are intentionally separate from release checkpoint state. They are
verbose filesystem breadcrumbs meant to be removed before the pipeline reaches a
release state, not stabilized as a public API.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Mapping

from pocket_specialist.core.config import get_settings


ArtifactValue = bytes | str | Path


def _safe_name(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in value.strip())
    return cleaned or "checkpoint"


def development_stage_dir(document: str, stage: str) -> Path:
    return get_settings().paths.checkpoint_dir / document / "_dev_checkpoints" / _safe_name(stage)


def _markdown_value(value: object) -> str:
    if isinstance(value, str):
        return value
    return f"`{json.dumps(value, ensure_ascii=False, default=str)}`"


def _write_artifact(target_dir: Path, name: str, value: ArtifactValue) -> Path:
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / _safe_name(name)
    if isinstance(value, bytes):
        target.write_bytes(value)
        return target
    source = Path(value)
    if source.exists() and source.is_file():
        shutil.copy2(source, target)
        return target
    target.write_text(str(value), encoding="utf-8")
    return target


def write_development_artifact(document: str, stage: str, name: str, value: ArtifactValue) -> Path:
    """Write a debug artifact into the stage-scoped dev checkpoint folder."""
    return _write_artifact(development_stage_dir(document, stage) / "artifacts", name, value)


def write_development_checkpoint(
    document: str,
    stage: str,
    *,
    page: int | None = None,
    summary: Mapping[str, object] | None = None,
    artifacts: Mapping[str, ArtifactValue] | None = None,
) -> Path:
    """Write a markdown checkpoint after a development ingestion stage."""
    stage_dir = development_stage_dir(document, stage)
    stage_dir.mkdir(parents=True, exist_ok=True)
    artifact_paths: list[Path] = []
    if artifacts:
        artifact_dir = stage_dir / "artifacts"
        for name, value in artifacts.items():
            artifact_paths.append(_write_artifact(artifact_dir, name, value))

    filename = f"page_{page:04d}.md" if page is not None else "document.md"
    markdown_path = stage_dir / filename
    lines = [
        f"# Development Checkpoint: {stage}",
        "",
        f"- Document: `{document}`",
    ]
    if page is not None:
        lines.append(f"- Page: `{page}`")
    for key, value in sorted((summary or {}).items()):
        lines.append(f"- {key}: {_markdown_value(value)}")
    if artifact_paths:
        lines.extend(["", "## Artifacts"])
        for artifact_path in artifact_paths:
            lines.append(f"- `{artifact_path}`")
    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return markdown_path
