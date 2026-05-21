#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Cortex — Build & Deploy to GCP Cloud Run
#
# Usage:
#   ./scripts/deploy.sh
#
# Prerequisites:
#   - gcloud CLI authenticated: gcloud auth login
#   - GCP_PROJECT_ID set in .env or environment
#   - Docker / Cloud Build access
#
# This script is fully IDEMPOTENT — safe to run on first deploy and every
# subsequent update. It will NEVER delete or destroy any existing resources.
#
# GCP resource naming (all prefixed "cortex" to avoid collision with Flow):
#   Artifact Registry : cortex
#   Service Account   : cortex@{PROJECT}.iam.gserviceaccount.com
#   Firestore DB      : cortex   (named, isolated from Flow's archon-prod)
#   Cloud Run service : cortex
#   Secret prefix     : cortex-*
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

# ── Load .env ─────────────────────────────────────────────────────────────────
if [ -f "${ROOT_DIR}/.env" ]; then
  echo "→ Loading .env ..."
  set -a
  # shellcheck disable=SC1091
  source "${ROOT_DIR}/.env"
  set +a
fi

# ── Config ────────────────────────────────────────────────────────────────────
PROJECT_ID="${GCP_PROJECT_ID:?ERROR: GCP_PROJECT_ID is not set. Add it to .env or export it.}"
REGION="${GCP_REGION:-us-central1}"

SERVICE_NAME="cortex"
SA_NAME="cortex"
SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
AR_REPO="cortex"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO}/cortex:latest"
FIRESTORE_DB="cortex"

# Flow's service account — granted invocation rights on the Cortex Cloud Run service.
# Override via FLOW_SA env var if Flow uses a different SA in your project.
FLOW_SA="${FLOW_SA:-archon-prod@${PROJECT_ID}.iam.gserviceaccount.com}"

echo ""
echo "════════════════════════════════════════════════════════════"
echo "  Cortex Deploy"
echo "════════════════════════════════════════════════════════════"
echo "  Project : ${PROJECT_ID}"
echo "  Region  : ${REGION}"
echo "  Service : ${SERVICE_NAME}"
echo "  Image   : ${IMAGE}"
echo "  DB      : ${FIRESTORE_DB} (Firestore named database)"
echo "════════════════════════════════════════════════════════════"
echo ""

# ── 1. One-time infrastructure (idempotent) ───────────────────────────────────

echo "→ [1/5] Ensuring Artifact Registry repository '${AR_REPO}' ..."
if gcloud artifacts repositories describe "${AR_REPO}" \
    --location="${REGION}" \
    --project="${PROJECT_ID}" \
    --quiet 2>/dev/null; then
  echo "   Already exists — skipping creation."
else
  gcloud artifacts repositories create "${AR_REPO}" \
    --repository-format=docker \
    --location="${REGION}" \
    --description="Cortex MCP server container images" \
    --project="${PROJECT_ID}"
  echo "   Created."
fi

echo ""
echo "→ [2/5] Ensuring service account '${SA_NAME}' ..."
if gcloud iam service-accounts describe "${SA_EMAIL}" \
    --project="${PROJECT_ID}" \
    --quiet 2>/dev/null; then
  echo "   Already exists — skipping creation."
else
  gcloud iam service-accounts create "${SA_NAME}" \
    --display-name="Cortex MCP Server Service Account" \
    --project="${PROJECT_ID}"
  echo "   Created."
fi

echo ""
echo "→ [2a/5] Ensuring IAM roles for '${SA_EMAIL}' ..."
# NOTE: add-iam-policy-binding is additive — it never removes existing bindings.
for ROLE in \
  "roles/datastore.user" \
  "roles/secretmanager.secretAccessor" \
  "roles/logging.logWriter"; do
  echo "   Binding ${ROLE} ..."
  gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member="serviceAccount:${SA_EMAIL}" \
    --role="${ROLE}" \
    --condition=None \
    --quiet 2>/dev/null \
  || echo "   (binding may already exist — continuing)"
