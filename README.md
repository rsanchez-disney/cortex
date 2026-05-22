# Cortex

Structured architectural metadata extraction, aggregation, and MCP serving for cross-repo AI agent context.

> 💡 **New here?** Start with the [Vision & Value](docs/presentation.md) document — it explains the problem we're solving, the guiding principles behind Cortex, and why this is a key component of any real agentic AI solution.

## What is Cortex?

Cortex is a **living architectural knowledge graph** — the foundational context layer that makes AI agents reliable on a real platform.

AI agents are only as good as the context they receive. Static context packs, Confluence pages, and architecture docs get outdated the moment code is merged. Without current, accurate architectural knowledge, agents hallucinate: they call deprecated endpoints, miss inter-service dependencies, and break contracts they never knew existed. Cortex solves this by going straight to the **sources of truth**.

### Architecture, Not Code

Cortex is not a code indexer. It operates at a **layer above the code**, extracting the architectural signal that matters for decision-making — what modules exist, how they depend on each other, what endpoints a service exposes, what Kafka topics it produces and consumes, what DTOs define its contracts, and how services communicate across the platform. Agents get the structural understanding they need, without drowning in implementation details.

### Sources of Truth

Cortex derives its knowledge only from authoritative sources:

- **📦 The Code** *(fully implemented)* — Deterministic parsing of Android, iOS, and Spring Boot repositories. No LLMs, no guessing — exactly what the code says.
- **🌐 Live APIs** *(partially implemented)* — Swagger/OpenAPI contract references extracted and surfaced via the MCP server.
- **📊 Production Usage** *(next frontier)* — Observability data (e.g. Datadog) representing real traffic patterns and actual runtime behavior.

### Three components

1. **Extractor** (`cortex` CLI) — Parses build files, manifests, and source code from each repo to produce normalized architectural metadata per service.
2. **Pipeline** — Azure DevOps scheduled job that runs extraction across all repos and writes results to cloud storage, keeping the graph always up-to-date.
3. **MCP Server** — Exposes 4 query tools over the aggregated graph for consumption by any MCP-compatible AI agent.

It answers questions like:

- "Which services are involved in the payment flow?"
- "Does the backend expose a login endpoint?"
- "What Kafka topics does the orders service produce?"
- "What are the module dependencies of the Android app?"

## Quick Start

### Prerequisites

