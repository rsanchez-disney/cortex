# ─────────────────────────────────────────────────────────────────────────────
# Cortex MCP Server — Cloud Run container
#
# Runs the MCP server in streamable-HTTP mode on port 8080 (Cloud Run default).
# Storage backend: Firestore (named database configured via FIRESTORE_DATABASE).
#
# Only the MCP server and the storage layer are included in this image.
# Extractor code (cli.py, schema.py, aggregator.py, validation.py, extractors/)
# is intentionally excluded — it runs locally / in CI, never on Cloud Run.
#
# Build locally:
#   docker build -t cortex .
#
# Run locally (requires ADC credentials):
#   docker run -p 8080:8080 \
#     -e GCP_PROJECT_ID=your-project \
#     -e FIRESTORE_DATABASE=cortex \
#     -v ~/.config/gcloud:/root/.config/gcloud:ro \
#     cortex
# ─────────────────────────────────────────────────────────────────────────────

FROM python:3.12-slim AS builder

# Install uv (pinned for reproducible builds)
COPY --from=ghcr.io/astral-sh/uv:0.11.15 /uv /usr/local/bin/uv

WORKDIR /app

# Copy dependency files first for better layer caching
COPY pyproject.toml uv.lock ./

# Install dependencies into /app/.venv (no dev extras)
RUN uv sync --frozen --no-dev

# ─────────────────────────────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

WORKDIR /app

# Copy the virtualenv from builder
COPY --from=builder /app/.venv /app/.venv

# ── MCP server source (only what's needed at runtime) ──────────────────────
# cortex.storage: StorageBackend ABC + LocalStorageBackend + GCSStorageBackend
# cortex.firestore_storage: FirestoreStorageBackend
# mcp_server/: server.py + __main__.py entry point
# Extractors, CLI, schema, aggregator, validation are NOT copied.
COPY src/cortex/__init__.py          ./src/cortex/__init__.py
COPY src/cortex/storage.py           ./src/cortex/storage.py
COPY src/cortex/firestore_storage.py ./src/cortex/firestore_storage.py
COPY mcp_server/                     ./mcp_server/

# Make the venv the default Python environment
ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONPATH="/app/src"

# Cloud Run injects PORT at runtime (default: 8080)
ENV PORT=8080

# Required at runtime — set via Cloud Run env vars, not baked in
ENV GCP_PROJECT_ID=""
ENV GCP_REGION="us-central1"
ENV FIRESTORE_DATABASE="cortex"
ENV STORAGE_BACKEND="firestore"

# Non-root user for security
RUN useradd --no-create-home --shell /bin/false cortex
USER cortex

EXPOSE 8080

# Start MCP server directly via mcp_server/__main__.py.
# This bypasses cortex.cli entirely, so no extractor code is imported.
# PORT, STORAGE_BACKEND, and FIRESTORE_DATABASE are read from env vars.
CMD ["python", "-m", "mcp_server"]
