"""Aggregator — merges per-service manifests into a platform graph.

Reads all services/*/manifest.json from storage, builds a unified graph,
and handles failed extractions.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

import structlog

from cortex import __version__
from cortex.schema import (
    CommunicationGraph,
    EndpointIndex,
    EndpointParameter,
    EndpointRequestBody,
    EndpointResponse,
    ExtractionError,
    GraphEntry,
    GraphMetadata,
    PlatformGraph,
    ServiceEdge,
)
from cortex.storage import StorageBackend, StorageError

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

    # Collect all manifests in a single pass for both graph entry building and edge resolution
    all_manifests: list[dict] = []

    # Process each repo
    for repo_name, files in sorted(repos.items()):
        if files.get("has_manifest"):
            try:
                manifest_data = storage.read_json(f"services/{repo_name}/manifest.json")
                all_manifests.append(manifest_data)
                entry = _manifest_to_graph_entry(manifest_data)
                services.append(entry)
                logger.info("added service to graph", repo=repo_name)
            except (StorageError, KeyError, ValueError) as e:
                logger.error("failed to process manifest", repo=repo_name, error=str(e))
                failed.append(
                    ExtractionError(
                        repo=repo_name,
                        timestamp=datetime.now(UTC),
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
                        timestamp=datetime.now(UTC),
                        error="Failed to read extraction error file",
                        phase="aggregation",
                    )
                )

    # Resolve cross-service communication edges
    kafka_edges = _resolve_kafka_edges(all_manifests)
    http_edges = _resolve_http_edges(all_manifests, services)
    api_call_edges = _resolve_api_call_edges(all_manifests, services)

    # Fallback: for mobile services with zero path-matched edges, try interface-name matching
    path_matched_callers = {e.source for e in api_call_edges}
    iface_edges = _resolve_api_call_edges_by_interface(all_manifests, services)
    # Deduplicate: skip interface-name edges for services that already have path-match edges
    # and skip (source, target) pairs already covered by path-match edges
    path_edge_pairs = {(e.source, e.target) for e in api_call_edges}
    deduped_iface_edges = [
        e for e in iface_edges
        if e.source not in path_matched_callers
        or (e.source, e.target) not in path_edge_pairs
    ]

    communication = CommunicationGraph(
        edges=kafka_edges + http_edges + api_call_edges + deduped_iface_edges
    )

    graph = PlatformGraph(
        services=services,
        communication=communication,
        failed_extractions=failed,
        metadata=GraphMetadata(
            timestamp=datetime.now(UTC),
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
            # Build parameter/request_body/response from raw dicts
            params = [
                EndpointParameter(**p) for p in ep.get("parameters", [])
            ]
            raw_body = ep.get("request_body")
            req_body = EndpointRequestBody(**raw_body) if raw_body else None
            raw_resp = ep.get("response")
            resp = EndpointResponse(**raw_resp) if raw_resp else None

            endpoints.append(
                EndpointIndex(
                    method=ep.get("method"),
                    path=ep.get("path"),
                    summary=ep.get("summary"),
                    tags=ep.get("tags", []),
                    operation_id=ep.get("operation_id"),
                    parameters=params,
                    request_body=req_body,
                    response=resp,
                )
            )

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
        endpoints=endpoints,
        module_count=module_count,
        permissions=manifest.get("permissions", []),
        gradle_plugins=manifest.get("gradle_plugins", []),
        ci=manifest.get("ci"),
        framework=manifest.get("framework"),
        kafka_produces=manifest.get("kafka_produces", []),
        kafka_consumes=manifest.get("kafka_consumes", []),
    )


def _extract_topic_name(raw: str) -> str:
    """Extract the effective topic name from a raw topic reference.

    Handles:
    - "${VAR:default-topic-name}" → "default-topic-name"
    - "${VAR}" → "VAR"  (no default — keep var name for dedup only)
    - "plain-topic-name" → "plain-topic-name"
    - "CONSTANT_NAME" → "CONSTANT_NAME" (UPPER_SNAKE — returned as-is)
    """
    m = re.fullmatch(r"\$\{([^}]+)\}", raw.strip())
    if m:
        inner = m.group(1)
        if ":" in inner:
            return inner.split(":", 1)[1]  # use the default value
        return inner  # no default — keep env var name
    return raw


def _resolve_kafka_edges(manifests: list[dict]) -> list[ServiceEdge]:
    """Build Kafka communication edges from producer/consumer topic data.

    For each topic, creates ServiceEdge objects linking producers to consumers.
    If a topic has producers but no consumers (or vice versa), no edge is created
    since the counterpart is outside Cortex-tracked services.

    Topic names stored as ``${VAR:default}`` are resolved to their default value
    before matching, so producer/consumer sides with different Spring EL variable
    names can still be matched by shared default topic string.
    """
    # Map: effective_topic_name → list of producer service names
    topic_producers: dict[str, list[str]] = {}
    # Map: effective_topic_name → list of consumer service names
    topic_consumers: dict[str, list[str]] = {}

    for manifest in manifests:
        service_name = manifest.get("name", "")
        if not service_name:
            continue

        for topic in manifest.get("kafka_produces", []):
            if topic:
                effective = _extract_topic_name(topic)
                topic_producers.setdefault(effective, []).append(service_name)

        for topic in manifest.get("kafka_consumes", []):
            if topic:
                effective = _extract_topic_name(topic)
                topic_consumers.setdefault(effective, []).append(service_name)

    edges: list[ServiceEdge] = []
    all_topics = set(topic_producers) | set(topic_consumers)

    for topic in sorted(all_topics):
        producers = topic_producers.get(topic, [])
        consumers = topic_consumers.get(topic, [])

        # Only create edges when both sides are known within Cortex-tracked services
        for producer in producers:
            for consumer in consumers:
                edges.append(
                    ServiceEdge(
                        source=producer,
                        target=consumer,
                        protocol="kafka",
                        detail=topic,
                        confidence=0.9,
                    )
                )

    return edges


def _resolve_http_edges(manifests: list[dict], services: list[GraphEntry]) -> list[ServiceEdge]:
    """Build HTTP communication edges from outbound_calls data.

    Matches target_url hostnames and config key segments against known service names.
    """
    service_names = {s.name for s in services}
    edges: list[ServiceEdge] = []
    seen: set[tuple[str, str]] = set()

    for manifest in manifests:
        caller = manifest.get("name", "")
        if not caller:
            continue

        for call in manifest.get("outbound_calls", []):
            target_url = call.get("target_url") or ""
            config_key = call.get("config_key") or ""
            target_service = call.get("target_service")

            # Try to resolve target service if not already set
            if not target_service:
                # Check URL hostname for service name hints
                url_match = re.search(r"https?://([^/:]+)", target_url)
                if url_match:
                    hostname = url_match.group(1)
                    for svc_name in service_names:
                        # Strip common suffixes and check for substring match
                        base = re.sub(r"[-_](microservice|service|ms|api|svc)$", "", svc_name)
                        if base and base in hostname:
                            target_service = svc_name
                            break

                # Fall back to config key segments
                if not target_service and config_key:
                    key_parts = re.split(r"[.\-_]", config_key.lower())
                    for svc_name in service_names:
                        svc_base = re.sub(
                            r"[-_](microservice|service|ms|api|svc)$", "", svc_name
                        )
                        if svc_base and any(svc_base in part for part in key_parts):
                            target_service = svc_name
                            break

            if target_service and target_service in service_names and target_service != caller:
                edge_key = (caller, target_service)
                if edge_key not in seen:
                    seen.add(edge_key)
                    edges.append(
                        ServiceEdge(
                            source=caller,
                            target=target_service,
                            protocol="http",
                            detail=target_url or config_key or None,
                            confidence=0.7,
                        )
                    )

    return edges


def _normalize_path(path: str) -> str:
    """Strip path parameters and unresolved variables for matching.

    Handles:
    - {param} path parameters: /v1/orders/{id} → /v1/orders
    - Unresolved $VARIABLE tokens: /$UNKNOWN/orders → /orders
    - Trailing slashes
    """
    # Strip unresolved $VAR and ${VAR} Kotlin template references
    path = re.sub(r"\$\{?[A-Za-z_]\w*\}?", "", path)
    # Strip {param} REST path parameters
    path = re.sub(r"\{[^}]+\}", "", path)
    # Collapse any double slashes introduced by variable removal
    path = re.sub(r"/{2,}", "/", path)
    return path.rstrip("/")


def _resolve_api_call_edges(
    manifests: list[dict], services: list[GraphEntry]
) -> list[ServiceEdge]:
    """Build HTTP edges from mobile api_calls to backend services by path matching."""
    edges: list[ServiceEdge] = []
    seen: set[tuple[str, str, str]] = set()

    # Collect backend endpoints: service_name → list of (method, path)
    backend_endpoints: dict[str, list[tuple[str, str]]] = {}
    for svc in services:
        svc_type = svc.type
        if svc_type in ("android", "ios"):
            continue
        endpoints = [(ep.method or "", ep.path or "") for ep in svc.endpoints if ep.path]
        if endpoints:
            backend_endpoints[svc.name] = endpoints

    for manifest in manifests:
        caller = manifest.get("name", "")
        svc_type = manifest.get("type", "")
        if not caller or svc_type not in ("android", "ios"):
            continue

        for call in manifest.get("api_calls", []):
            method = (call.get("method") or "").upper()
            path = call.get("path") or ""
            if not path:
                continue

            normalized_call_path = _normalize_path(path)

            for backend_name, endpoints in backend_endpoints.items():
                for ep_method, ep_path in endpoints:
                    if method and ep_method and method != ep_method.upper():
                        continue
                    normalized_ep_path = _normalize_path(ep_path)
                    if normalized_call_path and normalized_ep_path:
                        # Match strategy: mobile paths include a routing prefix segment
                        # (e.g. /ticketing/v1/games, /commerce/v1/orders) while backend
                        # paths start at /v1/... The mobile prefix is the microservice
                        # routing segment; strip it before comparing to backend path.
                        #
                        # Only match if the mobile path's suffix (after stripping the
                        # first non-version segment) equals the backend endpoint path,
                        # AND the stripped prefix aligns with the backend service name.
                        match = False
                        if normalized_call_path == normalized_ep_path:
                            match = True
                        else:
                            # Try stripping the mobile routing prefix (first segment after /)
                            # e.g. /ticketing/v1/games → /v1/games
                            call_parts = normalized_call_path.lstrip("/").split("/")
                            if len(call_parts) > 1:
                                suffix = "/" + "/".join(call_parts[1:])
                                if suffix == normalized_ep_path and len(normalized_ep_path) >= 6:
                                    # Verify the prefix segment matches the backend service
                                    mobile_prefix = call_parts[0].lower()
                                    svc_stem = re.sub(
                                        r"[_-](microservice|service|ms|api|svc)$",
                                        "", backend_name, flags=re.IGNORECASE
                                    ).lower().replace("-", "").replace("_", "")
                                    if mobile_prefix and (
                                        mobile_prefix == svc_stem
                                        or mobile_prefix in svc_stem
                                        or svc_stem in mobile_prefix
                                    ):
                                        match = True
                        if match:
                            edge_key = (caller, backend_name, path)
                            if edge_key not in seen:
                                seen.add(edge_key)
                                edges.append(
                                    ServiceEdge(
                                        source=caller,
                                        target=backend_name,
                                        protocol="http",
                                        detail=f"{method} {path}" if method else path,
                                        confidence=0.7,
                                    )
                                )
                            break

    return edges


def _resolve_api_call_edges_by_interface(
    manifests: list[dict], services: list[GraphEntry]
) -> list[ServiceEdge]:
    """Fallback: match mobile interface names to backend service names heuristically.

    When path-based matching produces no edges (e.g. because Kotlin string template
    constants couldn't be resolved), interface names like ``TicketingApi`` are stemmed
    (``ticketing``) and matched against backend service name stems
    (``ticketing-microservice`` → ``ticketing``).

    Produces edges at confidence=0.5 since the match is heuristic.

    Handles multi-word interface names by splitting on camelCase boundaries and
    checking each word component against backend stems. E.g. ``PublicHttpApi``
    splits into [``public``, ``http``] — if any of these match a backend stem,
    an edge is created.
    """
    # Build backend service stem index
    # Strip common mobile/backend suffixes to get the bare service name
    iface_suffixes = re.compile(
        r'(HttpApi|Api|Service|Endpoint|Client|Repository|Repo)$'
    )
    svc_suffixes = re.compile(
        r'[_-](microservice|service|ms|api|svc)$', re.IGNORECASE
    )

    service_names = {s.name for s in services}
    # backend_stems: stem → service_name (skip mobile types)
    backend_stems: dict[str, str] = {}
    for svc in services:
        if svc.type in ("android", "ios"):
            continue
        stem = svc_suffixes.sub("", svc.name).lower().replace("-", "").replace("_", "")
        if stem:
            backend_stems[stem] = svc.name

    def _interface_stems(iface_name: str) -> list[str]:
        """Split camelCase interface name into candidate match stems."""
        # Strip known suffixes first
        stripped = iface_suffixes.sub("", iface_name)
        # CamelCase split: ["Ticketing"] or ["Public", "Http"]
        words = re.findall(r'[A-Z][a-z0-9]*|[a-z0-9]+', stripped)
        stems = []
        # Full stripped name (lowercased, no separators)
        full = stripped.lower()
        if full:
            stems.append(full)
        # Each individual word component longer than 2 chars
        for w in words:
            w_lower = w.lower()
            if len(w_lower) > 2 and w_lower not in stems:
                stems.append(w_lower)
        return stems

    edges: list[ServiceEdge] = []
    seen: set[tuple[str, str]] = set()

    for manifest in manifests:
        caller = manifest.get("name", "")
        svc_type = manifest.get("type", "")
        if not caller or svc_type not in ("android", "ios"):
            continue

        for call in manifest.get("api_calls", []):
            iface_name = call.get("interface_name") or ""
            if not iface_name:
                continue

            matched = False
            for iface_stem in _interface_stems(iface_name):
                if matched:
                    break
                # Check each backend stem: exact match OR one is a prefix of the other
                # (e.g. "payment" matches backend stem "payments" and vice versa)
                for backend_stem, backend_name in backend_stems.items():
                    if backend_name not in service_names or backend_name == caller:
                        continue
                    stems_match = (
                        iface_stem == backend_stem
                        or iface_stem.startswith(backend_stem)
                        or backend_stem.startswith(iface_stem)
                    )
                    if stems_match:
                        edge_key = (caller, backend_name)
                        if edge_key not in seen:
                            seen.add(edge_key)
                            edges.append(
                                ServiceEdge(
                                    source=caller,
                                    target=backend_name,
                                    protocol="http",
                                    detail=f"interface:{iface_name}",
                                    confidence=0.5,
                                )
                            )
                        matched = True
                        break  # One match per call is enough

    return edges
