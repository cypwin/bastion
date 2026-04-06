"""GPU backend abstraction for BASTION.

Provides a pluggable GPU monitoring layer.  Auto-detects the available
backend at startup:

- :class:`NvidiaBackend` — uses ``nvidia-smi`` (Linux + NVIDIA drivers)
- :class:`StubBackend` — no-op fallback (no GPU or unsupported vendor)

Usage::

    from bastion.gpu import get_backend
    gpu = get_backend()
    status = await gpu.query_status()
"""

from __future__ import annotations

import logging
import shutil

from bastion.gpu.base import GPUBackend
from bastion.gpu.nvidia import NvidiaBackend
from bastion.gpu.stub import StubBackend

logger = logging.getLogger(__name__)

__all__ = ["GPUBackend", "NvidiaBackend", "StubBackend", "get_backend"]

_backend: GPUBackend | None = None


def detect_backend() -> GPUBackend:
    """Auto-detect and return the best available GPU backend."""
    if shutil.which("nvidia-smi"):
        logger.info("GPU backend: NVIDIA (nvidia-smi found)")
        return NvidiaBackend()

    logger.info("GPU backend: stub (no supported GPU tools found)")
    return StubBackend()


def get_backend() -> GPUBackend:
    """Return the singleton GPU backend, detecting on first call."""
    global _backend
    if _backend is None:
        _backend = detect_backend()
    return _backend


def set_backend(backend: GPUBackend) -> None:
    """Override the GPU backend (useful for testing)."""
    global _backend
    _backend = backend
