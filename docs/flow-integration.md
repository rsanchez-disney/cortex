# Integrating Flow with the Cortex MCP Server

## Overview

**Cortex** is an MCP (Model Context Protocol) server deployed on Cloud Run in the same GCP project (`prj-ai-flow-orchestrator-gp-gc`). It provides structured architectural metadata about all platform services (Android, iOS, backend Java) — including endpoints, dependencies, communication graphs, and API contracts — exposed via 4 MCP tools. Flow workers can use Cortex to get real-time service context when planning or executing tasks.

## What's Already Done (Cortex Side)

1. **IAM access is already granted.** The Cortex deploy script automatically grants `roles/run.invoker` to Flow's service account (`archon-prod@prj-ai-flow-orchestrator-gp-gc.iam.gserviceaccount.com`). No additional IAM setup is needed.

2. **The MCP endpoint is live.** Cortex serves a single endpoint using the **Streamable HTTP** transport (MCP spec 2025-03-26).

## Connection Details

| Property | Value |
|---|---|
| **Cloud Run Service** | `cortex` |
| **Service URL** | `https://cortex-2gqh4rrbwa-uc.a.run.app` |
| **MCP Endpoint** | `POST /mcp` |
| **Full URL** | `https://cortex-2gqh4rrbwa-uc.a.run.app/mcp` |
| **Transport** | Streamable HTTP (MCP 2025-03-26) — stateless mode |
| **Auth** | Google Identity Token (Cloud Run IAM) |
| **Flow SA (already authorized)** | `archon-prod@prj-ai-flow-orchestrator-gp-gc.iam.gserviceaccount.com` |
| **GCP Project** | `prj-ai-flow-orchestrator-gp-gc` |
| **Region** | `us-central1` |

## How to Connect

### Option A: MCP Client Configuration (Recommended)

If Flow uses an MCP client library (e.g., the official MCP Python/TypeScript SDK), configure Cortex as a **remote** MCP server:

```json
{
  "mcp": {
    "cortex": {
      "type": "remote",
      "url": "https://cortex-2gqh4rrbwa-uc.a.run.app/mcp",
      "enabled": true
    }
  }
}
```

### Option B: Direct HTTP Calls

Cortex uses the MCP JSON-RPC protocol over HTTP. All requests are `POST /mcp` with `Content-Type: application/json`.

**Authentication:** Every request must include a Google Identity Token in the `Authorization` header. Since Flow workers run on Cloud Run with the `archon-prod` service account (which already has `roles/run.invoker`), you can fetch the token from the metadata server:

```python
import google.auth.transport.requests
import google.oauth2.id_token

target_audience = "https://cortex-2gqh4rrbwa-uc.a.run.app"
auth_req = google.auth.transport.requests.Request()
token = google.oauth2.id_token.fetch_id_token(auth_req, target_audience)

headers = {
    "Authorization": f"Bearer {token}",
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}
```

> **Important:** The `target_audience` for the identity token must be the **Cloud Run service URL** (without `/mcp`).

### MCP Protocol Flow

The server runs in **stateless mode** — no session state is maintained between requests. The MCP protocol requires an `initialize` handshake before calling tools:

**Step 1: Initialize**
```bash
curl -X POST https://cortex-2gqh4rrbwa-uc.a.run.app/mcp \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{
    "jsonrpc": "2.0",
    "method": "initialize",
    "id": 1,
    "params": {
      "protocolVersion": "2025-03-26",
      "capabilities": {},
      "clientInfo": {"name": "flow", "version": "1.0"}
    }
  }'
```

**Step 2: Call a tool**
```bash
curl -X POST https://cortex-2gqh4rrbwa-uc.a.run.app/mcp \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{
    "jsonrpc": "2.0",
    "method": "tools/call",
    "id": 2,
    "params": {
      "name": "find_relevant_services",
      "arguments": {
        "task_description": "payments order processing",
        "max_results": 5
      }
    }
  }'
```

> **Note:** Because the server is stateless, each HTTP request is independent. There is no session ID to track. You can send `initialize` + `tools/call` as separate requests without any session binding.

## Available MCP Tools

### 1. `find_relevant_services`

Discovers services relevant to a free-text task description using keyword matching.

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `task_description` | string | ✅ | — | Free-text description of the task (e.g., `"payments order processing"`) |
| `max_results` | int | ❌ | `5` | Maximum number of results to return |

**Returns:** Ranked list of candidate services with scores, types, domains, purposes, and communication neighbors.

---

### 2. `list_endpoints`

