# PRD: Platform Atlas

**Status:** Draft v1.0 **Target implementer:** Claude Code (agentic coder with full repo access) **Timeline:** \~2 weeks to v1 ship

---

## 1\. Overview

### 1.1 Problem

We operate \~12 repositories across three ecosystems (Android, iOS, backend microservices). AI agents working on feature development need cross-repo architectural context to answer questions like:

- *"I need to add a login page to Android — does the backend already expose a login endpoint?"*  
- *"Which services would be affected by changing the payment flow?"*  
- *"What's the contract for `POST /v1/transfers`?"*

Today this context lives in engineers' heads, scattered Confluence pages, and individual READMEs. Agents either hallucinate or require a human to hand-feed context per task.

### 1.2 Solution

Build **Platform Atlas**: a three-part system that extracts structured architectural metadata from all repositories, aggregates it into a queryable graph, and exposes it to agents via an MCP server.

**Three components:**

1. **Extractor** — a Python tool that parses known files in each repo (`service.yaml`, `go.mod`, `build.gradle`, OpenAPI specs, etc.) and produces normalized per-service manifests.  
2. **Pipeline** — an Azure DevOps scheduled job that runs the extractor across all repos and writes results to cloud storage.  
3. **MCP server** — a service that exposes a small set of query operations over the aggregated data for consumption by agents.

### 1.3 Non-goals for v1

Explicitly out of scope — do not implement these:

- Graph database (Neo4j, etc.) — stay on flat JSON files in cloud storage.  
- Vector embeddings / semantic search — use keyword \+ tag matching.  
- Webhook-driven extraction — scheduled runs only.  
- GraphRAG or LLM-based retrieval — deterministic queries only.  
- Code-level search (use Sourcegraph/ripgrep separately).  
- Runtime/observability data ingestion (OTel traces, metrics).  
- Per-endpoint drift detection between spec and code.  
- A UI. MCP \+ logs are the only interfaces.

These are v2+ concerns. Do not pre-build hooks for them.

### 1.4 Success criteria

The v1 is successful when:

1. A scheduled nightly pipeline extracts data from all registered repos with \>95% success rate.  
2. The aggregated graph contains at minimum: service name, type, domain, purpose, dependencies, and endpoint index for every registered repo.  
3. An AI agent can answer "does endpoint X exist in service Y" correctly in under 2 seconds via MCP.  
4. An AI agent can answer "which services are relevant to task Z" and return a ranked list of ≤5 candidates.  
5. Adding a new repo to the system requires only (a) adding an entry to `config/repos.yaml` and (b) ensuring the repo has a valid `service.yaml`.

---

## 2\. Architecture

### 2.1 High-level flow

Source repos (12)

    │

    │  (scheduled, via git clone)

    ▼

Azure DevOps pipeline

    │

    │  runs: atlas extract  (per repo, in parallel)

    │  runs: atlas aggregate (once, at end)

    ▼

Cloud storage bucket

    ├── graph/latest.json                      ← aggregate graph (the index)

    ├── graph/{timestamp}.json                 ← snapshots

    └── services/{repo-name}/

        ├── manifest.json                      ← full per-service record

        ├── openapi.yaml                       ← copied from source repo

        └── proto/\*.proto                      ← copied from source repo

    │

    ▼

MCP server (stateless, reads bucket)

    │

    ▼

Agents (Flow, Claude Code, IDE plugins, etc.)

### 2.2 Component boundaries

**Extractor (`atlas` CLI):**

- Input: a local path to a cloned repo \+ repo name.  
- Output: writes `manifest.json` and raw artifacts to configured storage.  
- Stateless; idempotent; one repo per invocation.

**Aggregator (also `atlas` CLI, separate subcommand):**

- Input: storage location.  
- Output: writes `graph/latest.json` \+ timestamped snapshot.  
- Stateless; reads all `services/*/manifest.json`; merges.

**Pipeline:**

- Orchestrates the above.  
- Cloning, auth, parallelism, scheduling all live here — NOT in the extractor code.

**MCP server:**

- Stateless HTTP service (or stdio, depending on deployment).  
- Reads from storage; caches graph in memory; refreshes periodically.  
- Exposes 4 tools (see §6).

### 2.3 One-way data flow principle

