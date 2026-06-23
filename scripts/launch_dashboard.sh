#!/bin/bash
# BASTION Dashboard Launcher
# Ensures GPU, Ollama, and BASTION are ready, then launches the TUI dashboard.
# Cleanup: on exit, stops BASTION if we started it (Ollama keeps running).
#
# Env vars:
#   BASTION_CONDA_ENV        conda env to activate (else any env with the deps
#                            is auto-detected). Useful for GUI .desktop launches,
#                            which do not load your shell's conda init.
#   BASTION_LAUNCH_SELFTEST  if set, resolve Python + verify deps, then exit 0
#                            before touching the GPU/Ollama/broker (used by tests).

# Note: no set -euo here — conda activate breaks with set -u,
# and many commands (nvidia-smi, curl, systemctl) intentionally return non-zero.

# Resolve project directory relative to this script
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

# Prevent duplicate launches via lock file
LOCK_FILE="${XDG_RUNTIME_DIR:-/tmp}/bastion-dashboard.lock"
if [ -f "$LOCK_FILE" ]; then
    OLD_PID="$(cat "$LOCK_FILE" 2>/dev/null)"
    # Check if process is alive AND is actually a bastion dashboard
    if [ -n "$OLD_PID" ] && kill -0 "$OLD_PID" 2>/dev/null && \
       grep -q "bastion" "/proc/$OLD_PID/cmdline" 2>/dev/null; then
        echo "[!!] Dashboard already running (PID $OLD_PID). Exiting."
        exit 1
    fi
    rm -f "$LOCK_FILE"
fi
echo $$ > "$LOCK_FILE"

cd "$PROJECT_DIR" || exit 1

# ---------------------------------------------------------------------------
# Resolve a Python interpreter that has the dashboard's dependencies.
#
# GUI .desktop launches do NOT source ~/.bashrc, so conda is never initialised
# and no env is active — `python` may not even be on PATH. We therefore source
# conda, honour BASTION_CONDA_ENV if set, and otherwise auto-detect *any* conda
# env that can import the deps (portable: no hard-coded env name). Any fatal
# failure calls die(), which keeps the terminal window open so the message is
# readable — a .desktop-spawned terminal closes the instant this script exits.
# ---------------------------------------------------------------------------

# Imports the TUI dashboard needs at startup (collectors pull in psutil).
_DASH_DEPS="textual, httpx, psutil"

# Keep a .desktop-spawned terminal open long enough to read a message. No-op
# when stdin is not a TTY (CI / piped / selftest) so automation never hangs.
keep_open() {
    if [ -t 0 ]; then
        echo
        read -rn1 -p "Press any key to close this window..." _ || true
        echo
    fi
}

die() {
    echo "[!!] $*" >&2
    keep_open
    rm -f "$LOCK_FILE"
    exit 1
}

# True when the `python` on PATH can import every dashboard dependency.
_deps_ok() {
    command -v python &>/dev/null && python -c "import ${_DASH_DEPS}" 2>/dev/null
}

# Source conda so `conda activate` becomes available in this non-login shell.
CONDA_BASE="$(conda info --base 2>/dev/null || true)"
for _conda_sh in \
    "${CONDA_BASE:+$CONDA_BASE/etc/profile.d/conda.sh}" \
    "$HOME/miniforge3/etc/profile.d/conda.sh" \
    "$HOME/miniconda3/etc/profile.d/conda.sh" \
    "$HOME/anaconda3/etc/profile.d/conda.sh"; do
    if [ -n "$_conda_sh" ] && [ -f "$_conda_sh" ]; then
        # shellcheck disable=SC1090
        source "$_conda_sh"
        break
    fi
done

# 1. An explicitly requested env wins.
if [ -n "${BASTION_CONDA_ENV:-}" ]; then
    conda activate "$BASTION_CONDA_ENV" 2>/dev/null \
        || die "BASTION_CONDA_ENV='${BASTION_CONDA_ENV}' could not be activated."
