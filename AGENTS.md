# AGENTS.md — Platform Cortex

## What This Project Is

Platform Cortex extracts structured architectural metadata from Android, iOS, and backend Java (Spring Boot) repositories, aggregates it into a queryable graph, and exposes it to AI agents via an MCP server. It is a Python CLI tool (`cortex`) with a separate MCP server component.

## Setup

```bash
# Install all dependencies (Python 3.12+ required)
uv sync --extra dev

# Optional: iOS pbxproj parsing support
uv sync --extra ios --extra dev
```

The CLI entry point is `cortex` (defined in `pyproject.toml` as `cortex.cli:app`). Always run via `uv run cortex ...`.

## Project Structure

```
memory-hub/
├── src/cortex/                  # Core package
│   ├── cli.py                  # CLI commands (typer)
│   ├── schema.py               # Pydantic v2 models (ServiceYaml, ServiceManifest, PlatformGraph, etc.)
│   ├── validation.py           # Service metadata validation (JSON Schema + Pydantic)
│   ├── storage.py              # StorageBackend ABC → LocalStorageBackend, GCSStorageBackend, FirestoreStorageBackend
│   ├── firestore_storage.py    # FirestoreStorageBackend (used by Cloud Run)
│   ├── aggregator.py           # Merges manifests into graph/latest.json
│   └── extractors/
│       ├── __init__.py          # Extractor registry (type → class)
│       ├── base.py              # Abstract Extractor base class
│       ├── android.py           # Android extractor
│       ├── ios.py               # iOS extractor
│       └── backend_java.py     # Backend Java (Spring Boot) extractor
├── mcp_server/
│   ├── server.py               # MCP server with 4 tools (FastMCP, stdio + streamable-http)
│   ├── __main__.py             # Cloud Run entry point: python -m mcp_server
│   └── tests/
├── tests/
│   ├── fixtures/               # Sample repos for testing
│   │   ├── sample-android-repo/
│   │   ├── sample-ios-repo/
│   │   ├── sample-ios-multitarget-repo/
│   │   └── sample-backend-java-repo/
│   ├── conftest.py
│   └── test_*.py
├── schemas/                    # JSON Schema files
│   ├── service.schema.json
│   └── manifest.schema.json
├── config/                     # Repo registry configs
│   ├── repos.yaml              # Azure DevOps pipeline (URLs only)
│   ├── repos-local.yaml        # Local dev (paths or URLs)
│   ├── repos-fixtures.yaml     # Points at test fixtures
│   └── repos-real.yaml         # Points at real local repos
├── scripts/
│   ├── deploy.sh               # Idempotent GCP Cloud Run deploy
│   └── upload-to-firestore.sh  # Push local cortex-output to Firestore
├── Dockerfile                  # Cloud Run container (streamable-HTTP mode, port 8080)
└── pipelines/
    └── azure-pipelines.yml
```

## After Making Code Changes

Run these commands from the project root to verify changes:

```bash
# 1. Run the full test suite
uv run pytest tests/ mcp_server/tests/ -v

# 2. Run with coverage (must stay above 75%)
uv run pytest --cov=cortex tests/ mcp_server/tests/ -v

# 3. End-to-end smoke test — runs extract → aggregate → report against fixtures
uv run cortex run-local --config config/repos-fixtures.yaml --output-dir /tmp/cortex-smoke

# 4. Lint
uv run ruff check src/ tests/ mcp_server/
```

If adding a new extractor or modifying an existing one, also run the specific extractor test in isolation to check it first:
```bash
uv run pytest tests/test_android_extractor.py -v
uv run pytest tests/test_ios_extractor.py -v
uv run pytest tests/test_backend_java_extractor.py -v
```

## Key Design Decisions

1. **No auto-detection (`detect.py` does not exist).** The `type` field in the repos config is the sole source of truth for repo type. The extractor registry in `src/cortex/extractors/__init__.py` maps type → extractor class. If a type has no registered extractor, extraction fails with a clear error.

2. **No Go extractor.** `android`, `ios`, and `backend-java` (Spring Boot) extractors are implemented. Other backend types (Go, Node, React) are deferred.

3. **No Azure Blob storage.** Storage backends are `local`, `gcs`, and `firestore` only.

4. **`get_endpoint_contract` MCP tool** returns "no API spec available" for mobile service types. It is still implemented (hard constraint: exactly 4 MCP tools, never add a 5th).

