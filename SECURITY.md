# Security Policy

## Supported versions

| Version | Supported |
| ------- | --------- |
| 0.3.x   | Yes       |
| < 0.3   | No        |

## Reporting a vulnerability

If you discover a security vulnerability in BASTION, please report it
responsibly:

1. **Do NOT open a public issue.**
2. Email **cw.claustrum@gmail.com** with a description of the
   vulnerability, steps to reproduce, and any relevant logs or config.
3. Alternatively, use GitHub's
   [private security advisory](https://github.com/CyprianESPI/BASTION/security/advisories/new)
   feature to report the issue confidentially.

You should receive an acknowledgement within 72 hours. We will work with you to
understand the issue and coordinate a fix before any public disclosure.

## Scope and threat model

BASTION is a **local GPU/LLM broker** designed to run on a single machine. It is
not designed to be exposed to the public internet.

Key considerations:

- **Bind to localhost only.** The default configuration binds to `127.0.0.1`.
  Binding to `0.0.0.0` exposes the proxy and admin API to your local network.
  Only do this on trusted networks and behind a firewall.
- **nftables port lockdown.** Ollama's backend port (11435) is restricted via
  nftables rules so that only processes in the `bastion` group can connect.
  This prevents local users from bypassing the broker.
- **No authentication by default.** The admin API (`/broker/*`) does not require
  authentication out of the box. If you expose BASTION beyond localhost, enable
  API key authentication in `config/broker.yaml`.
- **No TLS.** BASTION does not terminate TLS. If remote access is required, put
  it behind a reverse proxy (e.g., Caddy, nginx) with TLS termination.

## Security-related configuration

See `config/broker.yaml` for:

- `auth.enabled` / `auth.api_keys` — API key authentication for admin endpoints
- `server.host` — bind address (default `127.0.0.1`)
- `rate_limit` — per-IP rate limiting
