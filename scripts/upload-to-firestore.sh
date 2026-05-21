#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Cortex — Upload extracted data to Firestore
#
# Usage:
#   ./scripts/upload-to-firestore.sh [OUTPUT_DIR] [FIRESTORE_DATABASE]
#
#   OUTPUT_DIR         Local cortex-output directory (default: ./cortex-output)
#   FIRESTORE_DATABASE Firestore named database      (default: cortex)
#
# Examples:
#   ./scripts/upload-to-firestore.sh
#   ./scripts/upload-to-firestore.sh ./cortex-output
#   ./scripts/upload-to-firestore.sh /tmp/cortex-smoke cortex
#
# What this uploads (essential data only — graph + service manifests):
#   graph/latest.json              → Firestore collection "graph" / doc "latest"
#   services/{name}/manifest.json  → Firestore collection "services" / doc "{name}"
#
# SAFETY: This script NEVER deletes or clears any existing Firestore data.
#         All writes are upserts (merge). Existing documents are preserved.
#
# Prerequisites:
#   - gcloud CLI authenticated (Application Default Credentials):
#       gcloud auth application-default login
#   - GCP_PROJECT_ID set in .env or environment
#   - uv available on PATH (pip install uv  OR  brew install uv)
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

# ── Load .env ─────────────────────────────────────────────────────────────────
if [ -f "${ROOT_DIR}/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  source "${ROOT_DIR}/.env"
  set +a
fi

# ── Args / defaults ───────────────────────────────────────────────────────────
OUTPUT_DIR="${1:-${ROOT_DIR}/cortex-output}"
FIRESTORE_DB="${2:-cortex}"
PROJECT_ID="${GCP_PROJECT_ID:?ERROR: GCP_PROJECT_ID is not set. Add it to .env or export it.}"

echo ""
echo "════════════════════════════════════════════════════════════"
echo "  Cortex → Firestore Upload"
echo "════════════════════════════════════════════════════════════"
echo "  Source dir : ${OUTPUT_DIR}"
echo "  Project    : ${PROJECT_ID}"
echo "  Database   : ${FIRESTORE_DB}"
echo "════════════════════════════════════════════════════════════"
echo ""

# ── Validate source directory ─────────────────────────────────────────────────
if [ ! -d "${OUTPUT_DIR}" ]; then
  echo "ERROR: Output directory not found: ${OUTPUT_DIR}"
  echo ""
  echo "Run the extractor first:"
  echo "  uv run cortex run-local --config config/repos-real.yaml --output-dir ./cortex-output"
  exit 1
fi

if [ ! -f "${OUTPUT_DIR}/graph/latest.json" ]; then
  echo "ERROR: graph/latest.json not found in ${OUTPUT_DIR}"
  echo "       Run 'cortex aggregate' or 'cortex run-local' first."
  exit 1
fi

# ── Run the Python upload script ──────────────────────────────────────────────
echo "→ Uploading to Firestore (database: ${FIRESTORE_DB}) ..."
echo ""

uv run \
  --directory "${ROOT_DIR}" \
  python "${SCRIPT_DIR}/upload_to_firestore.py" \
  --output-dir "${OUTPUT_DIR}" \
  --database "${FIRESTORE_DB}" \
  --project "${PROJECT_ID}"

echo ""
echo "════════════════════════════════════════════════════════════"
echo "  Upload Complete"
echo "════════════════════════════════════════════════════════════"
echo ""
echo "  The Cortex MCP server on Cloud Run will serve this data"
echo "  on the next request (graph is reloaded on first query)."
echo ""
echo "  Verify in the Firestore console:"
echo "  https://console.cloud.google.com/firestore/databases/${FIRESTORE_DB}/data?project=${PROJECT_ID}"
echo "════════════════════════════════════════════════════════════"
