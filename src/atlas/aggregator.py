"""Aggregator — merges per-service manifests into a platform graph.

Reads all services/*/manifest.json from storage, builds a unified graph,
and handles failed extractions.
"""

from __future__ import annotations

from datetime import datetime, timezone

import structlog

from atlas import __version__
from atlas.schema import (
    EndpointIndex,
    ExtractionError,
    GraphEntry,
    GraphMetadata,
    PlatformGraph,
)
from atlas.storage import StorageBackend, StorageError

logger = structlog.get_logger()


def aggregate(storage: StorageBackend) -> PlatformGraph:
    """Aggregate all service manifests from storage into a PlatformGraph.

    1. List all services/*/manifest.json from storage
    2. Read each manifest
    3. For repos with only extraction-error.json, include in failed_extractions
    4. Build the graph with lightweight GraphEntry summaries

    Args:
        storage: The storage backend to read from

    Returns:
        A PlatformGraph containing all services and failures
    """
    services: list[GraphEntry] = []
    failed: list[ExtractionError] = []

    # List all files under services/
    all_files = storage.list("services")

    # Group by repo name
    repos: dict[str, dict[str, bool]] = {}
    for file_path in all_files:
        parts = file_path.split("/")
        if len(parts) >= 3 and parts[0] == "services":
            repo_name = parts[1]
            filename = parts[2]
            if repo_name not in repos:
                repos[repo_name] = {}
            if filename == "manifest.json":
                repos[repo_name]["has_manifest"] = True
            elif filename == "extraction-error.json":
                repos[repo_name]["has_error"] = True

    # Process each repo
    for repo_name, files in sorted(repos.items()):
        if files.get("has_manifest"):
            try:
                manifest_data = storage.read_json(f"services/{repo_name}/manifest.json")
                entry = _manifest_to_graph_entry(manifest_data)
                services.append(entry)
                logger.info("added service to graph", repo=repo_name)
            except (StorageError, KeyError, ValueError) as e:
                logger.error("failed to process manifest", repo=repo_name, error=str(e))
                failed.append(
                    ExtractionError(
                        repo=repo_name,
                        timestamp=datetime.now(timezone.utc),
                        error=f"Failed to process manifest: {e}",
                        phase="aggregation",
                    )
                )
        elif files.get("has_error"):
            # Only error file, no manifest
            try:
                error_data = storage.read_json(f"services/{repo_name}/extraction-error.json")
                failed.append(
                    ExtractionError(
                        repo=error_data.get("repo", repo_name),
                        timestamp=datetime.fromisoformat(error_data["timestamp"]),
                        error=error_data.get("error", "unknown"),
                        phase=error_data.get("phase", "unknown"),
                    )
                )
            except Exception:
                failed.append(
                    ExtractionError(
                        repo=repo_name,
                        timestamp=datetime.now(timezone.utc),
                        error="Failed to read extraction error file",
                        phase="aggregation",
                    )
                )

    graph = PlatformGraph(
        services=services,
        failed_extractions=failed,
        metadata=GraphMetadata(
            timestamp=datetime.now(timezone.utc),
            version=__version__,
            service_count=len(services),
        ),
    )

    logger.info(
        "aggregation complete",
        services=len(services),
        failures=len(failed),
    )

    return graph


def _manifest_to_graph_entry(manifest: dict) -> GraphEntry:
    """Convert a full manifest dict to a lightweight GraphEntry."""
    # Extract endpoint summaries from api_contracts
    endpoints: list[EndpointIndex] = []
    for contract in manifest.get("api_contracts", []):
        for ep in contract.get("endpoints", []):
            endpoints.append(
                EndpointIndex(
                    method=ep.get("method"),
                    path=ep.get("path"),
                    summary=ep.get("summary"),
                    tags=ep.get("tags", []),
                    operation_id=ep.get("operation_id"),
                )
            )

    # Extract dependency names only (lightweight)
    dep_names = [d["name"] for d in manifest.get("dependencies", []) if "name" in d]

    # Compute lightweight module count (full module details stay in manifest)
    module_count = len(manifest.get("modules", []))

    return GraphEntry(
        name=manifest["name"],
        type=manifest["type"],
        owner=manifest["owner"],
        domain=manifest["domain"],
        tier=manifest["tier"],
        status=manifest.get("status", "active"),
        purpose=manifest["purpose"],
        keywords=manifest.get("keywords", []),
        language=manifest.get("language"),
        dependencies=dep_names,
        endpoints=endpoints,
        module_count=module_count,
        permissions=manifest.get("permissions", []),
        gradle_plugins=manifest.get("gradle_plugins", []),
        ci=manifest.get("ci"),
    )
