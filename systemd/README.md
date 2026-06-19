# Systemd Service Files

This directory contains `.example` templates for running BASTION as a systemd service.

## Setup

1. **Copy and customize** each `.example` file, replacing placeholders with your actual values:

   | Placeholder           | Description                              | Example                                    |
   |-----------------------|------------------------------------------|--------------------------------------------|
   | `<RUN_USER>`          | Linux user the bastion.service unit runs as | `john`                                  |
   | `<USER>`              | Linux user granted sudoers systemctl access | `john`                                  |
   | `<BASTION_DIR>`       | Absolute path to BASTION project root    | `/home/john/bastion`                       |
   | `<PYTHON_PATH>`       | Absolute path to Python binary           | `/home/john/miniconda3/envs/ml/bin/python` |
   | `<POWER_LIMIT_WATTS>` | GPU power cap in watts                   | `425`                                      |

2. **Install the files:**

   ```bash
   # BASTION service
   sudo cp bastion.service.example /etc/systemd/system/bastion.service
   # Edit /etc/systemd/system/bastion.service — replace all <PLACEHOLDERS>

   # Ollama port override (moves Ollama to 11435)
   sudo mkdir -p /etc/systemd/system/ollama.service.d/
   sudo cp ollama-port-override.conf.example /etc/systemd/system/ollama.service.d/override.conf

   # GPU power cap (optional, recommended for high-TDP GPUs)
   sudo cp nvidia-powercap.service.example /etc/systemd/system/nvidia-powercap.service
   # Edit — set your GPU's power limit

   # Sudoers (optional, allows dashboard to restart the service)
   sudo cp bastion-sudoers.example /etc/sudoers.d/bastion
   sudo chmod 0440 /etc/sudoers.d/bastion
   sudo visudo -cf /etc/sudoers.d/bastion
   ```

3. **Reload and start:**

   ```bash
   sudo systemctl daemon-reload
   sudo systemctl restart ollama
   sudo systemctl enable --now bastion
   ```

## Files

| File                                | Purpose                                             |
|-------------------------------------|-----------------------------------------------------|
| `bastion.service.example`           | Main BASTION service unit                           |
| `ollama-port-override.conf.example` | Moves Ollama to port 11435 so BASTION owns 11434   |
| `nvidia-powercap.service.example`   | Sets GPU power limit on boot (crash prevention)     |
| `bastion-sudoers.example`           | Passwordless systemctl for dashboard TUI            |
