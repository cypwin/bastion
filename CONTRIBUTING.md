# Contributing to BASTION

## Prerequisites

- **Python 3.11+** (3.12 recommended)
- **Ollama** installed and running (for integration testing)
- **NVIDIA GPU** with nvidia-smi (optional; BASTION gracefully degrades without a GPU)
- **Git** for version control

## Development Setup

```bash
# Clone the repository
git clone https://github.com/cyprian-w/bastion.git
cd bastion

# Install in editable mode with dev dependencies
pip install -e ".[dev]"

# Verify installation
python -m bastion --help
```

### Optional Dependencies

```bash
# Prometheus metrics support
pip install -e ".[metrics]"

# TUI dashboard (Textual)
pip install -e ".[dashboard]"

# All optional features
pip install -e ".[dev,metrics,dashboard]"
```

## Code Conventions

### Type Hints

All functions require type annotations. No exceptions.

```python
def process_request(model: str, timeout: float = 30.0) -> dict[str, Any]:
    ...
```

### Future Annotations

Every `.py` file must include this as the first import:

```python
from __future__ import annotations
```

This enables PEP 604 union syntax (`str | None`) and deferred evaluation of type hints.

### Import Order

Imports are organized in three groups, separated by blank lines:

1. Standard library
2. Third-party packages
3. Local imports (`bastion.*`)

```python
from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx
from fastapi import FastAPI

from bastion.models import BrokerConfig, QueuedRequest
from bastion.queue import AffinityQueue
```

### Line Length

100 characters maximum (enforced by ruff).

### Naming

- Classes: `PascalCase`
- Functions and variables: `snake_case`
- Constants: `UPPER_SNAKE_CASE`
- Private attributes: `_leading_underscore`

## Running Tests

```bash
# Run the full test suite
python -m pytest tests/ -v

# Run a specific test file
python -m pytest tests/test_proxy.py -v

# Run with coverage
python -m pytest tests/ -v --cov=bastion --cov-report=term-missing
```

Tests do not require a running Ollama instance or GPU. All external dependencies are mocked.

## Linting and Formatting

```bash
# Lint
ruff check src/ tests/

# Auto-fix
ruff check --fix src/ tests/

# Format
ruff format src/ tests/
```

## Commit Message Format

Use conventional commit format with a scope:

```
type(scope): description
```

**Types:**
- `feat` -- New feature
- `fix` -- Bug fix
- `refactor` -- Code restructuring without behavior change
- `test` -- Adding or updating tests
- `docs` -- Documentation changes
- `chore` -- Build, CI, or tooling changes

**Examples:**
```
feat(scheduler): add residency-aware cooldown skip
fix(proxy): handle empty request body in streaming mode
test(queue): add priority aging edge cases
docs(api): update lease management examples
```

## Project Structure

```
src/bastion/
├── __init__.py       # Package init, version
├── __main__.py       # CLI entry point (argparse + uvicorn)
├── a2a.py            # A2A protocol handler
├── audit.py          # Structured JSONL audit logging
├── auth.py           # API key + bearer token auth
├── circuitbreaker.py # Three-state circuit breaker
├── config.py         # YAML config loader
├── dashboard.py      # Textual TUI dashboard
├── health.py         # GPU health queries (nvidia-smi)
├── metrics.py        # Prometheus metrics (no-op fallback)
├── middleware.py      # FastAPI request metrics middleware
├── models.py         # Pydantic models (config, queue, GPU, A2A)
├── proxy.py          # Transparent Ollama proxy
├── queue.py          # AffinityQueue (per-model sub-queues)
├── ratelimit.py      # Per-IP rate limiting
├── scheduler.py      # Scheduling loop (model swaps, cooldown)
├── server.py         # FastAPI app factory + admin routes
├── taskstore.py      # Dual-store A2A task store
├── telemetry.py      # OpenTelemetry tracing (no-op fallback)
├── vram.py           # VRAM tracker (Ollama + nvidia-smi fusion)
└── watchdog.py       # Ollama process monitor + sd_notify

config/               # Configuration files
tests/                # Test suite
docs/                 # Documentation
```

## Adding a New Module

1. Create the file in `src/bastion/` with `from __future__ import annotations` as the first import.

2. Add type hints to all functions.

3. If the module has optional dependencies, use try/except imports with no-op fallbacks (see `metrics.py` for the pattern).

4. Add tests in `tests/test_<module>.py`.

5. If the module adds new configuration options, add the corresponding Pydantic model fields in `models.py` and update `config/broker.example.yaml`.

6. Update `docs/architecture.md` if the module introduces a new architectural concept.

## Design Principles

- **In-memory state only**: No external databases. All state (queues, tasks, leases) lives in memory. This keeps BASTION simple and fast.
- **Graceful degradation**: Optional dependencies (prometheus-client, textual, opentelemetry) use no-op fallbacks. BASTION runs with zero optional dependencies installed.
- **Transparent proxy**: Existing Ollama clients must work without modification. BASTION is invisible to clients except for improved scheduling behavior.
- **Archive, never delete**: When replacing files, move the old version to `_archive/` rather than deleting it.
