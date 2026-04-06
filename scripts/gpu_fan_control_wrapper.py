#!/usr/bin/env python3
"""
Minimal wrapper for GPU fan control commands.
Designed to be called via sudo NOPASSWD for security.

This script validates input and only accepts predefined fan speeds.
It wraps nvidia-settings commands for fan control.

Usage:
    sudo python gpu_fan_control_wrapper.py 90      # Set to 90%
    sudo python gpu_fan_control_wrapper.py 100     # Set to 100%
    sudo python gpu_fan_control_wrapper.py auto    # Reset to auto

Origin: Adapted from PHENOTYPE project for BASTION standalone use.
"""

from __future__ import annotations

import os
import subprocess
import sys

# Only these values are allowed - no arbitrary input
VALID_SPEEDS = {'30', '50', '70', '90', '100', 'auto'}


def get_display_env() -> dict:
    """Get environment with DISPLAY and XAUTHORITY set."""
    env = os.environ.copy()

    # Try to find the active display
    if 'DISPLAY' not in env:
        # Check common display values
        for display in [':1', ':0']:
            x_socket = f"/tmp/.X11-unix/X{display[1:]}"
            if os.path.exists(x_socket):
                env['DISPLAY'] = display
                break

    # Try to find XAUTHORITY
    if 'XAUTHORITY' not in env:
        uid = os.getenv('SUDO_UID', '1000')
        possible_paths = [
            f"/run/user/{uid}/gdm/Xauthority",
            f"/home/{os.getenv('SUDO_USER', os.getenv('USER', 'nobody'))}/.Xauthority",
        ]
        for path in possible_paths:
            if os.path.exists(path):
                env['XAUTHORITY'] = path
                break

    return env


def set_fan_speed(speed: str) -> bool:
    """Set GPU fan to specified speed or auto."""
    env = get_display_env()

    try:
        # Get number of GPUs
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=count", "--format=csv,noheader"],
            capture_output=True,
            text=True,
            check=True
        )
        n_gpus = int(result.stdout.strip())

        for gpu_id in range(n_gpus):
            if speed == 'auto':
                # Reset to automatic control
                subprocess.run(
                    ["nvidia-settings", "-a", f"[gpu:{gpu_id}]/GPUFanControlState=0"],
                    env=env,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False
                )
            else:
                # Enable manual control
                subprocess.run(
                    ["nvidia-settings", "-a", f"[gpu:{gpu_id}]/GPUFanControlState=1"],
                    env=env,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False
                )
                # Set fan speed (handle multiple fans per GPU)
                for fan_id in range(2):  # Most GPUs have 1-2 fans
                    fan_idx = gpu_id * 2 + fan_id
                    fan_arg = f"[fan:{fan_idx}]/GPUTargetFanSpeed={speed}"
                    subprocess.run(
                        ["nvidia-settings", "-a", fan_arg],
                        env=env,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        check=False
                    )

        return True

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return False


def main() -> None:
    if len(sys.argv) != 2:
        print("Usage: gpu_fan_control_wrapper.py <speed|auto>")
        print(f"Valid speeds: {', '.join(sorted(VALID_SPEEDS))}")
        sys.exit(1)

    speed = sys.argv[1].lower()

    if speed not in VALID_SPEEDS:
        print(f"Error: Invalid speed '{speed}'")
        print(f"Valid speeds: {', '.join(sorted(VALID_SPEEDS))}")
        sys.exit(1)

    if set_fan_speed(speed):
        if speed == 'auto':
            print("OK: Fan control set to AUTO")
        else:
            print(f"OK: Fan speed set to {speed}%")
        sys.exit(0)
    else:
        print("FAIL: Could not set fan speed")
        sys.exit(1)


if __name__ == "__main__":
    main()
