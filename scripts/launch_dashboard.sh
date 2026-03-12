#!/bin/bash
# BASTION Dashboard Launcher
# Ensures Ollama + BASTION are running, then launches the TUI dashboard.
# Cleanup: on exit, stops BASTION if we started it (Ollama keeps running).

# Resolve project directory relative to this script
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

# Change to project directory
cd "$PROJECT_DIR" || exit 1

# Source conda (auto-detect from current Python or fall back to common locations)
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

# Activate phenotype environment
conda activate phenotype 2>/dev/null || true

# Check and install dependencies if needed
python -c "import textual, httpx" 2>/dev/null || {
    echo "Installing required packages..."
    pip install "textual>=1.0" "httpx>=0.27" -q
}

# Log directory for service output
LOG_DIR="${PROJECT_DIR}/logs"
mkdir -p "$LOG_DIR"

# ---------------------------------------------------------------------------
# 1. Ensure Ollama is running (on port 11435 for BASTION proxy setup)
# ---------------------------------------------------------------------------
OLLAMA_PORT=11435
BASTION_PORT=11434
STARTED_OLLAMA=""
STARTED_BASTION=""

if curl -sf "http://127.0.0.1:${OLLAMA_PORT}/" >/dev/null 2>&1; then
    echo "[ok] Ollama already running on port ${OLLAMA_PORT}"
else
    echo "[..] Starting Ollama on port ${OLLAMA_PORT}..."
    OLLAMA_HOST="127.0.0.1:${OLLAMA_PORT}" \
    OLLAMA_MAX_LOADED_MODELS=4 \
    OLLAMA_NUM_PARALLEL=1 \
    OLLAMA_GPU_OVERHEAD=2147483648 \
    OLLAMA_FLASH_ATTENTION=1 \
    OLLAMA_KV_CACHE_TYPE=q8_0 \
        nohup ollama serve >> "${LOG_DIR}/ollama-serve.log" 2>&1 &
    STARTED_OLLAMA=$!

    # Wait up to 15 seconds for Ollama to respond
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

    # Final check — warn but don't abort
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

    # Wait up to 10 seconds for BASTION to respond
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

    # Final check — warn but don't abort
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
}
trap cleanup EXIT

# ---------------------------------------------------------------------------
# 4. Launch the TUI dashboard
# ---------------------------------------------------------------------------
PYTHONPATH=src python -m bastion.dashboard "$@"
