"""BASTION client library for broker API integration."""
from __future__ import annotations

from bastion_client.client import BastionClient, SyncBastionClient
from bastion_client.models import InferenceResult, IntentRequest, IntentResponse, VRAMInfo

__all__ = [
    "BastionClient",
    "SyncBastionClient",
    "InferenceResult",
    "IntentRequest",
    "IntentResponse",
    "VRAMInfo",
]
__version__ = "0.2.0"
