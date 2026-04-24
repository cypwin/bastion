"""Priority tiers demo — shows BASTION scheduling order.

Launches three concurrent requests at different priority tiers using
httpx directly (no bastion-client package required). Interactive
requests are served before pipeline and background, even when
submitted at the same time.

Make sure BASTION is running on localhost:11434 and a model is available
(e.g., ollama pull llama3.1:8b).
"""
from __future__ import annotations

import asyncio
import os
import time

import httpx

BASTION_URL = os.environ.get("BASTION_URL", "http://127.0.0.1:11434")
API_KEY: str | None = os.environ.get("BASTION_API_KEY")
MODEL = "llama3.1:8b"
PROMPT = "Reply with exactly one word: hello."


async def send_request(name: str, tier: str, start_time: float) -> None:
    """Send a single inference request at the given priority tier."""
    headers = {"X-Broker-Priority": tier}
    if API_KEY:
        headers["Authorization"] = f"Bearer {API_KEY}"
    async with httpx.AsyncClient(timeout=120.0) as client:
        print(f"[{name}] Submitting request (tier={tier})...")
        resp = await client.post(
            f"{BASTION_URL}/api/chat",
            json={
                "model": MODEL,
                "messages": [{"role": "user", "content": PROMPT}],
                "stream": False,
            },
            headers=headers,
        )
        resp.raise_for_status()
        data = resp.json()
        elapsed = time.monotonic() - start_time
        response = data["message"]["content"].strip()[:80]
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
