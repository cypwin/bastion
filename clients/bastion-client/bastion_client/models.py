"""Pydantic models for BASTION client requests and responses."""
from __future__ import annotations

from typing import Any, Dict, Optional

from pydantic import BaseModel


class IntentRequest(BaseModel):
    """Request to declare an upcoming model sequence for scheduler optimization."""

    profile: Optional[str] = None
    model_sequence: Optional[list[str]] = None
    estimated_requests: int = 10
    client_id: str = "anonymous"


class IntentResponse(BaseModel):
    """Response from declaring an intent."""

    intent_id: str
    resolved_priority: str
    model_sequence: list[str]
    estimated_requests: int
    status: str


class VRAMInfo(BaseModel):
    """GPU/VRAM status information from BASTION's /broker/status endpoint."""

    total_vram_gb: float = 0.0
    used_vram_gb: float = 0.0
    free_vram_gb: float = 0.0
    loaded_models: list[str] = []
    utilization_pct: float = 0.0


class InferenceResult(BaseModel):
    """Parsed result from a non-streaming inference request."""

    model: str
    response: str
    done: bool = False
    total_duration: Optional[int] = None
    eval_count: Optional[int] = None
    raw: Dict[str, Any] = {}
