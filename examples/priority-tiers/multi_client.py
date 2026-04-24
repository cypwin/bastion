"""Priority tiers demo — shows BASTION scheduling order.

Launches three concurrent clients at different priority tiers.
Interactive requests are served before pipeline and background,
even when submitted at the same time.

Prerequisites:
    # Until bastion-client is published to PyPI, install from source:
    pip install ./clients/bastion-client/

Make sure BASTION is running on localhost:11434 and a model is available
(e.g., ollama pull llama3.1:8b).
"""
from __future__ import annotations

import asyncio
import time

from bastion_client import BastionClient

MODEL = "llama3.1:8b"
PROMPT = "Reply with exactly one word: hello."


async def send_request(name: str, tier: str, start_time: float) -> None:
    """Send a single inference request and report timing."""
    async with BastionClient() as client:
        print(f"[{name}] Submitting request (tier={tier})...")
        result = await client.infer(MODEL, PROMPT, tier=tier)
        elapsed = time.monotonic() - start_time
        response = result["response"].strip()[:80]
        print(f"[{name}] Completed in {elapsed:.1f}s (tier={tier}): {response}")


async def main() -> None:
    print("Priority Tiers Demo")
    print("=" * 50)
    print()
    print("Submitting 3 concurrent requests at different priorities.")
    print("Watch the completion order — interactive should finish first.")
    print()

    start = time.monotonic()

    # Launch all three concurrently.
    # BASTION's scheduler serves higher priority tiers first.
    await asyncio.gather(
        send_request("Background ", "background", start),    # priority 10
        send_request("Pipeline   ", "pipeline", start),      # priority 25
        send_request("Interactive", "interactive", start),    # priority 100
    )

    total = time.monotonic() - start
    print()
    print(f"All requests completed in {total:.1f}s")
    print()
    print("If BASTION had a queue backlog, interactive would be served first,")
    print("then pipeline, then background. With an idle broker, all three may")
    print("complete in similar time since there is no contention.")


if __name__ == "__main__":
    asyncio.run(main())
