#!/bin/bash
# BASTION Dashboard Launcher
# Ensures GPU, Ollama, and BASTION are ready, then launches the TUI dashboard.
# Cleanup: on exit, stops BASTION if we started it (Ollama keeps running).

set -euo pipefail

# Resolve project directory relative to this script
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

# Prevent duplicate launches via lock file
LOCK_FILE="/tmp/bastion-dashboard.lock"
if [ -f "$LOCK_FILE" ] && kill -0 "$(cat "$LOCK_FILE" 2>/dev/null)" 2>/dev/null; then
    echo "[!!] Dashboard already running (PID $(cat "$LOCK_FILE")). Exiting."
    exit 1
fi
echo $$ > "$LOCK_FILE"
trap 'rm -f "$LOCK_FILE"' EXIT

cd "$PROJECT_DIR" || exit 1

# Source conda
CONDA_BASE="$(conda info --base 2>/dev/null || true)"
if [ -n "$CONDA_BASE" ] && [ -f "$CONDA_BASE/etc/profile.d/conda.sh" ]; then
    source "$CONDA_BASE/etc/profile.d/conda.sh"
elif [ -f "$HOME/miniforge3/etc/profile.d/conda.sh" ]; then
    source "$HOME/miniforge3/etc/profile.d/conda.sh"
elif [ -f "$HOME/miniconda3/etc/profile.d/conda.sh" ]; then
    source "$HOME/miniconda3/etc/profile.d/conda.sh"
elif [ -f "$HOME/anaconda3/etc/profile.d/conda.sh" ]; then
    source "$HOME/anaconda3/etc/profile.d/conda.sh"
else
    echo "[!!] Could not find conda — continuing without activation"
fi

conda activate bastion 2>/dev/null || true

python -c "import textual, httpx" 2>/dev/null || {
    echo "Installing required packages..."
    pip install "textual>=1.0" "httpx>=0.27" -q
}

LOG_DIR="${PROJECT_DIR}/logs"
mkdir -p "$LOG_DIR"

OLLAMA_PORT=11435
BASTION_PORT=11434
STARTED_OLLAMA=""
STARTED_BASTION=""

# ---------------------------------------------------------------------------
# 0. Ensure NVIDIA GPU device nodes exist (may be missing after reboot)
# ---------------------------------------------------------------------------
if ! nvidia-smi >/dev/null 2>&1; then
    echo "[..] GPU device nodes missing — running nvidia-modprobe..."
    if sudo nvidia-modprobe && sudo nvidia-modprobe -u; then
        echo "[ok] GPU device nodes created"
    else
        echo "[!!] nvidia-modprobe failed — GPU features may not work"
        echo "     To fix permanently: sudo systemctl enable --now nvidia-persistenced"
    fi
fi

# ---------------------------------------------------------------------------
# 1. Ensure Ollama is running on port 11435
#    Prefer systemd-managed Ollama if available; fall back to nohup.
# ---------------------------------------------------------------------------
if curl -sf "http://127.0.0.1:${OLLAMA_PORT}/" >/dev/null 2>&1; then
    echo "[ok] Ollama already running on port ${OLLAMA_PORT}"
elif systemctl is-active --quiet ollama 2>/dev/null; then
    # systemd service is running but not responding — might be on wrong port
    echo "[..] Ollama systemd service active but not responding on ${OLLAMA_PORT}"
    echo "     Restarting with port override..."
    sudo systemctl restart ollama
    for i in $(seq 1 30); do
        if curl -sf "http://127.0.0.1:${OLLAMA_PORT}/" >/dev/null 2>&1; then
            echo "[ok] Ollama restarted via systemd on port ${OLLAMA_PORT}"
            break
        fi
        sleep 0.5
    done
