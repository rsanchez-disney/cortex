"""Platform Atlas MCP server — exposes 4 tools for querying the platform graph.

Tools:
1. find_relevant_services — keyword-based service discovery
2. list_endpoints — endpoint index for a service
3. get_service_context — deep context on a single service
4. get_endpoint_contract — full endpoint schema (deferred for mobile)

Supports both stdio and HTTP/SSE modes.
"""

from __future__ import annotations

import json
import re
import time
from datetime import UTC, datetime
from typing import Any

import structlog
from mcp.server.fastmcp import FastMCP

from atlas.storage import StorageBackend, StorageError

logger = structlog.get_logger()

# Stop words for keyword matching
STOP_WORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "have",
        "has",
        "had",
        "do",
        "does",
        "did",
        "will",
        "would",
        "could",
        "should",
        "may",
        "might",
        "shall",
        "can",
        "need",
        "must",
        "dare",
        "to",
        "of",
        "in",
        "for",
        "on",
        "with",
        "at",
        "by",
        "from",
        "as",
        "into",
        "through",
        "during",
        "before",
        "after",
        "above",
        "below",
        "between",
        "out",
        "off",
        "over",
        "under",
        "again",
        "further",
        "then",
        "once",
        "here",
        "there",
        "when",
        "where",
        "why",
        "how",
        "all",
        "each",
        "every",
        "both",
        "few",
        "more",
        "most",
        "other",
        "some",
        "such",
        "no",
        "nor",
        "not",
        "only",
        "own",
        "same",
        "so",
        "than",
        "too",
        "very",
        "just",
        "because",
        "but",
        "and",
        "or",
        "if",
        "while",
        "about",
        "up",
        "it",
        "its",
        "i",
        "me",
        "my",
        "we",
        "our",
        "you",
        "your",
        "he",
        "him",
        "his",
        "she",
        "her",
        "they",
        "them",
        "what",
        "which",
        "who",
        "whom",
        "this",
        "that",
        "these",
        "those",
        "am",
        # discovery/listing words that carry no signal for keyword matching
        "get",
        "list",
        "show",
        "find",
        "fetch",
        "give",
        "tell",
        "project",
        "projects",
        "service",
        "services",
        "repo",
        "repos",
        "available",
    }
)