The extractor NEVER writes back to source repos. The graph is a *derivation* of the source repos. If the graph is wrong, the fix goes into the source repo's `service.yaml` — not into the graph. The whole bucket can be deleted and rebuilt from the repos at any time.

---

## 3\. `service.yaml` schema

Every source repo must contain a `service.yaml` at its root. This is the human-authored input to the extractor.

### 3.1 Schema

\# REQUIRED FIELDS

name: payment-reconciliation            \# unique identifier, kebab-case

type: backend-go                        \# android | ios | backend-go | backend-node | web-react

owner: team-payments                    \# team/squad name

domain: payments                        \# high-level bucket (payments, identity, notifications, etc.)

tier: critical                          \# critical | standard | experimental | deprecated

purpose: \>

  Reconciles incoming payment events against merchant ledgers for LATAM markets.

  One to three sentences. Must be scannable.

\# OPTIONAL FIELDS

status: active                          \# active | deprecated | archived (default: active)

slack: "\#team-payments"

runbook: https://confluence.../runbook

jira\_component: PAY-RECON

keywords:                               \# free-form terms to boost retrieval

  \- subscription

  \- billing

  \- merchant-ledger

\# INTEGRATION NOTES

\# Used by consuming agents. Do NOT dump arbitrary notes here.

\# Limits: max 10 global notes, max 3 per endpoint, max 200 chars each.

integration\_notes:

  \- scope: global

    note: "All endpoints require Authorization: Bearer \<token\>"

  \- scope: global

    note: "All endpoints require X-Device-ID header"

  \- scope: "POST /v1/transfers"

    note: "Returns 202 Accepted; poll /v1/transfers/{id} for final status"

\# EXTRACTOR HINTS (only if repo has non-standard layout)

\# Use sparingly. Most repos should not need this.

extractor\_hints:

  project\_root: ios/MyApp/              \# e.g. for nested Xcode projects

  additional\_docs:                      \# extra markdown files worth indexing

    \- docs/architecture.md

### 3.2 Validation

- A JSON Schema (`schemas/service.schema.json`) defines the full contract.  
- The extractor validates `service.yaml` against the schema. On failure: fail the extraction for that repo, log the error, continue with others.  
- Source repos should validate their own `service.yaml` in their CI (pre-merge check). This is out of scope for this PRD but document it in the README.

### 3.3 Size limits (enforced in extractor)

| Field | Limit |
| :---- | :---- |
| `purpose` | 500 chars |
| `keywords` | 10 items |
| `integration_notes` total | 20 items |
| `integration_notes` global notes | 10 items |
| `integration_notes` per-endpoint notes | 3 items per endpoint |
| each note text | 200 chars |

Violations \= extraction failure for that repo.

---

## 4\. Extractor

### 4.1 Repository layout

atlas/

├── README.md

├── pyproject.toml                      \# use uv

├── src/

│   └── atlas/

│       ├── \_\_init\_\_.py

│       ├── cli.py                      \# entry point (typer or click)

│       ├── detect.py                   \# repo type detection

│       ├── schema.py                   \# pydantic models for manifest \+ graph

│       ├── extractors/

│       │   ├── \_\_init\_\_.py

│       │   ├── base.py                 \# abstract Extractor class

│       │   ├── android.py

│       │   ├── ios.py

│       │   ├── backend\_go.py

│       │   └── (add more as needed)

│       ├── openapi.py                  \# OpenAPI spec parsing \+ indexing

│       ├── aggregator.py               \# per-service manifests → graph

│       ├── storage.py                  \# storage backend abstraction

│       └── validation.py               \# service.yaml schema validation

├── tests/

│   ├── conftest.py

│   ├── fixtures/

│   │   ├── sample-android-repo/       \# tiny fake repo for tests

│   │   ├── sample-ios-repo/

│   │   └── sample-go-repo/

│   ├── test\_detect.py

│   ├── test\_android\_extractor.py

│   ├── test\_ios\_extractor.py

│   ├── test\_backend\_go\_extractor.py

│   ├── test\_aggregator.py

│   └── test\_validation.py

├── schemas/

│   ├── service.schema.json             \# the service.yaml contract

│   └── manifest.schema.json            \# the extractor's output contract

├── pipelines/

│   └── azure-pipelines.yml