elif systemctl is-enabled --quiet ollama 2>/dev/null; then
    # systemd service exists but isn't running — start it
    echo "[..] Starting Ollama via systemd..."
    sudo systemctl start ollama
    for i in $(seq 1 30); do
        if curl -sf "http://127.0.0.1:${OLLAMA_PORT}/" >/dev/null 2>&1; then
            echo "[ok] Ollama started via systemd on port ${OLLAMA_PORT}"
            break
        fi
        sleep 0.5
    done
    if ! curl -sf "http://127.0.0.1:${OLLAMA_PORT}/" >/dev/null 2>&1; then
        echo "[!!] Ollama not responding after 15s — check: journalctl -u ollama"
    fi
else
    # No systemd service — launch manually
    echo "[..] Starting Ollama on port ${OLLAMA_PORT} (no systemd service found)..."
    OLLAMA_HOST="127.0.0.1:${OLLAMA_PORT}" \
    OLLAMA_MAX_LOADED_MODELS=4 \
    OLLAMA_NUM_PARALLEL=1 \
    OLLAMA_GPU_OVERHEAD=2147483648 \
    OLLAMA_FLASH_ATTENTION=1 \
    OLLAMA_KV_CACHE_TYPE=q8_0 \
        nohup ollama serve >> "${LOG_DIR}/ollama-serve.log" 2>&1 &
    STARTED_OLLAMA=$!

    for i in $(seq 1 30); do
        if curl -sf "http://127.0.0.1:${OLLAMA_PORT}/" >/dev/null 2>&1; then
            echo "[ok] Ollama started (PID ${STARTED_OLLAMA})"
            break
        fi
        if ! kill -0 "$STARTED_OLLAMA" 2>/dev/null; then
            echo "[!!] Ollama process exited — check ${LOG_DIR}/ollama-serve.log"
            STARTED_OLLAMA=""
            break
        fi
        sleep 0.5
    done

    if [ -n "$STARTED_OLLAMA" ] && ! curl -sf "http://127.0.0.1:${OLLAMA_PORT}/" >/dev/null 2>&1; then
        echo "[!!] Ollama not responding after 15s — dashboard may show errors"
    fi
fi

# ---------------------------------------------------------------------------
# 2. Ensure BASTION broker is running (proxy on 11434 -> Ollama on 11435)
# ---------------------------------------------------------------------------
if curl -sf "http://127.0.0.1:${BASTION_PORT}/broker/health" >/dev/null 2>&1; then
    echo "[ok] BASTION already running on port ${BASTION_PORT}"
else
    echo "[..] Starting BASTION broker..."
    PYTHONPATH=src python -m bastion --config config/broker.yaml >> "${LOG_DIR}/bastion-broker.log" 2>&1 &
    STARTED_BASTION=$!

    for i in $(seq 1 20); do
        if curl -sf "http://127.0.0.1:${BASTION_PORT}/broker/health" >/dev/null 2>&1; then
            echo "[ok] BASTION started (PID ${STARTED_BASTION})"
            break
        fi
        if ! kill -0 "$STARTED_BASTION" 2>/dev/null; then
            echo "[!!] BASTION process exited — check ${LOG_DIR}/bastion-broker.log"
            STARTED_BASTION=""
            break
        fi
        sleep 0.5
    done

    if [ -n "$STARTED_BASTION" ] && ! curl -sf "http://127.0.0.1:${BASTION_PORT}/broker/health" >/dev/null 2>&1; then
        echo "[!!] BASTION not responding after 10s — dashboard may show errors"
    fi
fi

echo ""

# ---------------------------------------------------------------------------
# 3. Cleanup: stop BASTION when the dashboard exits (Ollama stays running)
# ---------------------------------------------------------------------------
cleanup() {
    if [ -n "${STARTED_BASTION}" ] && kill -0 "${STARTED_BASTION}" 2>/dev/null; then
        echo "Stopping BASTION (PID ${STARTED_BASTION})..."
        kill "${STARTED_BASTION}" 2>/dev/null
        wait "${STARTED_BASTION}" 2>/dev/null || true
    fi
    rm -f "$LOCK_FILE"
}
trap cleanup EXIT

# ---------------------------------------------------------------------------
# 4. Launch the TUI dashboard
# ---------------------------------------------------------------------------
PYTHONPATH=src python -m bastion.dashboard "$@"
