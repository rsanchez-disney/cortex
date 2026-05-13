# Platform Atlas

Structured architectural metadata extraction, aggregation, and MCP serving for cross-repo AI agent context.

## What is Platform Atlas?

Platform Atlas extracts structured metadata from source repositories, aggregates it into a queryable graph, and exposes it to AI agents via an MCP server. It answers questions like:

- "Which services are involved in the payment flow?"
- "Does the backend expose a login endpoint?"
- "What are the dependencies of the Android app?"

### Three components

1. **Extractor** (`atlas` CLI) — Parses build files, manifests, and source code from each repo (using service metadata from the repos config) to produce normalized per-service metadata.
2. **Pipeline** — Azure DevOps scheduled job that runs extraction across all repos and writes results to cloud storage.
3. **MCP Server** — Exposes 4 query tools over the aggregated graph for consumption by AI agents.

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

The primary development workflow uses `atlas run-local` to run the full extract → aggregate → report pipeline locally.

#### 1. Smoke test with fixtures

```bash
# Run against built-in test fixtures (no PAT needed)
uv run atlas run-local --config config/repos-fixtures.yaml --output-dir ./atlas-output
```

#### 2. Run against real local repos

Edit `config/repos-local.yaml` to point at your local repo clones:

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
uv run atlas run-local --config config/repos-local.yaml --output-dir ./atlas-output
```

#### 3. Clone from Azure DevOps URLs

For repos not cloned locally, use URL entries with `AZURE_PAT`:

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
AZURE_PAT=your-pat-here uv run atlas run-local --config config/repos-local.yaml --output-dir ./atlas-output
```

#### 4. Start MCP server locally

```bash
# Point at the local pipeline output
uv run atlas mcp-server --mode stdio --storage-backend local --storage-bucket ./atlas-output
```

### Individual Commands

```bash
# Extract a single repo (all service metadata passed as CLI flags)
uv run atlas extract \
  --repo-path ./my-repo --repo-name my-repo \
  --storage-backend local --storage-bucket ./output \
  --type android --owner team-mobile --domain payments \
  --tier critical --purpose "Main banking app"

# Aggregate all manifests into a graph
uv run atlas aggregate --storage-backend local --storage-bucket ./output

# Generate a run report
uv run atlas report --storage-backend local --storage-bucket ./output
```

## Architecture

```
Source repos
    │
    │  (scheduled or run-local)
    ▼
atlas extract (per repo, parallel)
    │
    ▼
atlas aggregate (once)
    │
    ▼
Storage (local filesystem or GCS)
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

## Adding a New Repo

1. Add an entry to `config/repos.yaml` (for pipeline) or `config/repos-local.yaml` (for local dev).
2. Include all required service metadata fields inline in the config entry.
3. Run `uv run atlas run-local` to verify extraction succeeds.

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

Optional fields: `status`, `slack`, `runbook`, `jira_component`, `keywords`, `integration_notes`, `extractor_hints`, `branch`.

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

Create a `platform-atlas-secrets` variable group linked to Azure Key Vault with:

- **`GIT_PAT`** — Read-only PAT for cloning source repos (same value as `AZURE_PAT` used locally).
- **`STORAGE_CREDENTIALS`** — GCS service account JSON for writing to the storage bucket.

### Running the Pipeline

The pipeline runs nightly at 3am UTC. To run manually:

1. Go to Azure DevOps → Pipelines → Platform Atlas
2. Click "Run pipeline"
3. Optionally set `repoFilter` to extract a single repo
4. Click "Run"

### AZURE_PAT vs GIT_PAT

| Context | Env Var | Source |
|---|---|---|
| Local (`atlas run-local`) | `AZURE_PAT` | Set manually in shell |
| Azure DevOps pipeline | `GIT_PAT` | Key Vault → variable group |

Same PAT value, different injection mechanism. The extractor code never touches credentials — cloning is handled by `run-local` or the pipeline orchestration layer.

## Testing

```bash
# Run all tests
uv run pytest tests/ mcp_server/tests/ -v

# Run with coverage
uv run pytest --cov=atlas tests/ mcp_server/tests/ -v

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

1. Verify `graph/latest.json` exists: `uv run atlas report --storage-backend local --storage-bucket ./atlas-output`
2. Re-run the pipeline: `uv run atlas run-local --config config/repos-fixtures.yaml --output-dir ./atlas-output`
3. Restart the MCP server.

### AZURE_PAT errors

If you see `"AZURE_PAT environment variable required for cloning repo..."`:
- Set it: `export AZURE_PAT=your-pat-here`
- Or switch the repo entry from `url` to `path` (use a pre-cloned local directory)
- Or use `config/repos-fixtures.yaml` which only uses local paths

### New repo not appearing in graph

1. Verify the entry in `config/repos.yaml` or `config/repos-local.yaml` has all required fields.
2. Ensure the `type` matches a registered extractor (`android`, `ios`, `backend-java`).
3. Run `atlas run-local` and check for errors in the output.

## Project Structure

```
memory-hub/
├── pyproject.toml
├── src/atlas/
│   ├── cli.py              # CLI entry point (typer)
│   ├── schema.py            # Pydantic models
│   ├── validation.py        # Service metadata validation
│   ├── storage.py           # Storage backend (local + GCS)
│   ├── aggregator.py        # Graph aggregation
│   └── extractors/
│       ├── base.py          # Abstract extractor
│       ├── android.py       # Android extractor
│       ├── ios.py           # iOS extractor
│       └── backend_java.py  # Backend Java (Spring Boot) extractor
├── mcp_server/
│   ├── server.py            # MCP server (4 tools)
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
2. **MCP deployment target:** Cloud Run recommended for parity with existing infrastructure.
3. **Secret management:** Azure Key Vault configured. GCP Secret Manager is an alternative.
4. **Monorepo support:** If any repos contain multiple services, the schema would need a `services:` array variant.
5. **Repos config CI enforcement:** A shared GitHub Action / Azure template for validating repos config entries is a useful follow-up.
