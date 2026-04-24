# Python Client Example

Shows how to use the `bastion-client` library to interact with BASTION
programmatically.

## Prerequisites

- BASTION running on `localhost:11434`
- A model available (e.g., `ollama pull llama3.1:8b`)

## Install

`bastion-client` is not yet published to PyPI. Install from source:

```bash
pip install ../../clients/bastion-client/
```

(Once published, this becomes `pip install bastion-client`.)

## Run

```bash
python example.py
```

## What it does

1. Connects to BASTION on `localhost:11434`
2. Queries GPU/VRAM status via `/broker/status`
3. Sends an inference request with `interactive` priority tier
4. Prints the response

## Client API

```python
async with BastionClient(base_url="http://localhost:11434") as client:
    # Check VRAM status
    vram = await client.check_vram()

    # Declare intent (helps the scheduler plan ahead)
    await client.declare_intent(
        model_sequence=["llama3.1:8b", "nomic-embed-text"],
        estimated_requests=20,
    )

    # Run inference with a priority tier
    result = await client.infer("llama3.1:8b", "Hello", tier="interactive")
```

Priority tiers: `interactive` (100), `agent` (50), `pipeline` (25), `background` (10).
