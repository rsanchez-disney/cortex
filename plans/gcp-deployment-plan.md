# Cortex MCP Server — GCP Deployment Plan

This document is the finalized implementation plan for deploying the Cortex MCP Server
to GCP Cloud Run, complementing `plans/gcp.md` with project-specific details.

---

## Context

- **Kilo (local dev):** uses `stdio` mode — spawns `cortex mcp-server --mode stdio` directly. **No changes needed.**
- **Cloud Run (production):** uses `http` mode — `run_http` serves via streamable-HTTP transport (MCP 2025-03-26 spec).
- **Storage:** Local runs use `local` or `gcs` backends. Cloud Run reads from **Firestore** (named database `cortex`).
- **Shared GCP project:** This project coexists with Flow (using `archon-*` resources). All Cortex resources use the `cortex` prefix. **No delete/clear/destroy operations exist anywhere.**

---

## Deliverables

| # | File | Purpose |
|---|------|---------|
| 1 | `plans/gcp-deployment-plan.md` | This document |
| 2 | `Dockerfile` | Cloud Run container (Python 3.12, streamable-HTTP mode, port 8080) |
| 3 | `.dockerignore` | Keep container image lean |
| 4 | `src/cortex/firestore_storage.py` | `FirestoreStorageBackend` implementing `StorageBackend` ABC |
| 5 | `src/cortex/storage.py` | Add `"firestore"` to the factory method |
| 6 | `pyproject.toml` | Add `google-cloud-firestore>=2.16` dependency |
| 7 | `scripts/deploy.sh` | Idempotent build & deploy to Cloud Run |
| 8 | `scripts/upload-to-firestore.sh` | Shell wrapper to trigger data upload |
| 9 | `scripts/upload_to_firestore.py` | Python: push graph + manifests to Firestore |

---

## Architecture

```
Local machine:
  uv run cortex run-local → cortex-output/   (local JSON files)
           │
           ▼
  scripts/upload-to-firestore.sh
    → scripts/upload_to_firestore.py
    → Firestore (database: "cortex")
           │
           ▼
Cloud Run service "cortex":
  cortex mcp-server --mode http --storage-backend firestore --storage-bucket cortex
  → reads graph/latest.json, services/*/manifest.json from Firestore
           │
           ▼
  Flow workers (archon-dev SA) → authenticated HTTP → MCP tools
```

---

## Firestore Document Layout

The `FirestoreStorageBackend` maps the existing key-based storage layout to Firestore:

| Storage key | Firestore location |
|-------------|--------------------|
| `graph/latest.json` | collection `graph` / doc `latest` |
| `services/{name}/manifest.json` | collection `services` / doc `{name}` / field `manifest` (as nested map) |
| `logs/mcp/{date}.jsonl` | collection `logs_mcp` / doc `{date}` / field `entries` (array) |

Only `read_json`, `write_json`, and `exists` are used by the MCP server in practice.
`list`, `read_bytes`, and `write_bytes` are implemented for interface completeness.

---

## GCP Resources

| Resource | Name | Notes |
|----------|------|-------|
| Artifact Registry | `cortex` | Docker image repo |
| Service Account | `cortex@{PROJECT}.iam.gserviceaccount.com` | Minimal permissions |
| Firestore Database | `cortex` | Named, isolated from Flow's `archon-dev` |
| Cloud Run Service | `cortex` | `--ingress internal`, no public access |
| IAM roles on SA | `datastore.user`, `secretmanager.secretAccessor`, `logging.logWriter` | Additive only |

---

## Usage

### First-time setup / every update

```bash
# 1. Set your project ID
cp .env.example .env   # then edit GCP_PROJECT_ID

# 2. Deploy (idempotent — safe to re-run)
./scripts/deploy.sh
```

### Uploading / refreshing data

```bash
# After running cortex run-local locally:
uv run cortex run-local --config config/repos-real.yaml --output-dir ./cortex-output

# Push the essential data (graph + manifests) to Firestore:
./scripts/upload-to-firestore.sh ./cortex-output
```

---

## Safety Guarantees

- ✅ All resource names use `cortex` prefix (zero collision with `archon-*`)
- ✅ Separate Firestore database (`cortex` vs `archon-dev`)
- ✅ Separate service account (`cortex` vs `archon-dev`)
- ✅ **No delete/clear/destroy commands in any script**
- ✅ Deploy uses `describe ... || create` (idempotent, never destructive)
- ✅ Upload uses Firestore `set()` with merge (upsert — never deletes documents)
- ✅ IAM uses `add-iam-policy-binding` (additive, never removes existing bindings)
- ✅ Cloud Run env vars use `--update-env-vars` (never `--set-env-vars`)
