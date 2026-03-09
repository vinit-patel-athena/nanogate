# nanogate

A multi-tenant API Gateway and Docker orchestrator for the [nanobot](https://github.com/HKUDS/nanobot) framework.

`nanogate` acts as a reverse proxy and isolation layer, spinning up dedicated Docker containers for individual AI agent tenants on-demand. It handles dynamic CLI injections, environment variable mapping, and request proxying.

## Architecture

`nanogate` fundamentally changes how `nanobot` operates in production environments:
1. **API Gateway**: Exposes endpoints (`/api/tenant/config`, `/api/chat`, `/api/approve`) to manage tenant sessions dynamically.
2. **Container Provisioning**: Translates tenant JSON configs into isolated Docker containers running the `hkuds/nanobot:latest` image.
3. **Dynamic Setup**: Mounts local script directories and seamlessly installs global packages (`npm`, `pip`, `apt`) dynamically via the `setupCommands` array before unblocking traffic.
4. **Proxy**: Forwards `/chat` and human-in-the-loop `/approve` traffic specifically to the mapped ports of the containerized LLMs.

## Installation

```bash
git clone <this-repo> nanogate
cd nanogate
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

## Running the Server

Start the orchestration server locally on port `8765`:

```bash
source .venv/bin/activate
python -m gateway.server
```

## Usage

### 1. Provision a Tenant
Inject Docker orchestration commands, custom environment variables, and the Nanobot configuration payload:

```bash
curl -X POST http://localhost:8765/api/tenant/config \
 -H "Content-Type: application/json" \
 -d '{
  "tenant_id": "tenant-xyz",
  "config": {
    "gateway": {
      "setupCommands": ["npm install -g @googleworkspace/cli"],
      "env": {
        "GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE": "/app/gateway_scripts/client_secret.json"
      }
    },
    "agents": { ... },
    "providers": { ... },
    "tools": { ... }
  }
}'
```

### 2. Initiate Chat

```bash
curl -X POST http://localhost:8765/api/chat \
  -H "Content-Type: application/json" \
  -d '{
  "tenantId": "tenant-xyz",
  "sessionId": "session-1",
  "message": "Send test email to user@example.com"
}'
```

### 3. Approve Executions (Human-in-the-loop)

```bash
curl -X POST http://localhost:8765/api/approve \
  -H "Content-Type: application/json" \
  -d '{
  "tenantId": "tenant-xyz",
  "sessionId": "session-1",
  "request_id": "uuid-from-chat-response",
  "autoResume": true
}'
```

## Requirements
- Python 3.11+
- Docker Engine executing on host machine