5. **Service metadata lives in repos config, not in target repos.** All `ServiceYaml` fields (`type`, `owner`, `domain`, `tier`, `purpose`, etc.) are declared inline in each entry of `config/repos*.yaml`. Target repos require no `service.yaml` file. Validation happens at pipeline/extraction time from the config dict.

6. **Fail-soft per repo.** One repo's extraction failure must NOT block others. Errors are written to `services/{name}/extraction-error.json`.

7. **MCP HTTP transport is streamable-HTTP (MCP 2025-03-26 spec).** The `run_http()` method uses FastMCP's `streamable_http_app()`, serving a single `POST /mcp` endpoint. This is the transport used both locally (`--mode http`) and on Cloud Run. The legacy SSE transport (`/sse` + `/messages/`) is not used.

8. **No MCP server auth.** Authentication is handled at the infrastructure layer (Cloud Run IAM, `--no-allow-unauthenticated`). The server code has no auth middleware.

9. **Cloud Run ingress is `--ingress all` (not `internal`).** `--ingress internal` was initially used but blocks all external traffic including `gcloud run services proxy` tunnels and developer access from outside the GCP VPC. `--ingress all` is used with `--no-allow-unauthenticated` — IAM enforces auth on every request so there is still no anonymous public access. Security is unchanged; reachability is improved.

10. **FastMCP 1.27+ DNS rebinding protection must be disabled for Cloud Run.** FastMCP ≥1.27 enables DNS rebinding protection by default, which validates the `Host` header against an allow-list. Cloud Run proxies requests through its own ingress, rewriting the `Host` header, causing FastMCP to reject all requests with `421 Misdirected Request`. The fix is applied in `mcp_server/server.py` `run_http()`:
    ```python
    from mcp.server.transport_security import TransportSecuritySettings
    self._mcp.settings.transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=False,
    )
    ```
    This is safe because Cloud Run's infrastructure already handles all security concerns (TLS, IAM, HTTPS).

11. **`stateless_http=True` is required for Cloud Run.** FastMCP's default stateful HTTP mode stores session state in the container's memory. Cloud Run is serverless: instances are ephemeral, can scale to zero, and requests may be routed to different instances with no shared memory. When a client sends a request with an `mcp-session-id` that was created on a different instance, FastMCP returns `HTTP 404 / {"error": {"message": "Session not found"}}`. The fix is applied in `mcp_server/server.py` `run_http()`:
    ```python
    self._mcp.settings.stateless_http = True
    ```
    In stateless mode, FastMCP creates a fresh transport per request — no session ID is issued or required. This is the correct mode for serverless deployments. Security is unaffected: Cloud Run IAM enforces authentication on every request at the infrastructure layer before the container is reached. All 4 Cortex MCP tools are read-only and stateless by design, so no session continuity is needed.

## CLI Commands

```bash
# Extract a single repo (all service metadata passed as CLI flags)
uv run cortex extract \
  --repo-path PATH --repo-name NAME \
  --storage-backend local --storage-bucket DIR \
  --type android --owner team-mobile --domain payments \
  --tier critical --purpose "Main banking app"

# Aggregate all manifests into graph
uv run cortex aggregate --storage-backend local --storage-bucket DIR

# Print run report
uv run cortex report --storage-backend local --storage-bucket DIR

# Run full pipeline locally (extract all → aggregate → report)
uv run cortex run-local --config config/repos-fixtures.yaml --output-dir ./cortex-output

# Start MCP server — stdio mode (for local AI agents like Kilo)
uv run cortex mcp-server --mode stdio --storage-backend local --storage-bucket ./cortex-output

# Start MCP server — streamable-HTTP mode (serves POST /mcp on port 8000)
uv run cortex mcp-server --mode http --storage-backend local --storage-bucket ./cortex-output
```

## MCP Server Transports

| Mode | Transport | Endpoint | Used by |
|------|-----------|----------|---------|
| `stdio` | stdio | — | Local AI agents spawning the process |
| `http` | Streamable HTTP (MCP 2025-03-26) | `POST /mcp` | Kilo (`type: remote`), Cloud Run, network-connected agents |

The HTTP mode calls `self._mcp.streamable_http_app()` (FastMCP built-in). The same `run_http()` method runs both locally and in the Cloud Run container (`python -m mcp_server`).