Lists all endpoints a service exposes.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `service` | string | ✅ | Service name (e.g., `"orders-api"`) |

**Returns:** Array of endpoints with HTTP methods and paths.

---

### 3. `get_service_context`

Deep context on a single service — the main orientation tool for agents.

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `name` | string | ✅ | — | Service name |
| `include` | list[string] | ❌ | `["manifest", "deps", "contracts", "notes", "communication"]` | Sections to include |

**Sections:**
- `manifest` — core metadata (type, owner, domain, tier, purpose, tech stack)
- `deps` — direct library/module dependencies
- `contracts` — API contract definitions
- `notes` — integration notes (global and per-endpoint)
- `communication` — Kafka topics (publish/subscribe) and HTTP call graph

**Returns:** Manifest metadata, direct dependencies, API contracts, integration notes, and communication graph.

---

### 4. `get_endpoint_contract`

Full request/response schema for a specific endpoint.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `service` | string | ✅ | Service name |
| `method` | string | ✅ | HTTP method (e.g., `"GET"`, `"POST"`) |
| `path` | string | ✅ | Endpoint path (e.g., `"/api/v1/orders"`) |

**Returns:** Parameters, request body schema, response schema, DTO definitions, and integration notes. For mobile services (android/ios), returns a message indicating no API spec is available (mobile apps are consumers, not providers).

---

## Environment Variable for Flow Workers

We recommend setting the Cortex URL as an environment variable on Flow worker jobs. Use `--update-env-vars` (**not** `--set-env-vars`, which would wipe existing vars):

```bash
gcloud run jobs update archon-product-worker-prod \
  --region=us-central1 \
  --update-env-vars CORTEX_URL=https://cortex-2gqh4rrbwa-uc.a.run.app \
  --project=prj-ai-flow-orchestrator-gp-gc
```

Then in code, read `os.environ["CORTEX_URL"]` and append `/mcp` for the full MCP endpoint.

## Smoke Test (Verify Connectivity)

Run this from any environment with the `archon-prod` SA, or from a local machine with `gcloud auth`:

```bash
# Get an identity token (local machine)
TOKEN=$(gcloud auth print-identity-token)

# Or from within a Cloud Run container using the GCP metadata server:
# TOKEN=$(curl -s \
#   "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/identity?audience=https://cortex-2gqh4rrbwa-uc.a.run.app" \
#   -H "Metadata-Flavor: Google")

# Test connectivity — expected HTTP response: 200
curl -s -o /dev/null -w "%{http_code}" \
  -X POST https://cortex-2gqh4rrbwa-uc.a.run.app/mcp \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{
    "jsonrpc": "2.0",
    "method": "initialize",
    "id": 1,
    "params": {
      "protocolVersion": "2025-03-26",
      "capabilities": {},
      "clientInfo": {"name": "flow-test", "version": "0.1"}
    }
  }'
```

**Expected responses:**
- `200` — connection successful
- `401` — invalid or expired token
- `403` — SA not authorized (shouldn't happen — `archon-prod` is already granted)

## Key Notes

| Topic | Detail |
|---|---|
| **No session state** | Cortex runs in stateless mode. Every request is independent — no session IDs or connection pools needed. |
| **No app-level auth** | All authentication is handled by Cloud Run IAM. Just include a valid Google Identity Token. |
| **Read-only** | All 4 tools are read-only queries against pre-extracted service metadata. There are no write operations. |
| **Data freshness** | Cortex data is refreshed by the extraction pipeline (scheduled or manual). The MCP server caches the platform graph in memory on startup. Manifest data is cached for 1 hour. |
| **Flow SA already authorized** | The deploy script grants `roles/run.invoker` to `archon-prod@prj-ai-flow-orchestrator-gp-gc.iam.gserviceaccount.com` on every deploy. If Flow uses a different SA, contact the Cortex team. |
| **Namespace isolation** | All Cortex GCP resources are prefixed `cortex-` and use an isolated Firestore named database (`cortex`), separate from Flow's `archon-prod` database. |

## Contact / Access Issues

If Flow workers get `403 Forbidden`, the SA being used is not yet authorized. Share the SA email with the Cortex team and run:

```bash
gcloud run services add-iam-policy-binding cortex \
  --region=us-central1 \
  --project=prj-ai-flow-orchestrator-gp-gc \
  --member="serviceAccount:<flow-sa>@prj-ai-flow-orchestrator-gp-gc.iam.gserviceaccount.com" \
  --role="roles/run.invoker"
```