├── config/

│   └── repos.yaml                      \# list of repos to extract (see §5.2)

└── mcp\_server/                         \# MCP server (see §6)

    ├── \_\_init\_\_.py

    ├── server.py

    └── tests/

### 4.2 CLI contract

\# Extract one repo (what the pipeline runs per repo)

uv run atlas extract \\

  \--repo-path /tmp/target \\

  \--repo-name payment-reconciliation \\

  \--storage-backend gcs \\

  \--storage-bucket platform-atlas-prod

\# Aggregate (what the pipeline runs after all extractions)

uv run atlas aggregate \\

  \--storage-backend gcs \\

  \--storage-bucket platform-atlas-prod

\# Validate a service.yaml (useful locally and in source repo CI)

uv run atlas validate \--file path/to/service.yaml

### 4.3 Detection

`detect.py` returns the repo type given a repo path. Rules, in order:

1. If `service.yaml` declares `type:` → use it; verify against disk signals; fail if mismatch.  
2. Else, signal-based detection:  
   - `build.gradle*` \+ `AndroidManifest.xml` → `android`  
   - `Package.swift` OR `*.xcodeproj` OR `Podfile` → `ios`  
   - `go.mod` → `backend-go`  
   - `package.json` (and not React Native) → `backend-node` OR `web-react` (distinguish by presence of React in deps)  
3. If still ambiguous → fail with a clear error message.

### 4.4 Extractor interface

\# src/atlas/extractors/base.py

class Extractor(ABC):

    """Base class for all ecosystem-specific extractors."""

    type: str  \# e.g. "android"

    @abstractmethod

    def extract(self, repo\_path: Path, service\_yaml: ServiceManifest) \-\> ExtractedData:

        """Parse repo-specific files and return structured data."""

        ...

    @abstractmethod

    def find\_api\_contracts(self, repo\_path: Path) \-\> list\[ApiContract\]:

        """Find OpenAPI/proto files; return references, not contents."""

        ...

### 4.5 Per-extractor responsibilities

Each extractor knows its ecosystem's conventions. No ecosystem knowledge leaks between them.

#### Android (`android.py`)

Must extract:

- Language (Kotlin/Java) — inferred from source files  
- Min/target/compile SDK — from `build.gradle(.kts)` or `AndroidManifest.xml`  
- Application ID / package name — from `build.gradle(.kts)` or manifest  
- Dependencies — from `build.gradle(.kts)` or `libs.versions.toml`  
- Modules — from `settings.gradle(.kts)`  
- Permissions — from `AndroidManifest.xml`  
- Entry activities — from `AndroidManifest.xml` (`android.intent.action.MAIN`)  
- CI system — from `.github/workflows/` or `azure-pipelines.yml`

API contracts: typically none (mobile is a consumer). Skip unless `service.yaml` explicitly lists them under `extractor_hints.additional_docs`.

Use real parsers where available. For Gradle, parsing the files as text with regexes is acceptable v1; do not invoke Gradle itself (too slow, too heavy). Use `tomli` for `libs.versions.toml`.

#### iOS (`ios.py`)

Must extract:

- Language (Swift/Objective-C)  
- Swift version — from `Package.swift` or xcodeproj settings  
- Bundle identifier — from xcodeproj or `Info.plist`  
- Deployment target — from xcodeproj or Package.swift  
- Dependencies — from `Package.swift`, `Podfile`, or `Cartfile`  
- Targets — from xcodeproj  
- Entitlements (non-sensitive keys only) — from `*.entitlements`  
- CI system

Use `pbxproj` Python package for xcodeproj parsing. Never parse pbxproj files with regex — they are ordered and nested in ways that break text parsing.

API contracts: same as Android.

#### Backend Go (`backend_go.py`)

Must extract:

- Language and Go version — from `go.mod`  
- Module path — from `go.mod`  
- Dependencies — from `go.mod` (use `go list -m -json all` if available, else parse `go.mod`)  
- Entry points — look for `main.go` in root, `cmd/*/main.go` patterns  
- Dockerfile presence — `Dockerfile` at root  
- Deployment manifests — look for `k8s/`, `deploy/`, `*.yaml` with k8s `kind:`  
- OpenAPI specs — look for `api/openapi.{yaml,json}`, `docs/openapi.{yaml,json}`, `openapi.{yaml,json}` at root  
- Proto files — look for `*.proto` under `proto/`, `api/`, `rpc/`

