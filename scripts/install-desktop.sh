#!/usr/bin/env bash
# Install BASTION Dashboard as a pinnable Linux desktop app + login autostart.
#
# Drops two .desktop files into XDG user dirs:
#   ~/.local/share/applications/bastion-dashboard.desktop  (app menu)
#   ~/.config/autostart/bastion-dashboard.desktop          (runs on login)
#
# Usage:
#   scripts/install-desktop.sh                  # install both
#   scripts/install-desktop.sh --no-autostart   # app menu only
#   scripts/install-desktop.sh uninstall        # remove both
#
# Idempotent: re-running just rewrites the files.
#
# Launcher resolution (Exec= line):
#   1. scripts/launch_dashboard.sh   — preferred: it also ensures Ollama and the
#      BASTION broker are up before the TUI starts. If a conda env is active when
#      you run this installer, its name is baked in as `env BASTION_CONDA_ENV=…`
#      so the GUI launch (which does not load your shell) finds the interpreter.
#   2. bastion-dashboard on PATH     — fallback when run outside a repo checkout
#      (e.g. a pip install); launches only the TUI, not the broker.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
TEMPLATE="$PROJECT_DIR/packaging/bastion-dashboard.desktop.in"

APP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
AUTOSTART_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/autostart"
DESKTOP_FILE="bastion-dashboard.desktop"

uninstall() {
    local removed=0
    for path in "$APP_DIR/$DESKTOP_FILE" "$AUTOSTART_DIR/$DESKTOP_FILE"; do
        if [ -f "$path" ]; then
            rm -f "$path"
            echo "removed: $path"
            removed=1
        fi
    done
    if command -v update-desktop-database >/dev/null 2>&1; then
        update-desktop-database "$APP_DIR" 2>/dev/null || true
    fi
    [ "$removed" -eq 0 ] && echo "nothing to remove"
    return 0
}

resolve_launcher() {
    # Prefer the in-tree launcher (it starts Ollama + the broker too), baking in
    # the conda env that is active right now so the GUI launch can find Python.
    # Fall back to the installed console entry point.
    if [ -x "$SCRIPT_DIR/launch_dashboard.sh" ]; then
        if [ -n "${CONDA_DEFAULT_ENV:-}" ] && [ "${CONDA_DEFAULT_ENV}" != "base" ]; then
            echo "env BASTION_CONDA_ENV=${CONDA_DEFAULT_ENV} $SCRIPT_DIR/launch_dashboard.sh"
        else
            echo "$SCRIPT_DIR/launch_dashboard.sh"
        fi
    elif command -v bastion-dashboard >/dev/null 2>&1; then
        command -v bastion-dashboard
    else
        echo "error: cannot find scripts/launch_dashboard.sh in a checkout or bastion-dashboard on PATH" >&2
        echo "       run this script from a repo checkout, or install BASTION first (pip install -e .)" >&2
        exit 1
    fi
}

install_one() {
    local target="$1"
    local launcher="$2"
    mkdir -p "$(dirname "$target")"
    # `launcher` may contain '/', so use a sed delimiter that cannot appear in a path.
    sed "s|@LAUNCHER@|${launcher}|g" "$TEMPLATE" > "$target"
    chmod 644 "$target"
    echo "installed: $target"
}

case "${1:-install}" in
    uninstall|remove)
        uninstall
        ;;
    install|--no-autostart|"")
        [ -f "$TEMPLATE" ] || { echo "error: template missing: $TEMPLATE" >&2; exit 1; }
        launcher="$(resolve_launcher)"
        echo "launcher: $launcher"
        install_one "$APP_DIR/$DESKTOP_FILE" "$launcher"
        if [ "${1:-}" != "--no-autostart" ]; then
            install_one "$AUTOSTART_DIR/$DESKTOP_FILE" "$launcher"
        fi
        if command -v update-desktop-database >/dev/null 2>&1; then
            update-desktop-database "$APP_DIR" 2>/dev/null || true
        fi
        echo
        echo "Done. To pin: open the apps overview, search 'BASTION', right-click → Pin."
        echo "Autostart entry will launch the dashboard at next login."
        echo
        echo "Note: Terminal=true makes the OS pick your default terminal; window"
        echo "      placement (e.g. bottom-right corner) is manual until a"
        echo "      per-DE positioning wrapper is added. See packaging/."
        ;;
    -h|--help|help)
        sed -n '2,21p' "$0"
        ;;
    *)
        echo "unknown arg: $1" >&2
        echo "try: $0 --help" >&2
        exit 2
        ;;
esac
