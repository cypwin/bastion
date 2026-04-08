# Basic Proxy Quickstart

BASTION is a transparent proxy — you install it, move Ollama to a different port,
and everything else works exactly as before. BASTION sits on the standard Ollama
port (11434) and forwards requests to Ollama on 11435.

## 1. Install BASTION

```bash
pip install bastion
```

## 2. Move Ollama to port 11435

**Option A: systemd override (recommended for Linux)**

```bash
sudo mkdir -p /etc/systemd/system/ollama.service.d
sudo tee /etc/systemd/system/ollama.service.d/port.conf <<EOF
[Service]
Environment="OLLAMA_HOST=127.0.0.1:11435"
EOF
sudo systemctl daemon-reload
sudo systemctl restart ollama
```

**Option B: environment variable (any platform)**

```bash
OLLAMA_HOST=127.0.0.1:11435 ollama serve
```

## 3. Start BASTION

```bash
bastion
# or: python -m bastion
```

BASTION listens on port 11434 (Ollama's default) and proxies to 11435.

## 4. Use Ollama normally

Everything goes through BASTION transparently:

```bash
ollama run llama3.1:8b "Hello, world!"
ollama list
ollama pull nomic-embed-text
```

## 5. Check broker status

```bash
curl http://localhost:11434/broker/status | jq .
```

This shows loaded models, VRAM usage, queue depth, and scheduler state.