### 4.6 OpenAPI indexing

When an OpenAPI spec is found:

1. Parse with `openapi-spec-validator` and `prance`.  
2. Extract the **endpoint index** — for each `paths` entry:  
     
   {  
     
     "method": "POST",  
     
     "path": "/v1/payments",  
     
     "summary": "Create a payment",  
     
     "tags": \["payments"\],  
     
     "operation\_id": "createPayment"  
     
   }  
     
3. Copy the original spec file as-is to `services/{repo-name}/openapi.yaml`.

Do NOT inline the full spec into the manifest. The index is for cheap browsing; the full spec is retrieved on demand by MCP.

For proto files: extract service \+ method signatures similarly. Copy original files unchanged.

### 4.7 Normalized manifest output

Every extractor emits this shape to `services/{repo-name}/manifest.json`:

{

  "name": "payment-reconciliation",

  "type": "backend-go",

  "owner": "team-payments",

  "domain": "payments",

  "tier": "critical",

  "status": "active",

  "purpose": "Reconciles incoming payment events...",

  "keywords": \["subscription", "billing"\],

  "language": "go",

  "language\_version": "1.22",

  "slack": "\#team-payments",

  "runbook": "https://...",

  "jira\_component": "PAY-RECON",

  "dependencies": \[

    {

      "name": "github.com/stripe/stripe-go",

      "version": "v74.0.0",

      "source": "go.mod",

      "direct": true

    }

  \],

  "entry\_points": \[

    { "kind": "http-server", "ref": "cmd/server/main.go" }

  \],

  "api\_contracts": \[

    {

      "kind": "openapi",

      "version": "3.0.3",

      "path": "services/payment-reconciliation/openapi.yaml",

      "endpoints": \[

        {

          "method": "POST",

          "path": "/v1/payments",

          "summary": "Create a payment",

          "tags": \["payments"\]

        }

      \]

    }

  \],

  "runtime": {

    "docker": true,

    "k8s\_manifests": "k8s/production/"

  },

  "ci": "azure-pipelines",

  "integration\_notes": \[

    {

      "scope": "global",

      "note": "All endpoints require Authorization: Bearer \<token\>"

    }

  \],

  "extracted\_at": "2026-04-23T03:00:00Z",

  "extractor\_version": "1.0.0",

  "source\_repo": {

    "url": "https://dev.azure.com/.../payment-reconciliation",

    "commit": "abc123..."

  }

}

A JSON Schema (`schemas/manifest.schema.json`) must define this contract. The extractor validates its own output before writing.

### 4.8 Error handling

- Extraction failure for ONE repo must NOT fail the pipeline. Log, continue.  
- On failure, write `services/{repo-name}/extraction-error.json` with:  
    
  {  
    
    "repo": "payment-reconciliation",  
    
    "timestamp": "...",  
    
    "error": "service.yaml validation failed: 'domain' is required",  
    
    "phase": "validation"  
    
  }  
    
- Keep the previous successful `manifest.json` in place — don't delete good data on failure.  
- The aggregator must skip repos with only an error file and include them in a `failed_extractions` list in the graph.

---

## 5\. Pipeline

### 5.1 Design constraints (explicit user decisions)

- **Scheduled only for v1.** No webhooks, no per-repo triggers.  
- **Manual trigger supported** — engineers can run the pipeline on demand via Azure DevOps UI.  
- **Parallel per-repo extraction.** Aggregator runs after all extractions complete.  
- **Fail-soft per repo.** One repo's failure does not block others.

### 5.2 Repo registry — `config/repos.yaml`

\# Central list of repos to extract. Sole source of truth for "what's in the platform."

repos:

  \- name: mobile-banking-android

    url: https://dev.azure.com/org/project/\_git/mobile-banking-android

    type\_hint: android

  \- name: mobile-banking-ios

    url: https://dev.azure.com/org/project/\_git/mobile-banking-ios

    type\_hint: ios

  \- name: payment-reconciliation

    url: https://dev.azure.com/org/project/\_git/payment-reconciliation

    type\_hint: backend-go

  \# ... etc

