# AGENTS.md — Platform Atlas

## What This Project Is

Platform Atlas extracts structured architectural metadata from Android, iOS, and backend Java (Spring Boot) repositories, aggregates it into a queryable graph, and exposes it to AI agents via an MCP server. It is a Python CLI tool (`atlas`) with a separate MCP server component.

## Setup

```bash
# Install all dependencies (Python 3.12+ required)
uv sync --extra dev

# Optional: iOS pbxproj parsing support
uv sync --extra ios --extra dev
```

The CLI entry point is `atlas` (defined in `pyproject.toml` as `atlas.cli:app`). Always run via `uv run atlas ...`.

## Project Structure

```
memory-hub/
├── src/atlas/                  # Core package
│   ├── cli.py                  # CLI commands (typer)
│   ├── schema.py               # Pydantic v2 models (ServiceYaml, ServiceManifest, PlatformGraph, etc.)
│   ├── validation.py           # Service metadata validation (JSON Schema + Pydantic)
│   ├── storage.py              # StorageBackend ABC → LocalStorageBackend, GCSStorageBackend
│   ├── aggregator.py           # Merges manifests into graph/latest.json
│   └── extractors/
│       ├── __init__.py          # Extractor registry (type → class)
│       ├── base.py              # Abstract Extractor base class
│       ├── android.py           # Android extractor
│       ├── ios.py               # iOS extractor
│       └── backend_java.py     # Backend Java (Spring Boot) extractor
├── mcp_server/
│   ├── server.py               # MCP server with 4 tools (FastMCP)
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
└── pipelines/
    └── azure-pipelines.yml
```

## After Making Code Changes

Run these commands from the project root to verify changes:

```bash
# 1. Run the full test suite
uv run pytest tests/ mcp_server/tests/ -v

# 2. Run with coverage (must stay above 75%)
uv run pytest --cov=atlas tests/ mcp_server/tests/ -v

# 3. End-to-end smoke test — runs extract → aggregate → report against fixtures
uv run atlas run-local --config config/repos-fixtures.yaml --output-dir /tmp/atlas-smoke

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

1. **No auto-detection (`detect.py` does not exist).** The `type` field in the repos config is the sole source of truth for repo type. The extractor registry in `src/atlas/extractors/__init__.py` maps type → extractor class. If a type has no registered extractor, extraction fails with a clear error.

2. **No Go extractor.** `android`, `ios`, and `backend-java` (Spring Boot) extractors are implemented. Other backend types (Go, Node, React) are deferred.

3. **No Azure Blob storage.** Only `local` and `gcs` storage backends exist.

4. **`get_endpoint_contract` MCP tool** returns "no API spec available" for mobile service types. It is still implemented (hard constraint: exactly 4 MCP tools, never add a 5th).

5. **Service metadata lives in repos config, not in target repos.** All `ServiceYaml` fields (`type`, `owner`, `domain`, `tier`, `purpose`, etc.) are declared inline in each entry of `config/repos*.yaml`. Target repos require no `service.yaml` file. Validation happens at pipeline/extraction time from the config dict.

6. **Fail-soft per repo.** One repo's extraction failure must NOT block others. Errors are written to `services/{name}/extraction-error.json`.

## CLI Commands

```bash
# Extract a single repo (all service metadata passed as CLI flags)
uv run atlas extract \
  --repo-path PATH --repo-name NAME \
  --storage-backend local --storage-bucket DIR \
  --type android --owner team-mobile --domain payments \
  --tier critical --purpose "Main banking app"

# Aggregate all manifests into graph
uv run atlas aggregate --storage-backend local --storage-bucket DIR

# Print run report
uv run atlas report --storage-backend local --storage-bucket DIR

# Run full pipeline locally (extract all → aggregate → report)
uv run atlas run-local --config config/repos-fixtures.yaml --output-dir ./atlas-output

# Start MCP server (stdio mode)
uv run atlas mcp-server --mode stdio --storage-backend local --storage-bucket ./atlas-output
```

## How `run-local` Works

The `atlas run-local` command reads all service metadata from the repos config YAML. No `service.yaml` is required in target repos.

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

1. Create `src/atlas/extractors/{type}.py` implementing the `Extractor` base class from `base.py`.
2. Register it in `src/atlas/extractors/__init__.py` by adding to the `registry` dict.
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
- `src/atlas/schema.py` — Pydantic v2 models matching both schemas. Key models:
  - `ServiceYaml` — validated service metadata (sourced from repos config, not a file)
  - `ServiceManifest` — extractor output (includes backend-Java-specific fields like `spring_boot_version`, `java_version`, `kafka_topics`, `outbound_calls`, `api_calls`, etc.)
  - `PlatformGraph` — aggregated graph
  - `ExtractionError` — error record for failed extractions
  - `OutboundCall` / `ApiCall` — HTTP call metadata extracted from backend code
  - `ServiceEdge` / `CommunicationGraph` — inter-service communication graph
  - `ModuleInfo` — multi-module project structure metadata

When modifying schemas, update **both** the JSON Schema file and the corresponding Pydantic model in `schema.py`. They must stay in sync.

## Storage Layout

After running `atlas run-local`, the output directory contains:

```
atlas-output/
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