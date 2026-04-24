# bastion-client

Python client library for the [BASTION](https://github.com/cia-labs/BASTION) GPU/LLM broker.

## Installation

> **Note:** This package is not yet published to PyPI. To use it, install from source:
> ```
> cd clients/bastion-client && pip install -e .
> ```
> Examples in the top-level `examples/` directory use `httpx` directly to avoid
> this local-install step.

## Quick start

```python
from bastion_client import BastionClient
import asyncio

async def main():
    async with BastionClient() as client:
        result = await client.infer(
            "llama3.1:8b",
            "Explain what a GPU broker does in one sentence.",
            tier="interactive",
        )
        print(result["response"])

asyncio.run(main())
```

## Requirements

- Python 3.11+
- BASTION running on `http://127.0.0.1:11434` (or set `BASTION_URL`)
