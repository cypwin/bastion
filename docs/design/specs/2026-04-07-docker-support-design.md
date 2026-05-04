# BASTION Docker Support — Design Spec

> Phase 3.1 of the production roadmap. Docker image + docker-compose for
> one-command full-stack deployment.

## Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Base image | `python:3.12-slim` multi-stage | No CUDA needed; nvidia-smi injected by NVIDIA Container Toolkit at runtime. ~200MB vs ~2GB |
| Ollama relationship | Separate containers + compose | BASTION image stays focused; compose gives one-command full stack |
| GPU access | NVIDIA Container Toolkit (`--gpus all`) | Standard Docker GPU approach; no CUDA libraries bundled |
| Default config | `broker.example.yaml` (minimal) | GPU auto-detection works in-container; users run `--detect-models` or mount their own `broker.yaml` |
| Startup behavior | Start always, health check gates readiness | Matches bare-metal behavior; compose gets proper orchestration signals |
| CI/ghcr.io | Deferred to Phase 4 | No CI infrastructure yet; get Docker working locally first |

## 1. Dockerfile

Multi-stage build with two stages:

### Build stage

```dockerfile
FROM python:3.12-slim AS builder

WORKDIR /build
COPY pyproject.toml README.md ./
COPY src/ src/

RUN pip install --no-cache-dir --prefix=/install ".[persistence]"
```

### Runtime stage

```dockerfile
FROM python:3.12-slim

# Create non-root user
RUN groupadd -r bastion && useradd -r -g bastion -m bastion

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy default config
COPY config/broker.example.yaml /etc/bastion/broker.yaml

# Switch to non-root user
USER bastion
WORKDIR /home/bastion

# Default ports: 11434 (proxy), 9999 (admin two-port mode)
EXPOSE 11434 9999

# Health check: livez always works if process is up
HEALTHCHECK --interval=10s --timeout=3s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:11434/broker/livez')" || exit 1

ENTRYPOINT ["python", "-m", "bastion"]
CMD ["--config", "/etc/bastion/broker.yaml"]
```

### Design choices

- **Non-root**: Runs as `bastion` user (UID auto-assigned by useradd -r).
  Data dir `/home/bastion/.local/share/bastion/` follows XDG conventions.
- **Persistence built-in**: The build stage installs `.[persistence]` so
  aiosqlite is available. Persistence is enabled via env var in compose
  (not in the default config file).
- **No curl**: Health check uses Python's urllib to avoid installing curl in
  the slim image.
- **Default config**: Copies `broker.example.yaml` to `/etc/bastion/broker.yaml`
  (in the config search path). Has `models: {}` and `total_vram_gb: 0`
  (auto-detect) — works out of the box. Users override via env vars, volume
  mount, or `docker exec bastion --detect-models`.
- **CMD vs ENTRYPOINT**: ENTRYPOINT is `python -m bastion`; CMD provides
  default args (`--config /etc/bastion/broker.yaml`). Users can override CMD
  to pass different flags: `docker run bastion --port 8080`.
- **nvidia-smi availability**: The image does NOT bundle nvidia-smi or CUDA.
  The NVIDIA Container Toolkit injects `/usr/bin/nvidia-smi` and the NVIDIA
  driver libraries into the container at runtime when `--gpus all` (or the
  compose `deploy.resources.reservations.devices` stanza) is used. BASTION's
  `health.py` already handles the `FileNotFoundError` gracefully when
  nvidia-smi is absent.
- **Start period**: 15s gives BASTION time to connect to Ollama and run
  migrations if persistence is enabled.

### Expected image size

~200MB (python:3.12-slim base ~130MB + BASTION deps ~70MB).

## 2. docker-compose.yml

