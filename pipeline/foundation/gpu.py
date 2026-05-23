"""Deterministic GPU ownership utilities for serialized model execution."""

from __future__ import annotations

import asyncio
import threading
from contextlib import asynccontextmanager, contextmanager

import torch

from .config import get_settings


class GPUScheduler:
    """Serialize GPU-heavy pipeline steps while keeping CPU work concurrent."""

    def __init__(self) -> None:
        self._async_lock = asyncio.Lock()
        self._sync_lock = threading.Lock()

    @asynccontextmanager
    async def acquire(self, resource_type: str):
        del resource_type
        timeout = get_settings().runtime.gpu_serialization_timeout_s
        await asyncio.wait_for(self._async_lock.acquire(), timeout=timeout)
        try:
            yield
        finally:
            self._async_lock.release()
            self.release_memory()

    @contextmanager
    def claim(self, resource_type: str):
        del resource_type
        timeout = get_settings().runtime.gpu_serialization_timeout_s
        acquired = self._sync_lock.acquire(timeout=timeout)
        if not acquired:
            raise TimeoutError("Timed out waiting for GPU scheduler lock")
        try:
            yield
        finally:
            self._sync_lock.release()
            self.release_memory()

    @staticmethod
    def release_memory() -> None:
        if torch.cuda.is_available():
            torch.cuda.empty_cache()


gpu_scheduler = GPUScheduler()