class AtlasMCPServer:
    """The Atlas MCP server, wrapping a FastMCP instance."""

    def __init__(self, storage: StorageBackend, refresh_interval_minutes: int = 15):
        self._storage = storage
        self._refresh_interval = refresh_interval_minutes * 60
        self._graph: dict | None = None
        self._manifest_cache: dict[str, tuple[dict, float]] = {}  # name -> (data, expiry)
        self._cache_ttl = 3600  # 1 hour

        self._mcp = FastMCP("Platform Atlas")
        self._register_tools()

    def _register_tools(self) -> None:
        """Register all 4 MCP tools."""

        @self._mcp.tool()
        async def find_relevant_services(
            task_description: str,
            max_results: int = 5,
        ) -> dict[str, Any]:
            """Given a free-text task description, return the most likely services to be involved.

            Uses keyword matching against service names, keywords, purpose, and domain.
            Returns a ranked list of candidates with scores.
            """
            start = time.time()
            graph = await self._ensure_graph()

            tokens = _tokenize(task_description)
            if not tokens:
                # No meaningful tokens — return all services (unscored listing query)
                comm_edges_all = graph.get("communication", {}).get("edges", [])
                all_svcs = []
                for svc in graph.get("services", []):
                    svc_name = svc["name"]
                    neighbor_names: set[str] = set()
                    for edge in comm_edges_all:
                        if edge.get("source") == svc_name:
                            neighbor_names.add(edge.get("target", ""))
                        elif edge.get("target") == svc_name:
                            neighbor_names.add(edge.get("source", ""))
                    neighbor_names.discard("")
                    all_svcs.append(
                        {
                            "name": svc_name,
                            "type": svc["type"],
                            "domain": svc["domain"],
                            "purpose": svc["purpose"],
                            "score": 1.0,
                            "matched_on": ["all"],
                            "communicates_with": sorted(neighbor_names),
                        }
                    )
                all_svcs = all_svcs[:max_results]
                result = {"candidates": all_svcs}
                await self._log_query(
                    "find_relevant_services",
                    {"task_description": task_description},
                    {"num_candidates": len(all_svcs)},
                    start,
                )
                return result

            candidates = []
            for svc in graph.get("services", []):
                score, matched_on = _score_service(svc, tokens)
                if score > 0:
                    candidates.append(
                        {
                            "name": svc["name"],
                            "type": svc["type"],
                            "domain": svc["domain"],
                            "purpose": svc["purpose"],
                            "score": round(score, 2),
                            "matched_on": matched_on,
                        }
                    )

            # Sort by score descending, take top N
            candidates.sort(key=lambda x: x["score"], reverse=True)
            candidates = candidates[:max_results]

            # Normalize scores to 0-1
            if candidates:
                max_score = candidates[0]["score"]
                if max_score > 0:
                    for c in candidates:
                        c["score"] = round(c["score"] / max_score, 2)

            # Enrich candidates with immediate communication neighbors
            comm_edges = graph.get("communication", {}).get("edges", [])
            for candidate in candidates:
                svc_name = candidate["name"]
                neighbor_names: set[str] = set()
                for edge in comm_edges:
                    if edge.get("source") == svc_name:
                        neighbor_names.add(edge.get("target", ""))
                    elif edge.get("target") == svc_name:
                        neighbor_names.add(edge.get("source", ""))
                neighbor_names.discard("")
                candidate["communicates_with"] = sorted(neighbor_names)

            result = {"candidates": candidates}
            await self._log_query(
                "find_relevant_services",
                {"task_description": task_description, "max_results": max_results},
                {
                    "num_candidates": len(candidates),
                    "top_score": candidates[0]["score"] if candidates else 0,
                },
                start,
            )
            return result

        @self._mcp.tool()
        async def list_endpoints(service: str) -> dict[str, Any]:
            """List all endpoints a service exposes. Used for browsing and existence checks.

            Returns the endpoint index from the graph — no OpenAPI file fetch.
            """
            start = time.time()
            graph = await self._ensure_graph()

            svc = _find_service(graph, service)
            if svc is None:
                result = {"error": f"Service '{service}' not found"}
                await self._log_query(
                    "list_endpoints", {"service": service}, {"error": True}, start
                )
                return result

            endpoints = svc.get("endpoints", [])
            result = {
                "service": service,
                "endpoints": endpoints,
            }
            await self._log_query(
                "list_endpoints",
                {"service": service},
                {"num_endpoints": len(endpoints)},
                start,
            )
            return result

        @self._mcp.tool()
        async def get_service_context(
            name: str,
            include: list[str] | None = None,
        ) -> dict[str, Any]:
            """Deep context on a single service. The main tool for agents orienting to a service.

            Args:
                name: Service name
                include: Sections to include. Default: ["manifest", "deps", "contracts", "notes"]
            """
            start = time.time()

            if include is None:
                include = ["manifest", "deps", "contracts", "notes", "communication"]

            graph = await self._ensure_graph()
            svc = _find_service(graph, name)
            if svc is None:
                result = {"error": f"Service '{name}' not found"}
                await self._log_query("get_service_context", {"name": name}, {"error": True}, start)
                return result

            # Fetch full manifest on demand
            manifest = await self._get_manifest(name)

            context: dict[str, Any] = {"name": name}

            if "manifest" in include and manifest:
                # Return subset of manifest (exclude large fields)
                context["manifest"] = {
                    k: v
                    for k, v in manifest.items()
                    if k not in ("dependencies", "api_contracts", "integration_notes")
                }

            if "deps" in include:
                if manifest:
                    context["direct_dependencies"] = [
                        d["name"] for d in manifest.get("dependencies", [])
                    ]
                else:
                    # Manifest not cached; dependency data is unavailable until re-extraction.
                    context["direct_dependencies"] = None

            if "contracts" in include:
                if manifest:
                    context["api_contracts"] = manifest.get("api_contracts", [])
                else:
                    context["api_contracts"] = []

            if "notes" in include:
                notes = manifest.get("integration_notes", []) if manifest else []
                global_notes = [n["note"] for n in notes if n.get("scope") == "global"]
                by_endpoint: dict[str, list[str]] = {}
                for n in notes:
                    if n.get("scope") and n["scope"] != "global":
                        by_endpoint.setdefault(n["scope"], []).append(n["note"])
                context["integration_notes"] = {
                    "global": global_notes,
                    "by_endpoint": by_endpoint,
                }

            if "communication" in include:
                comm = graph.get("communication", {})
                edges = comm.get("edges", [])

                # Filter edges involving this service
                calls_out = [e for e in edges if e.get("source") == name]
                called_by = [e for e in edges if e.get("target") == name]

                # Group Kafka edges by topic
                publishes_to: dict[str, list[str]] = {}
                for e in calls_out:
                    if e.get("protocol") == "kafka":
                        topic = e.get("detail", "unknown")
                        publishes_to.setdefault(topic, []).append(e.get("target", ""))

                subscribes_to: dict[str, list[str]] = {}
                for e in called_by:
                    if e.get("protocol") == "kafka":
                        topic = e.get("detail", "unknown")
                        subscribes_to.setdefault(topic, []).append(e.get("source", ""))

                context["communication"] = {
                    "publishes_to": [
                        {"topic": t, "consumers": c} for t, c in publishes_to.items()
                    ],
                    "subscribes_to": [
                        {"topic": t, "producers": p} for t, p in subscribes_to.items()
                    ],
                    "http_calls": [e for e in calls_out if e.get("protocol") == "http"],
                    "http_called_by": [e for e in called_by if e.get("protocol") == "http"],
                }

            await self._log_query(
                "get_service_context",
                {"name": name, "include": include},
                {"sections": list(context.keys())},
                start,
            )
            return context

        @self._mcp.tool()
        async def get_endpoint_contract(
            service: str,
            method: str,
            path: str,
        ) -> dict[str, Any]:
            """Return the full request/response schema for one endpoint.

            Note: For mobile service types (android, ios), this returns a message
            indicating no API spec is available, since mobile apps are consumers,
            not API providers. Full OpenAPI support is deferred until backend
            extractors are added.
            """
            start = time.time()
            graph = await self._ensure_graph()
            svc = _find_service(graph, service)

            if svc is None:
                result = {"error": f"Service '{service}' not found"}
                await self._log_query(
                    "get_endpoint_contract",
                    {"service": service, "method": method, "path": path},
                    {"error": True},
                    start,
                )
                return result

            svc_type = svc.get("type", "")
            if svc_type in ("android", "ios"):
                result = {
                    "service": service,
                    "method": method,
                    "path": path,
                    "message": (
                        f"No API spec available for service type '{svc_type}'. "
                        "Mobile apps are typically API consumers, not providers. "
                        "API contract details are available for backend services."
                    ),
                }
                await self._log_query(
                    "get_endpoint_contract",
                    {"service": service, "method": method, "path": path},
                    {"message": "no_api_spec_mobile"},
                    start,
                )
                return result

            # For backend services: try to fetch OpenAPI spec
            try:
                self._storage.read_bytes(f"services/{service}/openapi.yaml")
                result = {
                    "service": service,
                    "method": method,
                    "path": path,
                    "message": "OpenAPI parsing is deferred in v1. Raw spec available.",
                    "spec_available": True,
                }
            except StorageError:
                # Get integration notes and swagger_url for this endpoint
                manifest = await self._get_manifest(service)
                notes = []
                swagger_url = None
                if manifest:
                    swagger_url = manifest.get("swagger_url")
                    for n in manifest.get("integration_notes", []):
                        scope = n.get("scope", "")
                        if scope == f"{method} {path}" or scope == "global":
                            notes.append(n["note"])

                result = {
                    "service": service,
                    "method": method,
                    "path": path,
                    "message": (
                        f"Live Swagger/OpenAPI docs available at: {swagger_url}"
                        if swagger_url
                        else "No API spec file found for this service."
                    ),
                    "integration_notes": notes,
                }
                if swagger_url:
                    result["swagger_url"] = swagger_url

            await self._log_query(
                "get_endpoint_contract",
                {"service": service, "method": method, "path": path},
                {"has_spec": result.get("spec_available", False)},
                start,
            )
            return result

    async def _ensure_graph(self) -> dict:
        """Load graph if not cached."""
        if self._graph is None:
            await self._refresh_graph()
        assert self._graph is not None
        return self._graph

    async def _refresh_graph(self) -> None:
        """Reload graph from storage."""
        try:
            self._graph = self._storage.read_json("graph/latest.json")
            logger.info("graph loaded", service_count=len(self._graph.get("services", [])))
        except StorageError:
            logger.warning("graph/latest.json not found, using empty graph")
            self._graph = {"services": [], "failed_extractions": [], "metadata": {}}

    async def _get_manifest(self, name: str) -> dict | None:
        """Fetch a service manifest, with in-process caching (TTL: 1 hour)."""
        now = time.time()
        if name in self._manifest_cache:
            data, expiry = self._manifest_cache[name]
            if now < expiry:
                return data

        try:
            data = self._storage.read_json(f"services/{name}/manifest.json")
            self._manifest_cache[name] = (data, now + self._cache_ttl)
            return data
        except StorageError:
            return None

    async def _log_query(
        self, tool: str, input_data: dict, output_summary: dict, start_time: float
    ) -> None:
        """Log a tool call to storage for later review."""
        duration_ms = int((time.time() - start_time) * 1000)
        entry = {
            "timestamp": datetime.now(UTC).isoformat(),
            "tool": tool,
            "input": input_data,
            "output_summary": output_summary,
            "duration_ms": duration_ms,
        }

        date_str = datetime.now(UTC).strftime("%Y-%m-%d")
        log_key = f"logs/mcp/{date_str}.jsonl"

        try:
            # Append to existing log file
            try:
                existing = self._storage.read_bytes(log_key)
                new_content = existing + json.dumps(entry).encode() + b"\n"
            except StorageError:
                new_content = json.dumps(entry).encode() + b"\n"

            self._storage.write_bytes(log_key, new_content)
        except Exception:
            # Don't fail the tool call if logging fails
            logger.debug("failed to write MCP query log", tool=tool)

    async def run_stdio(self) -> None:
        """Run the server in stdio mode."""
        from mcp.server.stdio import stdio_server

        await self._refresh_graph()
        async with stdio_server() as (read_stream, write_stream):
            await self._mcp._mcp_server.run(
                read_stream,
                write_stream,
                self._mcp._mcp_server.create_initialization_options(),
            )

    async def run_http(self, host: str = "0.0.0.0", port: int = 8000) -> None:
        """Run the server in HTTP/SSE mode."""
        await self._refresh_graph()

        from mcp.server.sse import SseServerTransport
        from starlette.applications import Starlette
        from starlette.routing import Route

        sse = SseServerTransport("/messages/")

        async def handle_sse(request):
            async with sse.connect_sse(request.scope, request.receive, request._send) as streams:
                await self._mcp._mcp_server.run(
                    streams[0],
                    streams[1],
                    self._mcp._mcp_server.create_initialization_options(),
                )

        starlette_app = Starlette(
            routes=[
                Route("/sse", endpoint=handle_sse),
                Route("/messages/", endpoint=sse.handle_post_message, methods=["POST"]),
            ],
        )

        import uvicorn

        config = uvicorn.Config(starlette_app, host=host, port=port)
        server = uvicorn.Server(config)
        await server.serve()


