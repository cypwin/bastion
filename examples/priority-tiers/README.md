# Priority Tiers Demo

Demonstrates BASTION's priority scheduling by sending concurrent requests
at different tiers and observing completion order.

## Priority Tiers

| Tier          | Priority | Use case                        |
|---------------|----------|---------------------------------|
| `interactive` | 100      | User-facing (ollama run, chat)  |
| `agent`       | 50       | AI agents                       |
| `pipeline`    | 25       | Batch data pipelines            |
| `background`  | 10       | Background jobs, indexing       |

Higher priority requests are dequeued first. Requests that wait in the queue
gain priority over time (aging rate: +2 points/second) to prevent starvation.

## Prerequisites

- BASTION running on `localhost:11434`
- A model available (e.g., `ollama pull llama3.1:8b`)

## Install

```bash
pip install bastion-client
```

## Run

```bash
python multi_client.py
```

## What to expect

The script submits three requests simultaneously:
- One at `background` priority (10)
- One at `pipeline` priority (25)
- One at `interactive` priority (100)

When BASTION has queue pressure (multiple models competing for VRAM),
interactive requests are served first. On an idle broker with one model
already loaded, all three may complete at similar times since there is
no contention.

To see clear scheduling differences, try running this while other clients
are also sending requests, or use a model that takes longer to load.

## How priority is set

Clients set priority via the `X-Broker-Priority` HTTP header. The
`bastion-client` library handles this automatically based on the `tier`
parameter:

```python
result = await client.infer("llama3.1:8b", "Hello", tier="interactive")
```

Without `bastion-client`, set the header directly:

```bash
curl http://localhost:11434/api/generate \
  -H "X-Broker-Priority: interactive" \
  -d '{"model": "llama3.1:8b", "prompt": "Hello", "stream": false}'
```
