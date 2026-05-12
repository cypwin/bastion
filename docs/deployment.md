# Deployment Guide

This guide covers installing and running BASTION in production. Three deployment
methods are supported, from simplest to most isolated:

| Method | Best for | GPU required on host? |
|--------|----------|----------------------|
| [Desktop launcher](#desktop-launcher) | Single-user workstations | Yes |
| [Systemd services](#systemd-services) | Headless servers, always-on | Yes |
| [Docker Compose](#docker-compose) | Isolation, reproducibility | Yes (via NVIDIA Container Toolkit) |

All three methods result in the same architecture: BASTION on port 11434
(standard Ollama port), proxying to Ollama on port 11435. Clients connect to
11434 transparently.

---

## Prerequisites

### NVIDIA GPU and Drivers

BASTION requires an NVIDIA GPU with working drivers. Verify with:

```bash
nvidia-smi
```

If this fails after a reboot, the GPU device nodes (`/dev/nvidia*`) are missing.
Fix immediately:

```bash
sudo nvidia-modprobe && sudo nvidia-modprobe -u
```

To make this permanent (recommended):

```bash
sudo systemctl enable --now nvidia-persistenced
```

> **Why this happens:** The NVIDIA kernel modules load at boot, but device nodes
> require `nvidia-persistenced` or `nvidia-modprobe` to create them. Without
> either, `nvidia-smi` fails, Ollama runs in CPU-only mode, and BASTION's GPU
> monitoring returns nulls.

### Ollama

Install Ollama from [ollama.com](https://ollama.com/download). After
installation, Ollama's systemd service listens on port 11434 by default. BASTION
needs Ollama on port **11435** so it can own the standard port.

### Python

Python 3.11+ is required. Install BASTION and its dependencies:

```bash
pip install -e ".[persistence]"    # From source
# or
pip install bastion[persistence]   # From PyPI (when published)
```

---

## Desktop Launcher

The simplest method. A `.desktop` file launches a terminal with the TUI
dashboard, automatically starting Ollama and BASTION if needed.

### Setup

1. **Move Ollama to port 11435:**

   ```bash
   sudo mkdir -p /etc/systemd/system/ollama.service.d/
   sudo cp systemd/ollama-port-override.conf.example \
           /etc/systemd/system/ollama.service.d/override.conf
   sudo systemctl daemon-reload
   sudo systemctl restart ollama
   ```

2. **Create the bastion group** (for nftables firewall — see
   [Security](#port-lockdown-with-nftables) below):

   ```bash
   sudo groupadd -g 983 bastion 2>/dev/null || true
   sudo usermod -aG bastion $USER
   # Log out and back in for the group to take effect
   ```

3. **Ensure GPU persistence across reboots:**

   ```bash
   sudo systemctl enable --now nvidia-persistenced
   ```

4. **Install the desktop shortcut:**

   ```bash
   cp ~/BASTION/scripts/bastion-dashboard.desktop ~/Desktop/
   # Or for all users:
   cp ~/BASTION/scripts/bastion-dashboard.desktop ~/.local/share/applications/
   ```

5. **(Optional) Passwordless sudo** for fully hands-off launches after reboot:

   ```bash
   echo "$USER ALL=(ALL) NOPASSWD: /usr/bin/nvidia-modprobe, \
   /usr/bin/systemctl start ollama, \
   /usr/bin/systemctl stop ollama, \
   /usr/bin/systemctl restart ollama" \
     | sudo tee /etc/sudoers.d/bastion-launcher
   sudo chmod 0440 /etc/sudoers.d/bastion-launcher
   ```

### What the launcher does

1. Creates GPU device nodes if missing (`nvidia-modprobe`)
2. Detects Ollama: starts/restarts via systemd, or falls back to manual launch
3. Starts BASTION under the `bastion` group (required for nftables access)
4. Launches the Textual TUI dashboard
5. On exit: stops BASTION (Ollama keeps running)

### Troubleshooting the launcher

| Symptom | Cause | Fix |
|---------|-------|-----|
| "Dashboard already running" but nothing visible | Stale lock file from a crash | `rm ${XDG_RUNTIME_DIR}/bastion-dashboard.lock` |
| Terminal opens then closes immediately | `conda activate` or a command failed | Run `bash -x ~/BASTION/scripts/launch_dashboard.sh 2>&1` to see the error |
| Ollama shows "unhealthy" in dashboard | BASTION can't reach Ollama on 11435 | Check nftables (see below) and that Ollama is on 11435: `sg bastion "curl -sf http://127.0.0.1:11435/api/tags"` |
| GPU panels show null values | `nvidia-smi` not working | Run `nvidia-smi` — if it fails, `sudo nvidia-modprobe && sudo nvidia-modprobe -u` |
| Duplicate Ollama instances fighting for port | Clicked launcher multiple times before it finished | `pkill -f "ollama serve"` then wait and relaunch |

---

## Systemd Services

For headless servers or always-on operation. BASTION runs as a systemd service
with watchdog integration, automatic restart, and journal logging.

### Setup

1. **Move Ollama to port 11435** (same as desktop launcher step 1 above).

2. **Create the bastion group and user:**

   ```bash
   sudo groupadd -g 983 bastion 2>/dev/null || true
   sudo usermod -aG bastion $USER
   ```

3. **Install the BASTION service:**

   ```bash
   sudo cp systemd/bastion.service.example /etc/systemd/system/bastion.service
   ```

   Edit `/etc/systemd/system/bastion.service` and replace all placeholders:

   | Placeholder | Example value |
   |-------------|---------------|
   | `<USER>` | `bastion` |
   | `<BASTION_DIR>` | `/opt/bastion` |
   | `<PYTHON_PATH>` | `/opt/bastion/.venv/bin/python` |

4. **Enable GPU persistence:**

   ```bash
   sudo systemctl enable --now nvidia-persistenced
   ```

5. **Start everything:**

   ```bash
   sudo systemctl daemon-reload
   sudo systemctl restart ollama
   sudo systemctl enable --now bastion
   ```

6. **Verify:**

   ```bash
   systemctl status ollama bastion --no-pager
   curl -s http://localhost:11434/broker/status | python3 -m json.tool
   ```

### Logs

```bash
journalctl -u bastion -f          # Live BASTION logs
journalctl -u ollama -f           # Live Ollama logs
journalctl -u bastion --since "1h ago"  # Last hour
```

---

## Docker Compose

For isolated, reproducible deployments. BASTION and Ollama run in separate
containers with GPU access via the NVIDIA Container Toolkit.

### Prerequisites

- **Official Docker** (not snap). The snap Docker package cannot access NVIDIA
  GPU libraries due to confinement. If you have snap Docker:

  ```bash
  sudo snap remove docker
  curl -fsSL https://get.docker.com | sudo sh
  sudo usermod -aG docker $USER
  # Log out and back in
  ```

- **NVIDIA Container Toolkit:**

  ```bash
  # Follow: https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html
  # Then verify:
  nvidia-container-cli info
  ```

  If `nvidia-container-cli` fails with "driver not loaded", ensure GPU device
  nodes exist (`sudo nvidia-modprobe && sudo nvidia-modprobe -u`) and restart
  Docker (`sudo systemctl restart docker`).

- **GPU persistence:**

  ```bash
  sudo systemctl enable --now nvidia-persistenced
  ```

### Start

```bash
cd ~/BASTION
docker compose up --build -d
```

### Verify

```bash
docker compose ps                                    # Both healthy?
curl -s http://localhost:11434/api/tags               # Ollama models via proxy
curl -s http://localhost:11434/broker/status           # BASTION status
```

### Configuration

The Docker image ships with a minimal default config. All tuning is via
environment variables in `docker-compose.yml`:

| Variable | Purpose | Default |
|----------|---------|---------|
| `BASTION_OLLAMA_HOST` | Ollama hostname | `ollama` (Docker DNS) |
| `BASTION_OLLAMA_PORT` | Ollama port | `11434` (internal) |
| `BASTION_PERSISTENCE_ENABLED` | SQLite persistence | `true` |
| `BASTION_AUTH_ENABLED` | Enable API auth | `false` |
| `BASTION_API_KEYS` | Comma-separated keys | (empty) |

For full config (models section, scheduler tuning), volume-mount a broker.yaml:

```yaml
volumes:
  - ./my-broker.yaml:/etc/bastion/broker.yaml:ro
```

### Docker troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `nvidia-container-cli: initialization error: load library failed` | NVIDIA driver not loaded or snap Docker | Ensure `nvidia-smi` works on host, use official Docker (not snap), restart Docker after fixing GPU |
| `address already in use` on port 11435 | Host Ollama service running | `sudo systemctl stop ollama` before `docker compose up` |
| BASTION `PermissionError` on audit log | Volume mounted as root, BASTION runs as non-root | Rebuild image: `docker compose up --build` (Dockerfile creates the data dir with correct ownership) |
| Ollama container "unhealthy" | Health check using curl but curl not in image | Rebuild — current Dockerfile uses `ollama list` for health check |
| Containers die after host reboot | GPU device nodes missing | `sudo systemctl enable --now nvidia-persistenced` |

### Volumes

| Volume | Contents | Safe to delete? |
|--------|----------|----------------|
| `ollama_data` | Downloaded models (~5-30 GB each) | Yes, but models must be re-pulled |
| `bastion_data` | Audit logs, SQLite DB | Yes, but history is lost |

```bash
docker compose down          # Stop containers, keep volumes
docker compose down -v       # Stop and DELETE volumes (data loss!)
```

---

## Security

### Port Lockdown with nftables

By default, any process on the host can bypass BASTION and talk to Ollama
directly on port 11435. To enforce that all traffic goes through BASTION, add
an nftables OUTPUT rule that restricts port 11435 to the `bastion` group
(GID 983):

```bash
# Only processes running as GID 983 (bastion) can connect to Ollama
sudo nft add rule inet filter output tcp dport 11435 skgid != 983 reject with tcp reset
```

> **Impact:** After enabling this rule, `curl http://127.0.0.1:11435/...` from
> your terminal will get "Connection refused" unless you run it as
> `sg bastion "curl ..."`. BASTION, running under the `bastion` group, is
> unaffected.

This is optional but recommended for multi-user systems where you want to
ensure all inference goes through BASTION's queue, rate limiter, and audit log.

### Authentication

Enable API key auth for admin endpoints:

```yaml
auth:
  enabled: true
  api_keys: ["sk-your-secret-key"]
```

Or via environment variable:

```bash
BASTION_AUTH_ENABLED=true
BASTION_API_KEYS=sk-key1,sk-key2
```

Proxy routes (`/api/*`) remain open for Ollama client compatibility.

---

## Post-Install Verification

After any deployment method, verify the full stack:

```bash
# 1. GPU is accessible
nvidia-smi --query-gpu=name,memory.total --format=csv,noheader

# 2. Ollama is on 11435
# (use sg bastion if nftables is enabled)
curl -sf http://127.0.0.1:11435/api/tags | python3 -m json.tool

# 3. BASTION proxies correctly on 11434
curl -sf http://localhost:11434/api/tags | python3 -m json.tool

# 4. Admin API works
curl -sf http://localhost:11434/broker/status | python3 -m json.tool

# 5. Watchdog sees everything healthy
curl -sf http://localhost:11434/broker/watchdog | python3 -m json.tool
# Expected: ollama_state: "healthy", gpu_state: "responsive"
```

---

## Common Issues

### nvidia-smi fails after reboot

**Symptom:** `nvidia-smi` returns "couldn't communicate with the NVIDIA driver".
GPU panels in dashboard show null. Ollama runs in CPU-only mode.

**Cause:** NVIDIA kernel modules are loaded but `/dev/nvidia*` device nodes
don't exist. This happens when `nvidia-persistenced` isn't enabled.

**Fix:**
```bash
sudo nvidia-modprobe && sudo nvidia-modprobe -u   # Immediate fix
sudo systemctl enable --now nvidia-persistenced    # Permanent fix
```

### BASTION shows "draining" state and Ollama "unhealthy"

**Symptom:** Dashboard shows Ollama as unhealthy, BASTION state is "draining",
circuit breaker is open.

**Cause:** BASTION cannot reach Ollama on port 11435. Common reasons:
- Ollama isn't running
- Ollama is on the wrong port (11434 instead of 11435)
- nftables blocks the connection (BASTION not running as `bastion` group)

**Diagnosis:**
```bash
# Is Ollama running?
pgrep -a ollama

# What port?
ss -tlnp | grep -E '11434|11435'

# Can bastion group reach it?
sg bastion "curl -sf http://127.0.0.1:11435/api/tags" | head -1

# Is BASTION in the bastion group?
ps -o pid,group,cmd -p $(pgrep -f "python -m bastion")
```

**Fix:** Restart Ollama on the correct port, ensure BASTION runs under
the `bastion` group:
```bash
sudo systemctl restart ollama
pkill -f "python -m bastion"
sg bastion -c "PYTHONPATH=src python -m bastion --config config/broker.yaml &"
```

### Snap Docker + NVIDIA GPU doesn't work

**Symptom:** `docker compose up` fails with `nvidia-container-cli: initialization
error: load library failed: libnvidia-ml.so.1`.

**Cause:** Snap's confinement sandbox prevents the NVIDIA Container Toolkit from
accessing host GPU libraries. This is a known incompatibility.

**Fix:** Switch to official Docker:
```bash
sudo snap remove docker
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER
# Log out and back in
```

### Docker port conflict with host Ollama

**Symptom:** `docker compose up` fails with "address already in use" on port
11435.

**Cause:** The host's systemd Ollama service is running on 11435, and Docker
tries to map the Ollama container to the same port.

**Fix:** Stop the host Ollama before using Docker:
```bash
sudo systemctl stop ollama
docker compose up -d
```

Docker and systemd deployments are mutually exclusive — use one or the other,
not both.
