# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

> `AGENTS.md` is the canonical, exhaustive reference (full CLI flags, GCP runbook, design-decision rationale, anti-requirements). Read it for operational detail. This file is the orientation layer. When the two disagree, `AGENTS.md` wins — keep them in sync.

## What this is

Platform Cortex extracts structured architectural metadata from Android, iOS, and backend Java (Spring Boot) repos, aggregates it into a queryable JSON graph, and serves it to AI agents over an MCP server. It is a Python 3.12+ project with two installable packages: the `cortex` CLI (`src/cortex`) and the `mcp_server`.

## Common commands

All commands run through `uv`.

```bash
uv sync --extra dev                      # install (add --extra ios for pbxproj support)

# Tests
uv run pytest tests/ mcp_server/tests/ -v
uv run pytest --cov=cortex tests/ mcp_server/tests/ -v   # coverage must stay > 75% (see pyproject fail_under)
uv run pytest tests/test_android_extractor.py -v         # single file
uv run pytest tests/test_android_extractor.py::test_name -v  # single test

uv run ruff check src/ tests/ mcp_server/                # lint
uv run mypy src/                                         # type-check (strict mode)

# End-to-end smoke test against fixtures (extract → aggregate → report)
uv run cortex run-local --config config/repos-fixtures.yaml --output-dir /tmp/cortex-smoke
```

After any code change, run the test suite + the smoke test + ruff. The smoke test exercises the full pipeline against `tests/fixtures/` and is the fastest way to catch integration regressions.

## Pipeline architecture (the big picture)

Data flows through three stages, all reachable from `src/cortex/cli.py`:

1. **Extract** — `extractors/` parse one repo into a `ServiceManifest`. The registry in `extractors/__init__.py` maps a repo `type` string → extractor class (`android`, `ios`, `backend-java`). There is **no auto-detection** — `type` from the config is the sole source of truth. All extractors implement the abstract `Extractor` in `extractors/base.py`.
2. **Aggregate** — `aggregator.py` merges all per-service manifests into `graph/latest.json`, the index the MCP server reads.
3. **Serve** — `mcp_server/server.py` exposes the graph through MCP tools.

`run-local` chains extract → aggregate → report for every repo in a config file.

### Service metadata lives in the config, not in target repos

A critical and unusual decision: there is **no `service.yaml` in extracted repos**. Every `ServiceYaml` field (`type`, `owner`, `domain`, `tier`, `purpose`, …) is declared inline per entry in `config/repos*.yaml`. Each entry has `path` OR `url` (never both); `url` entries shallow-clone using `AZURE_PAT`. Use `config/repos-fixtures.yaml` for tests, `repos-real.yaml`/`repos-local.yaml` for local runs, `repos.yaml` for the pipeline.

### Storage backends

`storage.py` defines a `StorageBackend` ABC with three implementations — `local` (dev), `gcs` (Azure DevOps pipeline), and `firestore` (Cloud Run production, in `firestore_storage.py`). Output layout is documented in `AGENTS.md` (`graph/`, `services/`, `runs/`, `logs/`). Extraction is **fail-soft**: one repo failing writes `services/{name}/extraction-error.json` and never blocks the others.

### Schemas and models must stay in sync

`schemas/*.schema.json` (JSON Schema) and the Pydantic v2 models in `src/cortex/schema.py` describe the same contracts. When you change one, change both. `validation.py` validates metadata against both.

## MCP server

`mcp_server/server.py` is a FastMCP server exposing **exactly 4 read-only tools** — `find_relevant_services`, `list_endpoints`, `get_service_context`, `get_endpoint_contract`. **Never add a 5th tool** (hard constraint). `get_endpoint_contract` returns "no API spec available" for mobile types but stays implemented.

Two transports, both via the `mcp-server` CLI command:
- `--mode stdio` — local agents that spawn the process.
- `--mode http` — streamable-HTTP (MCP 2025-03-26 spec), serves a single `POST /mcp`. Same `run_http()` runs locally and in the Cloud Run container (`python -m mcp_server`).

For Cloud Run, `run_http()` must set `stateless_http=True` and disable FastMCP DNS-rebinding protection — both are required because Cloud Run is serverless and rewrites the `Host` header. See `AGENTS.md` decisions #10–#11 before touching `run_http()`. There is **no app-level auth**; security is entirely Cloud Run IAM.

## Adding an extractor

1. New `extractors/{type}.py` subclassing `Extractor`. 2. Register in `extractors/__init__.py`. 3. Add fixture `tests/fixtures/sample-{type}-repo/`. 4. Add `tests/test_{type}_extractor.py`. 5. Add an entry to `config/repos-fixtures.yaml`. 6. Run full tests + smoke test.

## Do NOT implement

No graph DB (JSON only), no embeddings/vector search (keyword matching only), no LLM-based extraction (deterministic parsing only), no `service.yaml` in target repos, no 5th MCP tool, no web UI, no MCP auth, no webhook receiver. Full list in `AGENTS.md`. Follow Clean Architecture patterns. In GCP, Cortex shares a project with other services — never delete unrelated content there.
