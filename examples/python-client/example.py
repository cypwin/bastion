"""BASTION Python client — basic usage example.

Prerequisites:
    # Until bastion-client is published to PyPI, install from source:
    pip install ./clients/bastion-client/

    Then run from anywhere (BASTION_URL env var optional, defaults to
    http://127.0.0.1:11434).

Make sure BASTION is running and a model is available
(e.g., ollama pull llama3.1:8b).
"""
from __future__ import annotations

import asyncio

from bastion_client import BastionClient


async def main() -> None:
    async with BastionClient() as client:
        # Check GPU/VRAM status
        vram = await client.check_vram()
        print(f"VRAM: {vram.used_vram_gb:.1f}/{vram.total_vram_gb:.1f} GB")
        print(f"Utilization: {vram.utilization_pct:.0f}%")
        print(f"Loaded models: {vram.loaded_models}")
        print()

        # Run inference with interactive priority
        print("Sending inference request...")
        result = await client.infer(
            "llama3.1:8b",
            "Explain what a GPU broker does in one sentence.",
            tier="interactive",
        )
        print(f"Response: {result['response']}")


if __name__ == "__main__":
    asyncio.run(main())