`type_hint` is optional. Detection must verify it matches reality and fail if not.

### 5.3 Azure DevOps pipeline

`pipelines/azure-pipelines.yml`:

schedules:

  \- cron: "0 3 \* \* \*"           \# 3am daily, UTC

    displayName: Nightly extraction

    branches:

      include: \[main\]

    always: true

trigger: none                    \# do not run on commits to this repo (use separate PR pipeline for that)

pr: none

parameters:

  \- name: repoFilter

    displayName: Only extract one repo (leave blank for all)

    type: string

    default: ''

  \- name: forceRefresh

    displayName: Force full refresh

    type: boolean

    default: false

variables:

  \- group: platform-atlas-secrets    \# holds git PAT, storage creds

  \- name: STORAGE\_BACKEND

    value: gcs                        \# or azure-blob, see §5.5

  \- name: STORAGE\_BUCKET

    value: platform-atlas-prod

jobs:

  \- job: extract

    displayName: Extract per repo

    strategy:

      matrix:

        \# Claude Code: generate this matrix programmatically from config/repos.yaml.

        \# One entry per repo. Example:

        mobile\_banking\_android:

          repoName: mobile-banking-android

          repoUrl: https://...

        payment\_reconciliation:

          repoName: payment-reconciliation

          repoUrl: https://...

      maxParallel: 6

    steps:

      \- checkout: self

      \- task: UsePythonVersion@0

        inputs: { versionSpec: '3.12' }

      \- script: pip install uv && uv sync

        displayName: Install deps

      \- script: |

          git clone \--depth 1 $(repoUrl) /tmp/target

        displayName: 'Clone $(repoName)'

        env:

          GIT\_ASKPASS: /bin/echo

          GIT\_USERNAME: $(GIT\_USERNAME)

          GIT\_PASSWORD: $(GIT\_PAT)     \# from Key Vault via variable group

      \- script: |

          uv run atlas extract \\

            \--repo-path /tmp/target \\

            \--repo-name $(repoName) \\

            \--storage-backend $(STORAGE\_BACKEND) \\

            \--storage-bucket $(STORAGE\_BUCKET)

        displayName: Extract

  \- job: aggregate

    dependsOn: extract

    condition: succeededOrFailed()    \# run even if some extractions failed

    steps:

      \- checkout: self

      \- task: UsePythonVersion@0

        inputs: { versionSpec: '3.12' }

      \- script: pip install uv && uv sync

      \- script: |

          uv run atlas aggregate \\

            \--storage-backend $(STORAGE\_BACKEND) \\

            \--storage-bucket $(STORAGE\_BUCKET)

        displayName: Aggregate into graph

  \- job: notify

    dependsOn: \[extract, aggregate\]

    condition: succeededOrFailed()

    steps:

      \- script: |

          \# Log a summary: N succeeded, M failed, write to AppInsights or stdout

          uv run atlas report \\

            \--storage-backend $(STORAGE\_BACKEND) \\

            \--storage-bucket $(STORAGE\_BUCKET)

        displayName: Summary

**Claude Code implementation note:** Generate the matrix entries programmatically from `config/repos.yaml` at pipeline compile time. Azure DevOps supports this via template expressions. Do not hand-maintain the matrix in YAML.

### 5.4 Auth

Use Azure Key Vault for secrets. Reference the Key Vault via an Azure DevOps variable group (`platform-atlas-secrets`). Secrets needed:

- `GIT_PAT` — read-only PAT for cloning source repos.  
- `STORAGE_CREDENTIALS` — GCS service account JSON or Azure Storage SAS token.

Do NOT commit secrets. Do NOT use plain pipeline variables.

### 5.5 Storage backend abstraction

The `atlas.storage` module must support both:

- **GCS** (`google-cloud-storage`)  
- **Azure Blob** (`azure-storage-blob`)

via a single interface:

class StorageBackend(ABC):

    def write\_json(self, key: str, data: dict) \-\> None: ...

    def write\_bytes(self, key: str, data: bytes) \-\> None: ...

    def read\_json(self, key: str) \-\> dict: ...

    def read\_bytes(self, key: str) \-\> bytes: ...

    def list(self, prefix: str) \-\> list\[str\]: ...

    def exists(self, key: str) \-\> bool: ...