done

echo ""
echo "→ [3/5] Ensuring Firestore database '${FIRESTORE_DB}' ..."
if gcloud firestore databases describe \
    --database="${FIRESTORE_DB}" \
    --project="${PROJECT_ID}" \
    --quiet 2>/dev/null; then
  echo "   Already exists — skipping creation."
else
  gcloud firestore databases create \
    --database="${FIRESTORE_DB}" \
    --location="${REGION}" \
    --type=firestore-native \
    --project="${PROJECT_ID}"
  echo "   Created."
fi

# ── 2. Build ──────────────────────────────────────────────────────────────────

echo ""
echo "→ [4/5] Building container image via Cloud Build ..."
echo "   Image: ${IMAGE}"
gcloud builds submit \
  --tag "${IMAGE}" \
  --project "${PROJECT_ID}" \
  "${ROOT_DIR}"

# ── 3. Deploy ─────────────────────────────────────────────────────────────────

echo ""
echo "→ [5/5] Deploying Cloud Run service '${SERVICE_NAME}' ..."
# NOTE: --update-env-vars is used (not --set-env-vars) so any existing env vars
#       not listed here are preserved, not wiped.
gcloud run deploy "${SERVICE_NAME}" \
  --image "${IMAGE}" \
  --region "${REGION}" \
  --service-account "${SA_EMAIL}" \
  --ingress all \
  --no-allow-unauthenticated \
  --update-env-vars "\
GCP_PROJECT_ID=${PROJECT_ID},\
GCP_REGION=${REGION},\
FIRESTORE_DATABASE=${FIRESTORE_DB}" \
  --cpu 1 \
  --memory 512Mi \
  --min-instances 0 \
  --max-instances 5 \
  --timeout 3600 \
  --project "${PROJECT_ID}"

# ── 4. Grant Flow workers invocation access ───────────────────────────────────

echo ""
echo "→ Granting invocation access to Flow SA '${FLOW_SA}' ..."
# NOTE: add-iam-policy-binding is additive — never removes existing bindings.
gcloud run services add-iam-policy-binding "${SERVICE_NAME}" \
  --region="${REGION}" \
  --member="serviceAccount:${FLOW_SA}" \
  --role="roles/run.invoker" \
  --project="${PROJECT_ID}" \
  --quiet \
|| echo "   (binding may already exist — continuing)"

# ── 5. Results ────────────────────────────────────────────────────────────────

CORTEX_URL=$(gcloud run services describe "${SERVICE_NAME}" \
  --region="${REGION}" \
  --format="value(status.url)" \
  --project="${PROJECT_ID}")

echo ""
echo "════════════════════════════════════════════════════════════"
echo "  Deploy Complete"
echo "════════════════════════════════════════════════════════════"
echo "  Cortex URL : ${CORTEX_URL}"
echo ""
echo "  Next steps:"
echo ""
echo "  1. Upload your extracted data to Firestore:"
echo "     ./scripts/upload-to-firestore.sh ./cortex-output"
echo ""
echo "  2. Connect Flow workers (use --update-env-vars, NOT --set-env-vars):"
echo ""
echo "     gcloud run jobs update archon-product-worker-prod \\"
echo "       --region=${REGION} \\"
echo "       --update-env-vars CORTEX_URL=${CORTEX_URL} \\"
echo "       --project=${PROJECT_ID}"
echo ""
echo "  3. Grant individual team member access (for debugging):"
echo ""
echo "     gcloud run services add-iam-policy-binding ${SERVICE_NAME} \\"
echo "       --region=${REGION} \\"
echo "       --member=\"user:you@company.com\" \\"
echo "       --role=roles/run.invoker \\"
echo "       --project=${PROJECT_ID}"
echo "════════════════════════════════════════════════════════════"
