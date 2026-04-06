"""Model discovery — queries Ollama for installed models and estimates VRAM.

Used by ``bastion --detect-models`` to generate a ``models:`` config section
that users can paste into their ``broker.yaml``.
"""

from __future__ import annotations

import logging
import subprocess

logger = logging.getLogger(__name__)

# Models under this size are likely embeddings
_EMBEDDING_THRESHOLD_GB = 1.0

# Well-known embedding model name fragments
_EMBEDDING_HINTS = {"embed", "bge", "e5", "gte", "nomic"}


def _is_likely_embedding(name: str, size_gb: float) -> bool:
    """Heuristic: is this model probably an embedding model?"""
    name_lower = name.lower()
    return size_gb < _EMBEDDING_THRESHOLD_GB or any(h in name_lower for h in _EMBEDDING_HINTS)


def detect_models(ollama_host: str = "127.0.0.1", ollama_port: int = 11435) -> None:
    """Query Ollama for installed models and print a YAML config section.

    Parameters
    ----------
    ollama_host : str
        Ollama host address.
    ollama_port : int
        Ollama port.
    """
    base_url = f"http://{ollama_host}:{ollama_port}"

    # Try to get model list from Ollama API
    models = _query_ollama_models(base_url)

    if models is None:
        # Ollama not reachable — try CLI fallback
        models = _query_ollama_cli()

    if not models:
        print("No Ollama models found.\n")
        print("Install models from https://ollama.com/library")
        print("  Popular choices:")
        print("    ollama pull llama3.1:8b        # General-purpose, 4.5 GB VRAM")
        print("    ollama pull qwen3:8b           # Fast reasoning, ~5 GB VRAM")
        print("    ollama pull mistral:7b         # Efficient all-rounder, ~4 GB VRAM")
        print("    ollama pull nomic-embed-text   # Embeddings, 0.3 GB VRAM")
        print("    ollama pull codellama:7b       # Code generation, ~4 GB VRAM")
        print("\n  After pulling, run `bastion --detect-models` again.")
        return

    print(f"Found {len(models)} model(s) in Ollama.\n")
    print("# Paste this into your broker.yaml:\n")
    print("models:")

    for m in models:
        name = m["name"]
        size_gb = m["size_gb"]
        is_embed = _is_likely_embedding(name, size_gb)

        print(f'  "{name}":')
        print(f"    vram_gb: {size_gb}")
        print(f"    default_num_ctx: {4096 if not is_embed else 512}")
        if is_embed:
            print("    always_allowed: true   # Embeddings don't count toward VRAM budget")
            print('    tags: ["embedding"]')
        else:
            print('    tags: ["general"]')
        print()

    print("# VRAM values are estimates from model file size.")
    print("# For exact values, load each model and check nvidia-smi.")
    print(f"# Total models: {len(models)}")

    non_embed = [m for m in models if not _is_likely_embedding(m["name"], m["size_gb"])]
    total_vram = sum(m["size_gb"] for m in non_embed)
    print(f"# Total VRAM (non-embedding): {total_vram:.1f} GB")


def _query_ollama_models(base_url: str) -> list[dict] | None:
    """Query Ollama HTTP API for model list."""
    try:
        import httpx

        resp = httpx.get(f"{base_url}/api/tags", timeout=5.0)
        resp.raise_for_status()
        data = resp.json()

        models = []
        for m in data.get("models", []):
            name = m.get("name", "unknown")
            size_bytes = m.get("size", 0)
            size_gb = round(size_bytes / (1024**3), 1)
            models.append({"name": name, "size_gb": size_gb})

        return sorted(models, key=lambda x: x["name"])

    except Exception as e:
        logger.debug("Ollama API query failed: %s", e)
        return None


def _query_ollama_cli() -> list[dict]:
    """Fallback: query models via `ollama list` CLI."""
    try:
        result = subprocess.run(
            ["ollama", "list"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return []

        models = []
        for line in result.stdout.strip().split("\n")[1:]:  # skip header
            parts = line.split()
            if not parts:
                continue
            name = parts[0]
            # Parse size from ollama list output (e.g., "4.7 GB")
            size_gb = _parse_size_from_ollama_list(parts)
            models.append({"name": name, "size_gb": size_gb})

        return sorted(models, key=lambda x: x["name"])

    except FileNotFoundError:
        logger.debug("ollama CLI not found")
        return []
    except Exception as e:
        logger.debug("ollama CLI query failed: %s", e)
        return []


def _parse_size_from_ollama_list(parts: list[str]) -> float:
    """Extract size in GB from ollama list output columns."""
    # ollama list format: NAME  ID  SIZE  MODIFIED
    # SIZE can be "4.7 GB" or "274 MB"
    for i, part in enumerate(parts):
        if part.upper() == "GB" and i > 0:
            try:
                return round(float(parts[i - 1]), 1)
            except ValueError:
                pass
        elif part.upper() == "MB" and i > 0:
            try:
                return round(float(parts[i - 1]) / 1024, 1)
            except ValueError:
                pass
    return 0.0
