"""Webhook endpoint for triggering single-repo extraction.

Designed to be called by Azure DevOps / GitHub webhooks on push to main.
Extracts only the repo that changed, updates its manifest, and re-aggregates.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import structlog
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from cortex.extractors import ExtractorError, get_extractor
from cortex.schema import ExtractionError
from cortex.storage import StorageBackend
from cortex.validation import ValidationError, validate_service_yaml

logger = structlog.get_logger()


async def handle_extract(request: Request) -> JSONResponse:
    """Handle a webhook extraction request.

    Expected JSON body:
    {
        "name": "service-name",
        "url": "https://dev.azure.com/org/project/_git/repo",  // or "path": "/local/path"
        "type": "backend-java",
        "owner": "team-name",
        "domain": "domain",
        "tier": "standard",
        "purpose": "description",
        "branch": "main"  // optional
    }
    """
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)

    required = ["name", "type", "owner", "domain", "tier", "purpose"]
    missing = [f for f in required if f not in body]
    if missing:
        return JSONResponse({"error": f"missing fields: {missing}"}, status_code=400)

    if "url" not in body and "path" not in body:
        return JSONResponse({"error": "must provide either url or path"}, status_code=400)

    # Get storage from app state
    storage: StorageBackend = request.app.state.storage
    name = body["name"]

    try:
        # Resolve repo path
        clone_dir = None
        if "path" in body:
            repo_path = Path(body["path"])
        else:
            # Clone from URL
            azure_pat = os.environ.get("AZURE_PAT", "")
            if not azure_pat:
                return JSONResponse(
                    {"error": "AZURE_PAT required for URL-based extraction"}, status_code=400
                )
            from cortex.repo_cloner import inject_pat

            clone_dir = tempfile.mkdtemp(prefix=f"cortex-webhook-{name}-")
            repo_path = Path(clone_dir) / name
            import subprocess

            auth_url = inject_pat(body["url"], azure_pat)
            branch_args = ["--branch", body["branch"]] if "branch" in body else []
            result = subprocess.run(
                ["git", "clone", "--depth", "1"] + branch_args + [auth_url, str(repo_path)],
                capture_output=True,
                text=True,
                timeout=120,
            )
            if result.returncode != 0:
                return JSONResponse({"error": f"clone failed: {result.stderr}"}, status_code=500)

        # Validate and extract
        service_data = {k: v for k, v in body.items() if k not in ("url", "path", "branch")}
        service_yaml = validate_service_yaml(service_data)
        extractor = get_extractor(service_yaml.type)
        manifest = extractor.extract(repo_path, service_yaml)

        # Write manifest
        manifest_data = json.loads(manifest.model_dump_json())
        storage.write_json(f"services/{name}/manifest.json", manifest_data)

        # Re-aggregate
        from cortex.aggregator import aggregate

        graph = aggregate(storage)
        graph_data = json.loads(graph.model_dump_json())
        storage.write_json("graph/latest.json", graph_data)

        return JSONResponse(
            {
                "status": "success",
                "service": name,
                "endpoints": len([e for c in manifest.api_contracts for e in c.endpoints]),
                "confidence": getattr(manifest, "extraction_confidence", None),
            }
        )

    except (ValidationError, ExtractorError) as e:
        error = ExtractionError(
            repo=name, timestamp=datetime.now(UTC), error=str(e), phase="extraction"
        )
        storage.write_json(
            f"services/{name}/extraction-error.json", json.loads(error.model_dump_json())
        )
        return JSONResponse({"error": str(e)}, status_code=422)

    except Exception as e:
        logger.error("webhook_extraction_failed", service=name, error=str(e))
        return JSONResponse({"error": f"internal error: {str(e)}"}, status_code=500)

    finally:
        if clone_dir and Path(clone_dir).exists():
            shutil.rmtree(clone_dir, ignore_errors=True)


async def handle_health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


def create_webhook_app(
    storage_backend: str = "local", storage_bucket: str = "./cortex-output"
) -> Starlette:
    """Create the webhook Starlette app."""
    storage = StorageBackend.from_config(storage_backend, storage_bucket)

    app = Starlette(
        routes=[
            Route("/extract", handle_extract, methods=["POST"]),
            Route("/health", handle_health, methods=["GET"]),
        ]
    )
    app.state.storage = storage
    return app
