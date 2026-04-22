# Getting Started

Step-by-step guide to install and run BASTION.

## 1. System Requirements

- **Linux** (Ubuntu 22.04+, Fedora 38+, Arch, or equivalent)
- **Python 3.11** or newer
- **NVIDIA GPU** with proprietary drivers installed
- **nvidia-smi** responding (test: `nvidia-smi`)
- **2 GB free disk space** (for models)

## 2. Install Ollama

Download and install Ollama from [ollama.com/download](https://ollama.com/download):

```bash
curl -fsSL https://ollama.com/install.sh | sh
```

Verify the installation:

```bash
ollama --version
```

Expected output:

```
ollama version 0.x.x
```

If this fails, see [Troubleshooting: Ollama Connection Issues](troubleshooting.md#ollama-connection-issues).

## 3. Move Ollama to Port 11435

BASTION needs to claim port 11434 (the standard Ollama port) so existing clients connect through it transparently. Ollama must move to port 11435.

### Option A: systemd override (recommended)

```bash
sudo mkdir -p /etc/systemd/system/ollama.service.d/
sudo tee /etc/systemd/system/ollama.service.d/override.conf > /dev/null << 'EOF'
[Service]
Environment="OLLAMA_HOST=127.0.0.1:11435"
EOF
sudo systemctl daemon-reload
sudo systemctl restart ollama
```

### Option B: Manual

```bash
OLLAMA_HOST=127.0.0.1:11435 ollama serve
```

### Verify

```bash
curl http://localhost:11435
```

Expected output:

```
Ollama is running
```

If this fails, see [Troubleshooting: Ollama Connection Issues](troubleshooting.md#ollama-connection-issues).

## 4. Pull a Model

```bash
ollama pull llama3.1:8b
```

Verify it is installed:

```bash
ollama list
```

Expected output:

```
NAME            ID           SIZE    MODIFIED
llama3.1:8b     ...          4.7 GB  ...
```

Browse available models at [ollama.com/library](https://ollama.com/library).

## 5. Install BASTION

```bash
git clone https://github.com/cyprian-sw/bastion.git
cd bastion
pip install -e ".[dev]"
```

Verify the installation:

```bash
python -m bastion --help
```

## 6. Generate Configuration

Generate a starter config file with auto-detected GPU values:

```bash
bastion --init-config
```

This creates `~/.config/bastion/broker.yaml` with sensible defaults for your hardware.

Next, discover your installed Ollama models:

```bash
bastion --detect-models
```

This prints a YAML `models:` section. Copy and paste it into your `broker.yaml` to register your models with accurate VRAM estimates.

## 7. Validate Your Setup

Run the pre-flight validator to check that everything is configured correctly:

```bash
bastion --validate
```

Expected output:

```
BASTION Pre-flight Check
========================

[PASS] Python version: 3.11.x
[PASS] NVIDIA GPU: NVIDIA GeForce RTX 4090, 24576 MB VRAM, driver 565.x
[PASS] GPU profile: RTX 4090 -- swap limit 5/min, headroom 6GB, thermal 83C
[PASS] Ollama: reachable on 127.0.0.1:11435
[PASS] Installed models: 3 model(s): llama3.1:8b, mistral:7b, qwen3:1.7b
[PASS] Port: 11434: available
[PASS] Config: ~/.config/bastion/broker.yaml valid
[PASS] Permissions: GPU device nodes accessible

Result: 8 passed, 0 warning(s), 0 failed
```

If any check fails, see [Troubleshooting](troubleshooting.md) for specific fixes.

## 8. Start BASTION

```bash
# With default config
bastion

# With a specific config file
bastion --config path/to/broker.yaml

# Two-port mode (proxy on :11434, admin on :9999)
bastion --admin-port 9999
```

You should see:

```
INFO bastion: Starting BASTION on 0.0.0.0:11434 -> Ollama at 127.0.0.1:11435
```

### As a systemd service

```bash
sudo cp systemd/bastion.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now bastion
```

## 9. Verify It Works

```bash
# Should return "Ollama is running" (proxied through BASTION)
curl http://localhost:11434

# Check broker status
curl http://localhost:11434/broker/status | python -m json.tool

# Use Ollama normally -- everything is transparent
ollama run llama3.1:8b "Hello, world!"
```

## What's Next?

- [Configuration Guide](configuration.md) -- tune for your hardware
- [Hardware Guide](hardware-guide.md) -- check GPU compatibility and VRAM requirements
- [Operations Guide](operations.md) -- monitoring, restart, day-2 management
- [Troubleshooting](troubleshooting.md) -- common issues and fixes
- [Security Guide](security.md) -- authentication, TLS, network isolation