Factory pattern: `StorageBackend.from_config(backend="gcs", bucket="...")`.

Default to GCS (user is on GCP for Flow). Support Azure Blob so v1 can run in Azure DevOps without cross-cloud data movement if user prefers.

### 5.6 Observability

Every pipeline run must log (to stdout AND a structured log file written to storage):

- Run ID, start/end timestamps  
- Per repo: name, success/failure, duration, size of emitted manifest  
- Aggregation: number of services merged, graph size  
- Total runtime

Write a run summary to `storage/runs/{timestamp}.json`. Keep the last 30 days of these.

---

## 6\. MCP server

### 6.1 Deployment shape

Single-process Python service. Two deployment modes, both supported:

1. **Stdio mode** — for local dev and direct integration with Claude Code. The server reads from stdin, writes to stdout.  
2. **HTTP/SSE mode** — for remote access from Flow and other agents. Runs behind whatever authentication your platform already has (do not reinvent auth here).

Use the official `mcp` Python SDK (`pip install mcp`).

### 6.2 Data loading

On startup, the server:

1. Reads `graph/latest.json` from storage.  
2. Loads it into memory.  
3. Indexes it for the queries below.  
4. Schedules a background refresh every 15 minutes (configurable).

The server is stateless beyond this cache. No database.

Per-service manifests and raw artifacts (OpenAPI specs) are fetched from storage **on demand** and cached in-process with a TTL (default 1 hour).

### 6.3 Tool surface — exactly 4 tools

#### `find_relevant_services(task_description: str, max_results: int = 5)`

**Purpose:** Given a free-text task description, return the most likely services to be involved.

**Implementation (v1 — keyword only):**

- Tokenize the task description.  
- For each service, compute a score based on:  
  - Matches in `name` (highest weight)  
  - Matches in `keywords`  
  - Matches in `purpose`  
  - Matches in `domain`  
- Return top N with scores.

**Return shape:**

{

  "candidates": \[

    {

      "name": "auth-service",

      "type": "backend-go",

      "domain": "identity",

      "purpose": "...",

      "score": 0.88,

      "matched\_on": \["name", "purpose", "keywords"\]

    }

  \]

}

Do NOT implement embeddings, vector search, or LLM-based ranking in v1. This is an explicit constraint. We want to measure keyword failure rates before graduating.

#### `list_endpoints(service: str)`

**Purpose:** Cheap index of all endpoints a service exposes. Used for browsing and existence checks.

**Return shape:**

{

  "service": "auth-service",

  "endpoints": \[

    {

      "method": "POST",

      "path": "/v1/auth/login",

      "summary": "Authenticate user with email+password",

      "tags": \["auth"\]

    }

  \]

}

Reads from `graph/latest.json` only. No OpenAPI file fetch.

#### `get_service_context(name: str, include: list[str] = ["manifest", "deps", "contracts", "notes"])`

**Purpose:** Deep context on a single service. The main tool for agents orienting to a service.

**Parameters:**

- `include` — a list of sections to include. Default returns everything. Use to trim payload.

**Return shape:**

{

  "name": "auth-service",

  "manifest": { /\* subset of manifest.json \*/ },

  "direct\_dependencies": \[ /\* names only, not full dep info \*/ \],

  "direct\_dependents": \[ /\* services that depend on THIS one — computed from graph \*/ \],

  "api\_contracts": \[ /\* endpoint index, not full specs \*/ \],

  "integration\_notes": {

    "global": \["..."\],

    "by\_endpoint": { "POST /v1/auth/login": \["..."\] }

  }

}

Fetches `services/{name}/manifest.json` from storage on demand.

#### `get_endpoint_contract(service: str, method: str, path: str)`

**Purpose:** Return the full request/response schema for one endpoint. The tool agents use when writing integration code.

**Behavior:**

- Fetches `services/{service}/openapi.yaml` from storage.  
- Parses it.  
- Returns the specific operation's full schema.  
- Includes any `integration_notes` scoped to this endpoint.

**Return shape:**

{

  "service": "auth-service",

  "method": "POST",

  "path": "/v1/auth/login",

  "operation": { /\* full OpenAPI operation object \*/ },

  "request\_schema": { /\* dereferenced \*/ },

  "response\_schemas": { "200": {...}, "401": {...} },

  "integration\_notes": \["Bearer token must be passed in Authorization header on every call"\]

}