- Python 3.12+
- [uv](https://github.com/astral-sh/uv) package manager

### Installation

```bash
uv sync --extra dev
# For iOS extractor support (pbxproj parsing):
uv sync --extra ios --extra dev
```

### Local Development Workflow (Primary)

The primary development workflow uses `cortex run-local` to run the full extract → aggregate → report pipeline locally.

#### 1. Smoke test with fixtures

```bash
# Run against built-in test fixtures (no PAT needed)
uv run cortex run-local --config config/repos-fixtures.yaml --output-dir ./cortex-output
```

#### 2. Clone real repos and run locally (recommended)

Use `cortex clone-repos` to shallow-clone all repos from `config/repos-real.yaml` into a local `.repos/` directory and auto-generate `config/repos-local.yaml` pointing at those clones:

```bash
# Set your Azure DevOps PAT
export AZURE_PAT=your-pat-here

# Clone all repos and generate repos-local.yaml
uv run cortex clone-repos
```

This will:
- Shallow-clone each unique URL from `config/repos-real.yaml` into `.repos/<repo-name>/`
- Deduplicate shared URLs (e.g. two services sharing one iOS repo clone once)
- Overwrite `config/repos-local.yaml` with `path` entries pointing at the clones
- Preserve all service metadata (owner, domain, keywords, extractor_hints, etc.)

Then run the pipeline against the local clones:

```bash
uv run cortex run-local --config config/repos-local.yaml --output-dir ./cortex-output
```

To refresh clones (e.g. after upstream changes), re-run `cortex clone-repos` — existing clones are replaced with fresh shallow clones.

**Custom options:**

```bash
uv run cortex clone-repos \
  --config config/repos-real.yaml \
  --clone-dir .repos \
  --output-config config/repos-local.yaml
```

#### 3. Manual repos-local.yaml (alternative)

Instead of using `clone-repos`, you can manually edit `config/repos-local.yaml` to point at your own local repo clones:

```yaml
repos:
  - name: mobile-banking-android
    path: /Users/you/projects/mobile-banking-android
    type: android
    owner: team-mobile
    domain: payments
    tier: critical
    purpose: Main Android banking app
  - name: mobile-banking-ios
    path: /Users/you/projects/mobile-banking-ios
    type: ios
    owner: team-mobile
    domain: payments
    tier: critical
    purpose: Main iOS banking app
```

```bash
uv run cortex run-local --config config/repos-local.yaml --output-dir ./cortex-output
```

#### 4. Clone from Azure DevOps URLs (inline)

For one-off runs without persistent clones, use URL entries directly with `AZURE_PAT` (repos are cloned to a temp dir and cleaned up after):

```yaml
repos:
  - name: mobile-banking-ios
    url: https://dev.azure.com/org/project/_git/mobile-banking-ios
    type: ios
    owner: team-mobile
    domain: payments
    tier: critical
    purpose: Main iOS banking app
```

```bash
AZURE_PAT=your-pat-here uv run cortex run-local --config config/repos-local.yaml --output-dir ./cortex-output
```

#### 5. Start MCP server locally

The MCP server supports two transport modes:

**stdio** (for local AI agent use — e.g. Kilo in stdio mode):
```bash
uv run cortex mcp-server --mode stdio --storage-backend local --storage-bucket ./cortex-output
```

**streamable-http** (for remote/network use — serves at `POST /mcp`):
```bash
uv run cortex mcp-server --mode http --storage-backend local --storage-bucket ./cortex-output
# Default port: 8000 → http://localhost:8000/mcp
```

### Individual Commands

```bash
# Extract a single repo (all service metadata passed as CLI flags)
uv run cortex extract \
  --repo-path ./my-repo --repo-name my-repo \
  --storage-backend local --storage-bucket ./output \
  --type android --owner team-mobile --domain payments \
  --tier critical --purpose "Main banking app"

# Aggregate all manifests into a graph
uv run cortex aggregate --storage-backend local --storage-bucket ./output

# Generate a run report
uv run cortex report --storage-backend local --storage-bucket ./output
```

## Architecture

```
Source repos
    │
    │  (scheduled or run-local)
    ▼
cortex extract (per repo, parallel)
    │
    ▼
cortex aggregate (once)
    │
    ▼
Storage (local filesystem / GCS / Firestore)
    ├── graph/latest.json
    ├── services/{name}/manifest.json
    └── runs/{timestamp}.json
    │
    ▼
MCP Server (4 tools)
    │
    ▼
AI Agents
```

### Data Flow

1. Service metadata (type, owner, domain, purpose, etc.) is declared in the repos config YAML — no `service.yaml` is needed in target repos.
2. The extractor reads ecosystem-specific files (build.gradle, Package.swift, pom.xml, etc.) from each repo.
3. Output: normalized `manifest.json` per repo.
4. The aggregator merges all manifests into `graph/latest.json`.
5. The MCP server reads the graph and serves queries.

## MCP Server Transports

| Mode | Transport | Endpoint | Use case |
|------|-----------|----------|----------|
| `stdio` | stdio | — | Local AI agents (e.g. Kilo spawning the process) |
| `http` | Streamable HTTP (MCP 2025-03-26) | `POST /mcp` | Remote clients, Cloud Run, network-connected agents |

The HTTP mode uses FastMCP's `streamable_http_app()` — a single `/mcp` endpoint that handles both request/response and server-sent event streaming. This replaces the legacy SSE transport (`/sse` + `/messages/`).

### Connecting Kilo (local)

In `kilo.json`, use `type: remote` to connect to the local HTTP server:

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

Start the server before launching Kilo:
```bash
uv run cortex mcp-server --mode http --storage-backend local --storage-bucket ./cortex-output
```

## GCP Cloud Run Deployment

The MCP server is deployed to GCP Cloud Run for production use by AI agents. The same `run_http()` method used locally runs inside the container — the upgrade to streamable-http transport applies automatically.

### Architecture on Cloud Run

```
AI agent (authenticated SA)
    │  POST https://{cortex-url}/mcp  (OIDC token in Authorization header)
    ▼
Cloud Run ingress (internal-only, IAM-authenticated)
    │  HTTPS → HTTP:8080
    ▼
Container: python -m mcp_server
    │  server.run_http(host="0.0.0.0", port=8080)
    ▼
FastMCP streamable_http_app() → /mcp endpoint
    │
    ▼
Firestore (named database "cortex") — reads graph/latest.json + manifests
```

### Current Deployment

| Property | Value |
|---|---|
| **GCP Project** | `prj-ai-flow-orchestrator-gp-gc` |
| **Cloud Run service** | `cortex` |
| **Region** | `us-central1` |
| **Service URL** | `https://cortex-2gqh4rrbwa-uc.a.run.app` |
| **Firestore database** | `cortex` (named database, ~30 services) |

### Security layers

| Layer | Mechanism |
|-------|-----------|
| HTTPS | Cloud Run terminates TLS automatically — container speaks plain HTTP on 8080 |
| No public access | `--no-allow-unauthenticated` — every request requires a valid Google identity token |
| Ingress | `--ingress all` — Cloud Run accepts external HTTPS; IAM still enforces auth on every request |
| IAM invocation | `roles/run.invoker` granted only to authorized service accounts / users |
| Minimal SA permissions | Container SA has only `datastore.user`, `secretmanager.secretAccessor`, `logging.logWriter` |

No application-level auth in the MCP server code — Cloud Run handles authentication and authorization entirely at the infrastructure layer.

> **Note on `--ingress all` vs `--ingress internal`:** The service uses `--ingress all` (not `internal`) so that human developers and AI agents outside the GCP VPC can reach it via `gcloud run services proxy` or direct HTTPS. `--no-allow-unauthenticated` remains enforced, so every request still requires a valid Google identity token — there is no public anonymous access.

### Connecting to Cloud Run (Remote MCP)

Developers and AI agents access the Cloud Run MCP server via `gcloud run services proxy`, which creates a local tunnel authenticated with your Google identity:

```bash
# Start the authenticated local proxy (port 3128 → Cloud Run)
gcloud run services proxy cortex \
  --region=us-central1 \
  --project=prj-ai-flow-orchestrator-gp-gc \
  --port=3128
```

Then configure Kilo (or any MCP client) to use the local proxy URL:

```json
{
  "mcp": {
    "cortex": {
      "type": "remote",
      "url": "http://localhost:3128/mcp",
      "enabled": true
    }
  }
}
```

The proxy transparently forwards requests with your OIDC identity token — no manual token management needed.

### Access Management

All access is controlled via `roles/run.invoker` on the Cloud Run service. **No code changes required** — just IAM updates.

#### Grant access to a user

```bash
# Grant a developer access to the Cloud Run MCP server
gcloud run services add-iam-policy-binding cortex \
  --region=us-central1 \
  --project=prj-ai-flow-orchestrator-gp-gc \
  --member="user:name.surname@globant.com" \
  --role="roles/run.invoker"
```

#### Grant access to a service account (for automated agents)

```bash
# Grant an AI agent service account access
gcloud run services add-iam-policy-binding cortex \
  --region=us-central1 \
  --project=prj-ai-flow-orchestrator-gp-gc \
  --member="serviceAccount:my-agent-sa@prj-ai-flow-orchestrator-gp-gc.iam.gserviceaccount.com" \
  --role="roles/run.invoker"
```

#### Revoke access

```bash
# Revoke a user's access
gcloud run services remove-iam-policy-binding cortex \
  --region=us-central1 \
  --project=prj-ai-flow-orchestrator-gp-gc \
  --member="user:name.surname@globant.com" \
  --role="roles/run.invoker"
```

#### List current authorized principals

```bash
gcloud run services get-iam-policy cortex \
  --region=us-central1 \
  --project=prj-ai-flow-orchestrator-gp-gc
```

#### Verify your own access

```bash
# Should return HTTP 200 with MCP JSON response
TOKEN=$(gcloud auth print-identity-token)
curl -s -o /dev/null -w "%{http_code}" \
  -X POST https://cortex-2gqh4rrbwa-uc.a.run.app/mcp \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","method":"initialize","id":1,"params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"test","version":"0.1"}}}'
# Expected: 200
```

### Deploy

```bash
# One-time setup and every update (idempotent)
export GCP_PROJECT_ID=your-project-id
./scripts/deploy.sh
```

The script creates (if needed) and deploys: Artifact Registry repo, service account + IAM bindings, Firestore database (`cortex`), and the Cloud Run service.

### Upload data to Firestore

After running `cortex run-local` locally, push the extracted graph and manifests to Firestore:

```bash
uv run cortex run-local --config config/repos-real.yaml --output-dir ./cortex-output
./scripts/upload-to-firestore.sh ./cortex-output
```

### Storage backends

| Backend | Used when | Config |
|---------|-----------|--------|
| `local` | Local dev | `--storage-bucket ./cortex-output` |
| `gcs` | Azure DevOps pipeline | `--storage-bucket gs://my-bucket` |
| `firestore` | Cloud Run (production) | `FIRESTORE_DATABASE=cortex` env var |

## Adding a New Repo

1. Add an entry to `config/repos.yaml` (for pipeline) or `config/repos-local.yaml` (for local dev).
2. Include all required service metadata fields inline in the config entry.
3. Run `uv run cortex run-local` to verify extraction succeeds.

### Required Fields per Config Entry

```yaml
name: my-service              # kebab-case, unique
path: /path/to/repo           # OR url: https://dev.azure.com/...
type: android                 # android | ios | backend-java
owner: team-name
domain: mobile                # high-level area (payments, identity, etc.)
tier: standard                # critical | standard | experimental | deprecated
purpose: >
  One to three sentences describing the service.
```

Optional fields: `status`, `slack`, `runbook`, `jira_component`, `keywords`, `integration_notes`, `extractor_hints`, `branch`, `swagger_url`.

### Adding a Backend-Java Service with Swagger URL

All `backend-java` services in the IntuitDome platform expose live Swagger/OpenAPI docs hosted on Azure App Service. Add the `swagger_url` field directly in the config entry — it is pre-computed (no runtime IaC parsing required).

**Formula (Dev environment default):**

```
https://asv{region}dev{webapp_name}.azurewebsites.net/{webapp_name}/v3/api-docs
```

Where `{webapp_name}` is the Azure App Service name (e.g. `identity`, `payments`) and `{region}` is:

| Region acronym | Azure region | Used by |
|---|---|---|
| `aw3` | West US 3 (`westus3`) | Most services |
| `awu` | West US (`westus`) | `identity`, `ambientcontrol` (app_service_index=5 in IaC) |

**To determine the region for a new service**, look up `app_service_<name>` in the IaC `terraform.tfvars`. If the index resolves to `app_services_list[5]` or `app_services_list[6]` (both `westus`), use `awu`; otherwise use `aw3`.

**Example config entry:**

```yaml
- name: my-new-microservice
  url: https://dev.azure.com/IntuitDome/my-new-microservice/_git/my-new-microservice
  type: backend-java
  owner: team-backend
  domain: my-domain
  tier: standard
  purpose: Short description of the service.
  swagger_url: https://asvaw3devmynewmicroservice.azurewebsites.net/mynewmicroservice/v3/api-docs
```

The `swagger_url` flows automatically from the config into the extracted manifest and is surfaced by the `get_endpoint_contract` MCP tool when no OpenAPI spec file is present.

See `schemas/service.schema.json` for the full schema.

## Supported Extractors

| Type | Language Detection | Dependencies | Build Config | Manifest/Config |
|---|---|---|---|---|
| `android` | .kt/.java counts | build.gradle(.kts), libs.versions.toml | SDK versions, applicationId | AndroidManifest.xml (permissions, activities) |
| `ios` | .swift/.m counts | Package.swift, Podfile, Cartfile | Swift version, bundle ID, deployment target | Info.plist, *.entitlements |
| `backend-java` | .java/.kt counts | pom.xml, build.gradle | Spring Boot version, Java version | API endpoints, Kafka topics, outbound calls, database config |

Other backend extractors (Go, Node, React) are deferred.

## MCP Server Tools

| Tool | Purpose |
|---|---|
| `find_relevant_services(task_description, max_results)` | Keyword-based service discovery |
| `list_endpoints(service)` | Endpoint index for a service |
| `get_service_context(name, include)` | Deep context on a single service |
| `get_endpoint_contract(service, method, path)` | Endpoint schema (deferred for mobile) |

## Pipeline Setup (Azure DevOps)

### Variable Group

Create a `platform-cortex-secrets` variable group linked to Azure Key Vault with:

- **`GIT_PAT`** — Read-only PAT for cloning source repos (same value as `AZURE_PAT` used locally).
- **`STORAGE_CREDENTIALS`** — GCS service account JSON for writing to the storage bucket.

### Running the Pipeline

The pipeline runs nightly at 3am UTC. To run manually:

1. Go to Azure DevOps → Pipelines → Platform Cortex
2. Click "Run pipeline"
3. Optionally set `repoFilter` to extract a single repo
4. Click "Run"

### AZURE_PAT vs GIT_PAT

| Context | Env Var | Source |
|---|---|---|
| Local (`cortex run-local`) | `AZURE_PAT` | Set manually in shell |
| Azure DevOps pipeline | `GIT_PAT` | Key Vault → variable group |

Same PAT value, different injection mechanism. The extractor code never touches credentials — cloning is handled by `run-local` or the pipeline orchestration layer.

## Testing

```bash
# Run all tests
uv run pytest tests/ mcp_server/tests/ -v

# Run with coverage
uv run pytest --cov=cortex tests/ mcp_server/tests/ -v

# Run specific test file
uv run pytest tests/test_android_extractor.py -v
```

## Troubleshooting

### Extraction fails for a repo

1. Check the repos config entry has all required fields (`name`, `type`, `owner`, `domain`, `tier`, `purpose`).
2. Check the extractor supports the declared `type` (currently: `android`, `ios`, `backend-java`).
3. Check `services/{name}/extraction-error.json` for the specific error.

### Graph is stale

1. Check that the pipeline ran recently (Azure DevOps → Pipelines).
2. Check `graph/latest.json` exists and its `metadata.timestamp` is recent.
3. Check storage permissions (GCS IAM or local filesystem).

### MCP server returns empty results

1. Verify `graph/latest.json` exists: `uv run cortex report --storage-backend local --storage-bucket ./cortex-output`
2. Re-run the pipeline: `uv run cortex run-local --config config/repos-fixtures.yaml --output-dir ./cortex-output`
3. Restart the MCP server.

### AZURE_PAT errors

If you see `"AZURE_PAT environment variable required for cloning repo..."`:
- Set it: `export AZURE_PAT=your-pat-here`
- Or switch the repo entry from `url` to `path` (use a pre-cloned local directory)
- Or use `config/repos-fixtures.yaml` which only uses local paths

### New repo not appearing in graph

1. Verify the entry in `config/repos.yaml` or `config/repos-local.yaml` has all required fields.
2. Ensure the `type` matches a registered extractor (`android`, `ios`, `backend-java`).
3. Run `cortex run-local` and check for errors in the output.

## Project Structure

```
memory-hub/
├── pyproject.toml
├── Dockerfile                   # Cloud Run container (streamable-HTTP mode, port 8080)
├── scripts/
│   ├── deploy.sh                # Idempotent GCP Cloud Run deploy
│   └── upload-to-firestore.sh   # Push local cortex-output to Firestore
├── src/cortex/
│   ├── cli.py              # CLI entry point (typer)
│   ├── schema.py            # Pydantic models
│   ├── validation.py        # Service metadata validation
│   ├── storage.py           # Storage backend (local + GCS + Firestore)
│   ├── firestore_storage.py # FirestoreStorageBackend
│   ├── aggregator.py        # Graph aggregation
│   ├── repo_cloner.py       # Repo cloning & local config generation
│   └── extractors/
│       ├── base.py          # Abstract extractor
│       ├── android.py       # Android extractor
│       ├── ios.py           # iOS extractor
│       └── backend_java.py  # Backend Java (Spring Boot) extractor
├── mcp_server/
│   ├── server.py            # MCP server (4 tools, stdio + streamable-http)
│   ├── __main__.py          # Cloud Run entry point (python -m mcp_server)
│   └── tests/
├── tests/
│   ├── fixtures/            # Sample repos for testing (android, ios, ios-multitarget, backend-java)
│   ├── test_*.py
├── schemas/                 # JSON Schemas
├── config/                  # Repo registry configs
└── pipelines/               # Azure DevOps pipeline
```

## Open Questions

1. **Storage backend default:** Currently defaulting to GCS. Consider Azure Blob if needed.
2. **Secret management:** Azure Key Vault configured. GCP Secret Manager is an alternative.
3. **Monorepo support:** If any repos contain multiple services, the schema would need a `services:` array variant.
4. **Repos config CI enforcement:** A shared GitHub Action / Azure template for validating repos config entries is a useful follow-up.
