"""BASTION — Batch Affinity Scheduler for Throttled Inference on Ollama Networks.

A system-wide GPU/LLM inference broker that sits between ALL Ollama clients
and the Ollama server. Provides model-affinity scheduling, VRAM budget
enforcement, priority queuing with aging, and GPU health gating.

Architecture:
  Layer 1: Transparent Ollama HTTP proxy (port 11434 → 11435)
  Layer 2: Admin API (/broker/*)
  Layer 3: A2A agent interface (/.well-known/agent-card.json)
"""

__version__ = "0.4.0"