Resolves `$ref`s in the spec before returning.

### 6.4 What the MCP does NOT expose

Do not add these tools in v1:

- `get_full_graph()` or similar bulk-dump operations  
- `get_openapi_spec(service)` — full spec dump  
- `search_code(query)` — that's Sourcegraph's job  
- `get_adr(id)` — no ADR support in v1

Keeping the tool list at 4 is a hard constraint. Adding a tool has a real cost (context window for every agent).

### 6.5 Query logging

Log every tool call for later review:

{

  "timestamp": "...",

  "tool": "find\_relevant\_services",

  "input": { "task\_description": "..." },

  "output\_summary": { "num\_candidates": 3, "top\_score": 0.88 },

  "duration\_ms": 42

}

Write to `logs/mcp/{date}.jsonl` in storage. Rotate daily. Keep 30 days.

This is how we'll tune retrieval over time. Ship with this from day 1\.

---

## 7\. Testing

### 7.1 Unit tests (required, run on every PR)

- **Per extractor:** fixture-based. Each fixture is a tiny fake repo checked into `tests/fixtures/`. Test:  
  - Successful extraction produces correct manifest.  
  - Missing `service.yaml` fails with clear error.  
  - Schema violations fail validation.  
  - Non-standard layout (via `extractor_hints`) works.  
- **Detection:** each type signal tested; ambiguous cases fail cleanly.  
- **Aggregator:** feed it N manifests, assert graph contains expected services, dependencies, domains.  
- **Validation:** schema edge cases — missing required fields, exceeding size limits, invalid types.  
- **Storage backend:** mock both GCS and Azure Blob; test round-trips.  
- **MCP tools:** each tool tested against a fixture graph. Assert shape, scoring behavior, edge cases (missing service, missing endpoint).

Coverage target: 80%+ on `src/atlas/`. Use `pytest-cov`.

### 7.2 Integration test (required, run nightly or on-demand)

Single end-to-end test:

1. Clone a known real repo (read-only, probably one of the Globant repos).  
2. Run the extractor.  
3. Assert the output manifest has expected shape and non-empty key fields.

Do NOT run the full pipeline against all 12 repos in CI. That's the nightly production pipeline's job.

### 7.3 Smoke test (post-deployment)

After a pipeline run:

1. Assert `graph/latest.json` exists and is newer than 24h.  
2. Assert it contains at least N services (where N \= count in `config/repos.yaml` minus a small tolerance).  
3. Start the MCP server, call each tool with a known-good input, assert non-empty response.

---

## 8\. Implementation order

Claude Code: implement in this order. Do not parallelize phases.

**Phase 1 — foundation (1-2 days):**

1. Repo scaffolding (`pyproject.toml`, folder structure, `README.md`).  
2. `schemas/service.schema.json` and `schemas/manifest.schema.json`.  
3. `src/atlas/schema.py` — pydantic models matching schemas.  
4. `src/atlas/validation.py` — validate `service.yaml`.  
5. `src/atlas/detect.py` — repo type detection.  
6. `src/atlas/storage.py` — storage backend abstraction; implement local filesystem backend first for testing, GCS and Azure Blob after.  
7. Unit tests for all above.

**Phase 2 — first extractor (1 day):** 8\. `src/atlas/extractors/base.py` — abstract base class. 9\. `src/atlas/extractors/backend_go.py` — full Go extractor. 10\. `tests/fixtures/sample-go-repo/` — fake Go repo. 11\. Tests for Go extractor. 12\. `src/atlas/cli.py` — `atlas extract` subcommand. End-to-end run against fixture.

**Phase 3 — other extractors (2 days):** 13\. Android extractor \+ fixture \+ tests. 14\. iOS extractor \+ fixture \+ tests. 15\. OpenAPI indexing (`src/atlas/openapi.py`) \+ tests.

**Phase 4 — aggregation (1 day):** 16\. `src/atlas/aggregator.py` \+ tests. 17\. `atlas aggregate` CLI subcommand. 18\. `atlas report` CLI subcommand. 19\. End-to-end test: run extract on all fixtures, run aggregate, verify graph.