def create_server(
    storage_backend: str = "local",
    storage_bucket: str = "./atlas-output",
    refresh_interval_minutes: int = 15,
) -> AtlasMCPServer:
    """Create and return an AtlasMCPServer instance."""
    storage = StorageBackend.from_config(storage_backend, storage_bucket)
    return AtlasMCPServer(
        storage=storage,
        refresh_interval_minutes=refresh_interval_minutes,
    )


# --- Utility functions for scoring ---


def _tokenize(text: str) -> set[str]:
    """Tokenize text: lowercase, split on non-alphanumeric, remove stop words."""
    words = re.findall(r"[a-z0-9]+", text.lower())
    return {w for w in words if w not in STOP_WORDS and len(w) > 1}


def _score_service(svc: dict, query_tokens: set[str]) -> tuple[float, list[str]]:
    """Score a service against query tokens. Returns (score, matched_on)."""
    score = 0.0
    matched_on: list[str] = []

    # Name match: highest weight (3x)
    name_tokens = _tokenize(svc.get("name", "").replace("-", " "))
    name_overlap = query_tokens & name_tokens
    if name_overlap:
        score += len(name_overlap) * 3.0
        matched_on.append("name")

    # Keywords match: high weight (2x)
    keyword_tokens: set[str] = set()
    for kw in svc.get("keywords", []):
        keyword_tokens |= _tokenize(kw)
    keyword_overlap = query_tokens & keyword_tokens
    if keyword_overlap:
        score += len(keyword_overlap) * 2.0
        matched_on.append("keywords")

    # Purpose match: medium weight (1.5x)
    purpose_tokens = _tokenize(svc.get("purpose", ""))
    purpose_overlap = query_tokens & purpose_tokens
    if purpose_overlap:
        score += len(purpose_overlap) * 1.5
        matched_on.append("purpose")

    # Domain match: medium weight (1.5x)
    domain_tokens = _tokenize(svc.get("domain", ""))
    domain_overlap = query_tokens & domain_tokens
    if domain_overlap:
        score += len(domain_overlap) * 1.5
        matched_on.append("domain")

    # Gradle plugins match: medium weight (1.5x) — e.g. "hilt", "compose", "firebase"
    plugin_tokens: set[str] = set()
    for plugin in svc.get("gradle_plugins", []):
        plugin_tokens |= _tokenize(plugin.replace(".", " ").replace("-", " "))
    plugin_overlap = query_tokens & plugin_tokens
    if plugin_overlap:
        score += len(plugin_overlap) * 1.5
        matched_on.append("gradle_plugins")

    # Module names match: low weight (1.0x)
    module_tokens: set[str] = set()
    for mod in svc.get("modules", []):
        mod_name = mod.get("name", "") if isinstance(mod, dict) else str(mod)
        module_tokens |= _tokenize(mod_name.replace(":", " ").replace("-", " "))
    module_overlap = query_tokens & module_tokens
    if module_overlap:
        score += len(module_overlap) * 1.0
        matched_on.append("modules")

    return score, matched_on


def _find_service(graph: dict, name: str) -> dict | None:
    """Find a service in the graph by name."""
    for svc in graph.get("services", []):
        if svc.get("name") == name:
            return svc
    return None



