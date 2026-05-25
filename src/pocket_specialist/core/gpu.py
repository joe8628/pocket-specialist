"""Deterministic GPU ownership utilities for serialized model execution."""

from __future__ import annotations

import asyncio
import fcntl
import os
import time
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import BinaryIO

import torch

from .config import get_settings


class GPUScheduler:
    """Serialize GPU-heavy pipeline steps while keeping CPU work concurrent."""

    def __init__(self, lock_path: Path | str | None = None) -> None:
        self._lock_path = Path(lock_path or os.getenv("PIPELINE_GPU_LOCK_PATH", "/tmp/pocket-specialist-gpu.lock"))

    def _acquire_file_lock(self, timeout: float) -> BinaryIO:
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_file = self._lock_path.open("a+b")
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return lock_file
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    lock_file.close()
                    raise TimeoutError("Timed out waiting for GPU scheduler lock")
                remaining = max(deadline - time.monotonic(), 0.0)
                time.sleep(min(0.05, remaining))

    def _release_file_lock(self, lock_file: BinaryIO) -> None:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()

    @asynccontextmanager
    async def acquire(self, resource_type: str):
        del resource_type
        timeout = get_settings().runtime.gpu_serialization_timeout_s
        lock_file = await asyncio.to_thread(self._acquire_file_lock, timeout)
        try:
            yield
        finally:
            await asyncio.to_thread(self._release_file_lock, lock_file)
            self.release_memory()

    @contextmanager
    def claim(self, resource_type: str):
        del resource_type
        timeout = get_settings().runtime.gpu_serialization_timeout_s
        lock_file = self._acquire_file_lock(timeout)
        try:
            yield
        finally:
            self._release_file_lock(lock_file)
            self.release_memory()

    @staticmethod
    def release_memory() -> None:
        if torch.cuda.is_available():
            torch.cuda.empty_cache()


gpu_scheduler = GPUScheduler()