**Phase 5 — pipeline (1-2 days):** 20\. `pipelines/azure-pipelines.yml`. 21\. `config/repos.yaml` template (real contents filled in by the user post-implementation). 22\. Key Vault / variable group documentation in README. 23\. Manual dry run in Azure DevOps against 1-2 real repos.

**Phase 6 — MCP server (2-3 days):** 24\. `mcp_server/server.py` — MCP SDK setup, load graph on startup, background refresh. 25\. Implement all 4 tools. 26\. Query logging. 27\. Tests for each tool against fixture graph. 28\. Local stdio mode verified with Claude Code. 29\. HTTP/SSE mode \+ deployment notes.

**Phase 7 — polish (1 day):** 30\. Observability wiring (structured logs, run summaries). 31\. README with setup, local dev, deployment, troubleshooting. 32\. Runbook for common issues.

Total estimate: 10-13 working days.

---

## 9\. Dependencies (Python packages)

Pin these in `pyproject.toml`. Use `uv`.

\[project\]

name \= "atlas"

version \= "1.0.0"

requires-python \= "\>=3.12"

dependencies \= \[

    "pydantic\>=2.6",

    "pydantic-settings\>=2.2",

    "typer\>=0.12",

    "pyyaml\>=6.0",

    "jsonschema\>=4.21",

    "tomli\>=2.0",

    "prance\>=23.6",

    "openapi-spec-validator\>=0.7",

    "google-cloud-storage\>=2.16",

    "azure-storage-blob\>=12.19",

    "mcp\>=1.0",

    "structlog\>=24.1",

\]

\[project.optional-dependencies\]

ios \= \["pbxproj\>=4.2"\]

dev \= \[

    "pytest\>=8.0",

    "pytest-cov\>=5.0",

    "ruff\>=0.3",

    "mypy\>=1.9",

\]

\[project.scripts\]

atlas \= "atlas.cli:app"

---

## 10\. Things to explicitly NOT do

Claude Code: these are anti-requirements. Do not implement any of them, even if they seem obviously helpful.

1. Do not build a graph database wrapper "for future use." JSON files only.  
2. Do not add embeddings or vector search. Keyword matching only.  
3. Do not add LLM-based extraction or summarization. Deterministic parsing only.  
4. Do not build a webhook receiver. Scheduled pipeline only.  
5. Do not auto-generate `service.yaml` for repos that lack one. Fail extraction instead.  
6. Do not add a fifth MCP tool.  
7. Do not build a web UI.  
8. Do not add user authentication to the MCP server itself (rely on platform auth).  
9. Do not try to sync to Confluence. Not in scope.  
10. Do not add `agents.md` support in v1. `integration_notes` in `service.yaml` is the only consuming-agent context for now.

If any of these seem necessary during implementation, STOP and escalate to the PRD owner. Do not silently add them.

---

## 11\. Open questions for the PRD owner

Claude Code: surface these in the first PR's description. Do not block on them — pick a reasonable default and document it.

1. **Storage backend default:** GCS or Azure Blob? (Defaulting to GCS based on existing Flow infrastructure.)  
2. **MCP deployment target:** Cloud Run, Azure Container Apps, or a VM? (Recommend Cloud Run for parity with Flow.)  
3. **Secret management:** Azure Key Vault confirmed? Or GCP Secret Manager?  
4. **Monorepo support:** any of the \~12 repos actually monorepos with multiple services? If yes, schema needs a `services:` array variant.  
5. **Source repo CI enforcement:** should the extractor repo ship a GitHub Action / Azure template that source repos can adopt for `service.yaml` validation? Out of v1 scope but useful follow-up.

---

## 12\. Glossary

- **Atlas** — the name of this system.  
- **Extractor** — the Python tool that reads a repo and emits a manifest.  
- **Manifest** — the normalized per-service JSON output.  
- **Graph** — the aggregated JSON containing summaries of all services.  
- **MCP** — Model Context Protocol. The standard for exposing tools/resources to LLM agents.  
- **Service** — in this context, any repo registered in `config/repos.yaml`. May be a backend microservice, mobile app, or frontend.  
- **Domain** — high-level functional area (payments, identity, etc.). Used for grouping in queries.

---

*End of PRD. Implementer: build exactly what's here. Questions → the PRD owner, not assumptions.*  
