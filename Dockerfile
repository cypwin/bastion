# ── Build stage ────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /build
COPY pyproject.toml README.md ./
COPY src/ src/

# Install BASTION with persistence extra (aiosqlite)
RUN pip install --no-cache-dir --prefix=/install ".[persistence]"

# ── Runtime stage ──────────────────────────────────────────────────
FROM python:3.12-slim

# Create non-root user
RUN groupadd -r bastion && useradd -r -g bastion -m bastion

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy default config (in config search path: /etc/bastion/broker.yaml)
COPY config/broker.example.yaml /etc/bastion/broker.yaml

# Create data directory with correct ownership (volume mount point)
RUN mkdir -p /home/bastion/.local/share/bastion && \
    chown -R bastion:bastion /home/bastion/.local

# Switch to non-root user
USER bastion
WORKDIR /home/bastion

# Default ports: 11434 (proxy), 9999 (admin two-port mode)
EXPOSE 11434 9999

# Health check using stdlib (no curl in slim image)
HEALTHCHECK --interval=10s --timeout=3s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:11434/broker/livez')" || exit 1

ENTRYPOINT ["python", "-m", "bastion"]
CMD ["--config", "/etc/bastion/broker.yaml"]