```yaml
services:
  ollama:
    image: ollama/ollama
    restart: unless-stopped
    ports:
      - "11435:11434"
    volumes:
      - ollama_data:/root/.ollama
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu]
    healthcheck:
      test: ["CMD-SHELL", "curl -sf http://localhost:11434/api/tags || exit 1"]
      interval: 10s
      timeout: 5s
      start_period: 30s
      retries: 5

  bastion:
    build: .
    restart: unless-stopped
    ports:
      - "11434:11434"
    environment:
      BASTION_OLLAMA_HOST: ollama
      BASTION_OLLAMA_PORT: "11434"
      BASTION_PERSISTENCE_ENABLED: "true"
    volumes:
      - bastion_data:/home/bastion/.local/share/bastion
    depends_on:
      ollama:
        condition: service_healthy
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu]

volumes:
  ollama_data:
  bastion_data:
```

### Design choices

- **Ollama on host 11435**: Maps Ollama's internal 11434 to host 11435, so
  BASTION owns the standard 11434 port on the host — transparent to clients.
- **BASTION connects to `ollama:11434`**: Uses Docker network DNS hostname,
  not the host port mapping. The env vars override the config file's
  `127.0.0.1:11435` default — this is critical since localhost in a container
  is the container itself, not the host.
- **`depends_on: condition: service_healthy`**: BASTION waits for Ollama's
  health check before starting. BASTION itself starts gracefully even if
  Ollama isn't ready (existing resilience), but this avoids noisy warnings.
- **Both containers get GPU**: BASTION needs nvidia-smi for GPU monitoring;
  Ollama needs GPU for inference. The NVIDIA Container Toolkit handles device
  sharing between containers.
- **Persistence enabled by default**: In Docker, state loss on restart is
  the common case — persistence makes it durable via the `bastion_data` volume.
  The `[persistence]` extra is already installed in the image.
- **Volume mounts**: `ollama_data` persists models; `bastion_data` persists
  audit logs, SQLite DB, VRAM journal.
- **Ollama healthcheck uses CMD-SHELL**: The `ollama/ollama` image includes
  curl. Using `CMD-SHELL` with `|| exit 1` for proper exit code handling.

## 3. .dockerignore

```
.git
.github
.idea
.vscode
__pycache__
*.pyc
*.pyo
tests/
docs/
reference/
_archive/
systemd/
*.egg-info
dist/
build/
.ruff_cache
.mypy_cache
.pytest_cache
to_del_*
CLAUDE.md
CLAUDE.local.md
ARCHIVE/
.claude/
```

Keeps the build context small and fast. Tests and docs not needed in the image.

## 4. Configuration in Docker

All configuration via environment variables (already supported in `config.py`):

| Env Var | Purpose | Default in compose |
|---------|---------|-------------------|
| `BASTION_OLLAMA_HOST` | Ollama hostname | `ollama` |
| `BASTION_OLLAMA_PORT` | Ollama port | `11434` |
| `BASTION_PORT` | BASTION listen port | `11434` |
| `BASTION_PERSISTENCE_ENABLED` | Enable SQLite persistence | `true` |
| `BASTION_PERSISTENCE_DB_PATH` | Custom DB path | (auto: XDG data dir) |
| `BASTION_AUTH_ENABLED` | Enable auth | `false` |
| `BASTION_API_KEYS` | Comma-separated keys | (empty) |

Users who need full config (models section, scheduler tuning) volume-mount
a `broker.yaml`:

```yaml
volumes:
  - ./my-broker.yaml:/etc/bastion/broker.yaml:ro
```

## 5. File Impact Summary

### Created

| File | Purpose |
|------|---------|
| `Dockerfile` | Multi-stage build for BASTION image |
| `docker-compose.yml` | BASTION + Ollama full stack |
| `.dockerignore` | Build context exclusions |

### Not touched

All existing source files. No code changes needed — Docker uses existing
env var overrides and config search paths.

## 6. Testing Strategy

Manual verification (no automated Docker tests):

1. `docker build -t bastion .` — builds without errors
2. `docker run --rm bastion --help` — prints CLI help
3. `docker compose up` — both services start, BASTION connects to Ollama
4. Health check passes after Ollama is reachable
5. Proxy a request: `curl http://localhost:11434/api/tags` returns Ollama models
6. Verify persistence: restart BASTION container, check audit log/DB survived

Automated Docker CI deferred to Phase 4 (release automation).
