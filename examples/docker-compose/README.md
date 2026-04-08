# Docker Compose Quickstart

Run BASTION + Ollama as a single stack with Docker Compose.

## Prerequisites

- Docker with Compose v2
- NVIDIA GPU with drivers installed
- [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html)

## Usage

```bash
# Start the stack
docker compose up -d

# Verify both services are running
docker compose ps

# Check BASTION status
curl http://localhost:11434/broker/status | jq .

# Pull a model (through BASTION)
curl http://localhost:11434/api/pull -d '{"name": "llama3.1:8b"}'

# Run inference
curl http://localhost:11434/api/generate -d '{
  "model": "llama3.1:8b",
  "prompt": "What is BASTION?",
  "stream": false
}'

# Watch logs
docker compose logs -f bastion

# Shut down
docker compose down
```

## Architecture

```
Host port 11434 --> BASTION container --> Ollama container
                    (proxy + scheduler)   (GPU inference)
```

BASTION connects to Ollama over the Docker network. Only BASTION is exposed
to the host, so all requests are brokered through the scheduler.

## Custom Configuration

Mount a config file to override defaults:

```yaml
services:
  bastion:
    volumes:
      - ./my-broker.yaml:/etc/bastion/broker.yaml:ro
```

See `config/broker.example.yaml` in the BASTION repo for all options.
