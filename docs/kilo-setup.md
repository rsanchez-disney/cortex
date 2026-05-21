# Connecting Kilo to the Cortex MCP Server (Cloud Run)

This guide explains how to connect your Kilo AI agent to the Cortex MCP server running on GCP Cloud Run.

## Prerequisites

- [gcloud CLI](https://cloud.google.com/sdk/docs/install) installed and authenticated
- Kilo installed (≥ 7.3.1 recommended)
- IAM access to the Cortex Cloud Run service (request from an admin if needed — see [Requesting Access](#requesting-access))

---

## Step 1 — Request IAM Access (Admin Only)

A GCP admin with `roles/run.admin` or `roles/owner` on the project must grant you the `roles/run.invoker` role:

```bash
gcloud run services add-iam-policy-binding cortex \
  --region=us-central1 \
  --project=prj-ai-flow-orchestrator-gp-gc \
  --member="user:your.name@globant.com" \
  --role="roles/run.invoker"
```

> Without this role, all requests to the MCP server will return `403 Forbidden`.

---

## Step 2 — Authenticate with GCP

```bash
# Log in to GCP (opens browser)
gcloud auth login

# Set the active project
gcloud config set project prj-ai-flow-orchestrator-gp-gc
```

Verify your identity and access:

```bash
gcloud auth print-identity-token
# Should print a long JWT token — if this fails, re-run `gcloud auth login`
```

---

## Step 3 — Start the Authenticated Proxy Tunnel

The Cloud Run service requires a valid Google identity token on every request. The `gcloud run services proxy` command starts a local proxy that injects the token automatically, so Kilo doesn't need to handle auth itself.

```bash
gcloud run services proxy cortex \
  --region=us-central1 \
  --project=prj-ai-flow-orchestrator-gp-gc \
  --port=3128
```

**Important:**
- Keep this terminal open. The proxy runs in the **foreground** — closing it disconnects Kilo.
- By default it listens on `http://localhost:3128`.
- The proxy automatically refreshes your identity token before it expires.

---

## Step 4 — Configure `kilo.json`

Add the following entry to your `kilo.json` (at the root of your project, or wherever Kilo looks for its config):

```json
{
  "$schema": "https://app.kilo.ai/config.json",
  "mcp": {
    "cortex": {
      "type": "remote",
      "url": "http://localhost:3128/mcp",
      "enabled": true
    }
  },
  "permission": {
    "cortex_find_relevant_services": "allow",
    "cortex_get_service_context": "allow",
    "cortex_list_endpoints": "allow",
    "cortex_get_endpoint_contract": "allow"
  }
}
```

> **The URL must point to the proxy port** (`3128`). The proxy handles IAM authentication transparently.

---

## Step 5 — Verify Connectivity (Optional)

In a separate terminal, confirm the proxy and server are reachable:

```bash
curl -s -o /dev/null -w "%{http_code}\n" \
  -X POST http://localhost:3128/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","method":"initialize","id":1,"params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"test","version":"0.1"}}}'
```

Expected output: `200`

| Status | Meaning |
|--------|---------|
| `200`  | ✅ Connected and authenticated |
| `403`  | ❌ No IAM access — request it from an admin |
| `401`  | ❌ Not authenticated — run `gcloud auth login` |
| `Connection refused` | ❌ Proxy is not running — run Step 3 |

---

## Step 6 — Use Kilo

Open Kilo. It will automatically discover the `cortex` MCP server from your `kilo.json` and connect through the proxy.

You will have access to **4 tools**:

| Tool | Description |
|------|-------------|
| `find_relevant_services` | Find services matching a task description or keyword |
| `list_endpoints` | List all HTTP endpoints exposed by a service |
| `get_service_context` | Deep context on a single service (manifest, dependencies, Kafka topics, etc.) |
| `get_endpoint_contract` | Full endpoint schema including request/response DTOs |

---

## Troubleshooting

### `403 Forbidden`
Your Google account does not have `roles/run.invoker` on the `cortex` service. Ask a GCP admin to run the grant command in [Step 1](#step-1--request-iam-access-admin-only).

### `401 Unauthorized`
Your identity token is missing or expired. Re-authenticate:
```bash
gcloud auth login
```

### `Connection refused` on `localhost:3128`
The proxy is not running. Start it as shown in [Step 3](#step-3--start-the-authenticated-proxy-tunnel).

### Kilo doesn't show Cortex tools
- Confirm `kilo.json` has `"url": "http://localhost:3128/mcp"` (not port 8000 or the direct Cloud Run URL).
- Confirm the proxy terminal is still running.
- Restart Kilo after editing `kilo.json`.

### Token refresh issues during long sessions
The `gcloud run services proxy` command automatically refreshes identity tokens. If you see auth errors after many hours, stop and restart the proxy.

---

## Admin Operations

### List all principals with access

```bash
gcloud run services get-iam-policy cortex \
  --region=us-central1 \
  --project=prj-ai-flow-orchestrator-gp-gc
```

### Revoke access

```bash
gcloud run services remove-iam-policy-binding cortex \
  --region=us-central1 \
  --project=prj-ai-flow-orchestrator-gp-gc \
  --member="user:name.surname@globant.com" \
  --role="roles/run.invoker"
```

### View Cloud Run logs

```bash
# Last 50 log entries for the Cortex service only
gcloud logging read \
  'resource.type="cloud_run_revision" AND resource.labels.service_name="cortex"' \
  --project=prj-ai-flow-orchestrator-gp-gc \
  --limit=50 \
  --format="table(timestamp,textPayload)"
```
