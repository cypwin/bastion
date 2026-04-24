"""Minimal BASTION client example using httpx.

Shows how to call BASTION's Ollama-compatible /api/chat endpoint
without installing any BASTION-specific client package.
"""
from __future__ import annotations

import os

import httpx

BASTION_URL = os.environ.get("BASTION_URL", "http://127.0.0.1:11434")
# If auth is enabled, set BASTION_API_KEY in the environment or edit here.
API_KEY: str | None = os.environ.get("BASTION_API_KEY")


def chat(model: str, prompt: str) -> str:
    headers: dict[str, str] = {}
    if API_KEY:
        headers["Authorization"] = f"Bearer {API_KEY}"
    with httpx.Client(timeout=120.0) as client:
        resp = client.post(
            f"{BASTION_URL}/api/chat",
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
            },
            headers=headers,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["message"]["content"]


if __name__ == "__main__":
    out = chat("llama3.1:8b", "Say hi in one sentence.")
    print(out)
