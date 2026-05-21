"""Direct entry point for the Cortex MCP server.

Starts the server in streamable-HTTP mode without going through the CLI (cortex.cli),
so none of the extractor, schema, validation, or aggregator code is loaded.

Environment variables:
    STORAGE_BACKEND     Storage backend type (default: "firestore")
    FIRESTORE_DATABASE  Firestore named database (default: "cortex")
    PORT                HTTP port injected by Cloud Run (default: "8080")

Usage:
    python -m mcp_server
"""

from __future__ import annotations

import asyncio
import os

from mcp_server.server import create_server


def main() -> None:
    storage_backend = os.environ.get("STORAGE_BACKEND", "firestore")
    storage_bucket = os.environ.get("FIRESTORE_DATABASE", "cortex")
    port = int(os.environ.get("PORT", "8080"))

    server = create_server(
        storage_backend=storage_backend,
        storage_bucket=storage_bucket,
    )

    asyncio.run(server.run_http(host="0.0.0.0", port=port))


if __name__ == "__main__":
    main()