fi

# 2. If the deps still aren't importable, auto-detect a conda env that has them.
if ! _deps_ok; then
    _candidates=(
        "$HOME/miniforge3/envs/"*/bin/python
        "$HOME/miniconda3/envs/"*/bin/python
        "$HOME/anaconda3/envs/"*/bin/python
    )
    [ -n "$CONDA_BASE" ] && _candidates+=( "$CONDA_BASE/envs/"*/bin/python )
    for _env_py in "${_candidates[@]}"; do
        [ -x "$_env_py" ] || continue
        if "$_env_py" -c "import ${_DASH_DEPS}" 2>/dev/null; then
            export PATH="$(dirname "$_env_py"):$PATH"
            echo "[..] Using Python: $_env_py"
            break
        fi
    done
fi

# 3. Last resort: a named env exists but is missing deps — try to install them.
if ! _deps_ok && [ -n "${BASTION_CONDA_ENV:-}" ] && command -v python &>/dev/null; then
    echo "[..] Installing dashboard dependencies into '${BASTION_CONDA_ENV}'..."
    python -m pip install "textual>=1.0" "httpx>=0.27" "psutil>=5.9" -q || true
fi

# 4. Still no usable interpreter — fail loudly, window stays open.
if ! _deps_ok; then
    if ! command -v python &>/dev/null; then
        die "No Python found. GUI launches do not load your shell's conda setup.
     Fix: set BASTION_CONDA_ENV to your conda env name in the launcher, e.g.
       env BASTION_CONDA_ENV=<your-env> '$0'
     or run 'conda activate <your-env>' before launching from a terminal."
    fi
    die "Python ($(command -v python)) is missing required packages: ${_DASH_DEPS}.
     Install them:  python -m pip install textual httpx psutil
     or point BASTION_CONDA_ENV at an environment that has them."
fi

# Self-test hook: verify the interpreter resolved, then exit before touching
# the GPU / Ollama / broker. Lets the install path be validated non-invasively.
if [ -n "${BASTION_LAUNCH_SELFTEST:-}" ]; then
    echo "[ok] Python: $(command -v python)"
    echo "[ok] Dashboard deps present: ${_DASH_DEPS}"
    rm -f "$LOCK_FILE"
    exit 0
fi

LOG_DIR="${PROJECT_DIR}/logs"
mkdir -p "$LOG_DIR"

OLLAMA_PORT=11435
BASTION_PORT=11434
STARTED_OLLAMA=""
STARTED_BASTION=""

# Helper: curl Ollama through bastion group (nftables blocks other GIDs on 11435)
ollama_check() {
    sg bastion -c "curl -sf http://127.0.0.1:${OLLAMA_PORT}/" >/dev/null 2>&1
}

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
if ollama_check; then
    echo "[ok] Ollama already running on port ${OLLAMA_PORT}"
elif systemctl is-active --quiet ollama 2>/dev/null; then
    # systemd service is running but not responding — might be on wrong port
    echo "[..] Ollama systemd service active but not responding on ${OLLAMA_PORT}"
    echo "     Restarting with port override..."
    sudo systemctl restart ollama
    for i in $(seq 1 30); do
        if ollama_check; then
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
        if ollama_check; then
            echo "[ok] Ollama started via systemd on port ${OLLAMA_PORT}"
            break
        fi
        sleep 0.5
    done
    if ! ollama_check; then
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
        if ollama_check; then
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

    if [ -n "$STARTED_OLLAMA" ] && ! ollama_check; then
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
    sg bastion -c "PYTHONPATH=src python -m bastion --config config/broker.yaml >> '${LOG_DIR}/bastion-broker.log' 2>&1" &
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
_dash_rc=$?
if [ "$_dash_rc" -ne 0 ]; then
    echo "[!!] Dashboard exited with code ${_dash_rc} (see traceback above)."
    keep_open
fi
exit "$_dash_rc"