### Kilo config (`kilo.json`)

```json
{
  "mcp": {
    "cortex": {
      "type": "remote",
      "url": "http://localhost:8000/mcp",
      "enabled": true
    }
  }
}
```

For Cloud Run, replace the URL with the Cloud Run service URL:
```json
"url": "https://{cortex-cloud-run-url}/mcp"
```

## GCP Cloud Run Deployment

The MCP server runs on Cloud Run in production, reading from Firestore. The `Dockerfile` CMD (`python -m mcp_server`) calls `server.run_http()` — the same streamable-http method used locally.

### Current Deployment

| Property | Value |
|---|---|
| **GCP Project** | `prj-ai-flow-orchestrator-gp-gc` |
| **Cloud Run service** | `cortex` |
| **Region** | `us-central1` |
| **Service URL** | `https://cortex-2gqh4rrbwa-uc.a.run.app` |
| **Firestore database** | `cortex` (named database) |

### Security (all at infrastructure level — no app-level auth)

| Layer | Mechanism |
|-------|-----------|
| HTTPS | Cloud Run terminates TLS — container speaks plain HTTP on 8080 |
| No public access | `--no-allow-unauthenticated` — requires valid Google identity token |
| Ingress | `--ingress all` — external HTTPS allowed; IAM enforces auth on every request |
| IAM invocation | `roles/run.invoker` granted only to authorized service accounts / users |
| Minimal SA | Container SA: `datastore.user`, `secretmanager.secretAccessor`, `logging.logWriter` only |

### Deploy

```bash
export GCP_PROJECT_ID=your-project-id
./scripts/deploy.sh   # idempotent — safe to re-run on every update
```

### Upload data

```bash
uv run cortex run-local --config config/repos-real.yaml --output-dir ./cortex-output
./scripts/upload-to-firestore.sh ./cortex-output
```

### Storage backends

| Backend | When | Key config |
|---------|------|------------|
| `local` | Local dev | `--storage-bucket ./cortex-output` |
| `gcs` | Azure DevOps pipeline | `--storage-bucket gs://...` |
| `firestore` | Cloud Run (production) | `FIRESTORE_DATABASE=cortex` env var |

### Operational Runbook

#### Connect to Cloud Run MCP server (developer access)

```bash
# 1. Start the authenticated proxy tunnel (keeps running in foreground)
gcloud run services proxy cortex \
  --region=us-central1 \
  --project=prj-ai-flow-orchestrator-gp-gc \
  --port=3128

# 2. Configure kilo.json to point at the proxy
# "url": "http://localhost:3128/mcp"
```

#### Grant a new user access

```bash
gcloud run services add-iam-policy-binding cortex \
  --region=us-central1 \
  --project=prj-ai-flow-orchestrator-gp-gc \
  --member="user:name.surname@globant.com" \
  --role="roles/run.invoker"

gcloud run services add-iam-policy-binding cortex \
  --region=us-central1 \
  --project=prj-ai-flow-orchestrator-gp-gc \
  --member="user:name.surname@globant.com" \
  --role="roles/run.viewer"
```

#### Grant a service account access (for AI agents)

```bash
gcloud run services add-iam-policy-binding cortex \
  --region=us-central1 \
  --project=prj-ai-flow-orchestrator-gp-gc \
  --member="serviceAccount:my-sa@prj-ai-flow-orchestrator-gp-gc.iam.gserviceaccount.com" \
  --role="roles/run.invoker"
```

#### Revoke access

```bash
gcloud run services remove-iam-policy-binding cortex \
  --region=us-central1 \
  --project=prj-ai-flow-orchestrator-gp-gc \
  --member="user:name.surname@globant.com" \
  --role="roles/run.invoker"
```

#### List all principals with access

```bash
gcloud run services get-iam-policy cortex \
  --region=us-central1 \
  --project=prj-ai-flow-orchestrator-gp-gc
```

#### Verify connectivity (smoke test)

```bash
TOKEN=$(gcloud auth print-identity-token)
curl -s -o /dev/null -w "%{http_code}" \
  -X POST https://cortex-2gqh4rrbwa-uc.a.run.app/mcp \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","method":"initialize","id":1,"params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"test","version":"0.1"}}}'
# Expected: 200
# 403 = no token / not authenticated
# 401 = invalid token
```

