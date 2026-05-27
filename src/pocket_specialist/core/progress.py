"""Temporary development progress output for ingestion phases.

This module is intentionally simple and removable before release. It provides
human-readable phase status, progress bars, validation messages, and error
messages without changing phase contracts.
"""

from __future__ import annotations

import sys


def _emit(message: str) -> None:
    print(message, flush=True)


def phase_start(phase: str, detail: str | None = None) -> None:
    suffix = f" - {detail}" if detail else ""
    _emit(f"[START] {phase}{suffix}")


def phase_validation(phase: str, message: str) -> None:
    _emit(f"[VALIDATION] {phase}: {message}")


def phase_complete(phase: str, message: str) -> None:
    _emit(f"[COMPLETED] {phase}: {message}")


def phase_error(phase: str, message: str) -> None:
    print(f"[ERROR] {phase}: {message}", file=sys.stderr, flush=True)


def model_status(phase: str, message: str) -> None:
    _emit(f"[MODEL] {phase}: {message}")


def progress_bar(phase: str, current: int, total: int, detail: str = "") -> None:
    total = max(total, 1)
    current = min(max(current, 0), total)
    width = 28
    filled = int(width * current / total)
    bar = "#" * filled + "-" * (width - filled)
    percent = int(100 * current / total)
    suffix = f" {detail}" if detail else ""
    _emit(f"[PROGRESS] {phase}: [{bar}] {current}/{total} {percent:3d}%{suffix}")
