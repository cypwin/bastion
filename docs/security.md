# Security Guide

## Threat Model

BASTION is a local GPU broker. It is designed to run on a single machine or
trusted LAN. It is NOT designed for public internet exposure.

Default configuration:
- Binds to `0.0.0.0` (all interfaces) on port 11434
- No authentication required
- No TLS

This is safe for single-user workstations. For shared or remote access,
follow the hardening steps below.

## Reporting Vulnerabilities

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

## Hardening Checklist

### 1. Enable Authentication

Edit `broker.yaml`:

```yaml
auth:
  enabled: true
  api_keys:
    - "your-secret-key-1"
    - "your-secret-key-2"
```

Test with curl:

```bash
# Without key -- should return 401
curl http://localhost:11434/broker/status

# With key -- should return 200
curl http://localhost:11434/broker/status \
  -H "Authorization: Bearer your-secret-key-1"
```

Generate secure keys:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

### 2. Restrict Network Access

#### Bind to localhost only

Edit `broker.yaml`:

```yaml
server:
  host: "127.0.0.1"    # only accessible from this machine
```

Or via environment variable:

```bash
BASTION_HOST=127.0.0.1 bastion
```

#### nftables port lockdown

Restrict Ollama's backend port (11435) so only BASTION can reach it.
This prevents local users from bypassing the broker.

```bash
# Create the bastion group
sudo groupadd bastion

# Add your user to the group
sudo usermod -aG bastion $USER

# Add nftables rules
sudo nft add table inet bastion_guard
sudo nft add chain inet bastion_guard output '{ type filter hook output priority 0; policy accept; }'
sudo nft add rule inet bastion_guard output tcp dport 11435 skgid != bastion reject
```

Run BASTION under the bastion group:

```bash
sg bastion -c "bastion"
```

Or configure in the systemd service:

```ini
[Service]
Group=bastion
```

### 3. Add TLS via Reverse Proxy

BASTION does not terminate TLS directly. Use a reverse proxy for HTTPS.

#### Caddy (recommended -- automatic HTTPS)

```
bastion.local {
    reverse_proxy localhost:11434
}
```

#### nginx

```nginx
server {
    listen 443 ssl;
    server_name bastion.local;

    ssl_certificate /etc/ssl/certs/bastion.crt;
    ssl_certificate_key /etc/ssl/private/bastion.key;

    location / {
        proxy_pass http://127.0.0.1:11434;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_buffering off;          # important for streaming
        proxy_read_timeout 300s;      # match inference timeout
    }
}
```

### 4. Enable Rate Limiting

Prevent abuse from any single IP:

```yaml
rate_limit:
  enabled: true
  requests_per_minute: 60
  burst: 10
```

Adjust `requests_per_minute` based on your expected traffic. The `burst`
parameter allows short spikes above the sustained rate.

### 5. Systemd Security Hardening

The example service file includes security directives:

```ini
[Service]
# Run as dedicated user
User=bastion
Group=bastion

# Filesystem protection
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=/home/bastion/.local/share/bastion
ReadWritePaths=/home/bastion/.config/bastion

# Privilege escalation prevention
NoNewPrivileges=yes
PrivateTmp=yes

# Network restrictions
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
```

### 6. Audit Log Security

Audit logs are written to `~/.local/share/bastion/bastion-audit.jsonl`.

Protect log files:

```bash
chmod 600 ~/.local/share/bastion/bastion-audit.jsonl
```

Configure audit tier in `broker.yaml`:

```yaml
audit:
  tier: 2          # 1=minimal, 2=hashes (default), 3=full content
  content_hashing: true
```

- **Tier 1**: Timestamps, request ID, model, operation, token counts, latency
- **Tier 2**: Tier 1 + SHA-256 content hashes of prompt/response
- **Tier 3**: Tier 2 + full prompt/response text (debugging only -- do not use in production with sensitive data)

## Supported Versions

| Version | Supported |
|---------|-----------|
| 0.3.x   | Yes       |
| < 0.3   | No        |