#### View Cloud Run logs

```bash
gcloud logging read \
  'resource.type="cloud_run_revision" AND resource.labels.service_name="cortex"' \
  --project=prj-ai-flow-orchestrator-gp-gc \
  --limit=50 \
  --format="table(timestamp,textPayload)"
```

## How `run-local` Works

The `cortex run-local` command reads all service metadata from the repos config YAML. No `service.yaml` is required in target repos.

```yaml
repos:
  # Local path (no credentials needed)
  - name: my-android-app
    path: /Users/you/projects/my-android-app
    type: android
    owner: team-mobile
    domain: payments
    tier: critical
    purpose: Main Android banking app

  # Remote URL (requires AZURE_PAT env var)
  - name: my-ios-app
    url: https://dev.azure.com/org/project/_git/my-ios-app
    type: ios
    owner: team-mobile
    domain: payments
    tier: critical
    purpose: Main iOS banking app

  # Remote URL on a specific branch
  - name: my-android-staging
    url: https://dev.azure.com/org/project/_git/my-android-app
    branch: develop          # optional — omit to clone the default branch
    type: android
    owner: team-mobile
    domain: payments
    tier: standard
    purpose: Staging Android app on the develop branch
```

- Each entry must have `path` OR `url`, never both.
- Required fields per entry: `name`, `path`/`url`, `type`, `owner`, `domain`, `tier`, `purpose`.
- Optional fields: `status`, `slack`, `runbook`, `jira_component`, `keywords`, `integration_notes`, `extractor_hints`, `branch`.
- For `url` entries: reads `AZURE_PAT` from environment, shallow-clones to a temp dir, extracts, then cleans up.
- If `AZURE_PAT` is missing and a `url` entry is encountered → immediate failure with clear message.

## How to Add a New Extractor

1. Create `src/cortex/extractors/{type}.py` implementing the `Extractor` base class from `base.py`.
2. Register it in `src/cortex/extractors/__init__.py` by adding to the `registry` dict.
3. Create a test fixture in `tests/fixtures/sample-{type}-repo/` with realistic repo contents.
4. Write tests in `tests/test_{type}_extractor.py`.
5. Add a sample entry to `config/repos-fixtures.yaml` with the new type's metadata.
6. Run the full test suite and smoke test.

## Anti-Requirements (DO NOT implement)

1. No graph database — JSON files only
2. No embeddings or vector search — keyword matching only
3. No LLM-based extraction — deterministic parsing only
4. No webhook receiver — scheduled pipeline only
5. No service.yaml in target repos — all metadata lives in the repos config
6. No fifth MCP tool
7. No web UI
8. No MCP server auth
9. No Confluence sync
10. No agents.md support inside extracted repos

## Schemas and Models

- `schemas/service.schema.json` — Defines the contract for service metadata (reference)
- `schemas/manifest.schema.json` — Defines the contract for `manifest.json` (output)
- `src/cortex/schema.py` — Pydantic v2 models matching both schemas. Key models:
  - `ServiceYaml` — validated service metadata (sourced from repos config, not a file)
  - `ServiceManifest` — extractor output (includes backend-Java-specific fields like `spring_boot_version`, `java_version`, `kafka_topics`, `outbound_calls`, `api_calls`, etc.)
  - `PlatformGraph` — aggregated graph
  - `ExtractionError` — error record for failed extractions
  - `OutboundCall` / `ApiCall` — HTTP call metadata extracted from backend code
  - `ServiceEdge` / `CommunicationGraph` — inter-service communication graph
  - `ModuleInfo` — multi-module project structure metadata

When modifying schemas, update **both** the JSON Schema file and the corresponding Pydantic model in `schema.py`. They must stay in sync.

## Storage Layout

After running `cortex run-local`, the output directory contains:

```
cortex-output/
├── graph/
│   ├── latest.json              # Aggregated graph (the index)
│   └── {timestamp}.json         # Timestamped snapshots
├── services/
│   ├── {repo-name}/
│   │   ├── manifest.json        # Successful extraction output
│   │   └── extraction-error.json # Only present on failure
├── runs/
│   └── {timestamp}.json         # Run summaries
└── logs/
    └── mcp/
        └── {date}.jsonl         # MCP query logs
```

# Notes
- Always use Clean Architecture Patterns
- in GCP, Cortex lives along other services, so be careful to never delete content there.