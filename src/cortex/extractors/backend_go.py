"""Backend Go extractor — parses Go service repos to produce a ServiceManifest.

Extracts:
- Module name, Go version, and dependencies from go.mod
- HTTP framework detection (chi, gin, echo, gorilla/mux, net/http)
- HTTP route registrations from .go source files
- gRPC service definitions from .proto files
- Database type from dependencies (pgx, gorm, mongo-driver, etc.)
- Cache type from dependencies (go-redis, etc.)
- Kafka topics (produces/consumes) from .go source files
- Outbound HTTP calls (http.Get/Post, resty, etc.)
- Dockerfile runtime info
- Source repo git info (remote URL + HEAD commit)
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

import structlog

from cortex.extractors.base import Extractor
from cortex.extractors.utils import (
    detect_cache_type,
    detect_database_type,
    find_openapi_spec,
    parse_dockerfile,
    parse_go_mod,
    scan_kafka_topics,
    scan_outbound_urls,
)
from cortex.schema import (
    ApiContract,
    Dependency,
    EndpointIndex,
    ServiceManifest,
    ServiceYaml,
)

logger = structlog.get_logger()

# --- HTTP framework detection ---

_FRAMEWORK_MARKERS: dict[str, str] = {
    "github.com/go-chi/chi": "chi",
    "github.com/gin-gonic/gin": "gin",
    "github.com/labstack/echo": "echo",
    "github.com/gorilla/mux": "gorilla/mux",
}

# --- HTTP route patterns per framework ---

# chi: r.Get("/path", handler), r.Post("/path", handler), etc.
_CHI_ROUTE_RE = re.compile(
    r"""\.\s*(Get|Post|Put|Delete|Patch|Head|Options)\s*\(\s*["'`]([^"'`]+)["'`]""",
)

# gin: r.GET("/path", handler), r.POST("/path", handler), etc.
_GIN_ROUTE_RE = re.compile(
    r"""\.\s*(GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS)\s*\(\s*["'`]([^"'`]+)["'`]""",
)

# echo: e.GET("/path", handler), e.POST("/path", handler), etc.
_ECHO_ROUTE_RE = re.compile(
    r"""\.\s*(GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS)\s*\(\s*["'`]([^"'`]+)["'`]""",
)

# net/http: http.HandleFunc("/path", handler)
_NET_HTTP_ROUTE_RE = re.compile(
    r"""http\.HandleFunc\s*\(\s*["'`]([^"'`]+)["'`]""",
)

# chi group/route: r.Route("/prefix", func(...) { ... })
_CHI_GROUP_RE = re.compile(
    r"""\.\s*Route\s*\(\s*["'`]([^"'`]+)["'`]""",
)

# --- gRPC proto patterns ---

_GRPC_SERVICE_RE = re.compile(
    r"""service\s+(\w+)\s*\{""",
)
_GRPC_RPC_RE = re.compile(
    r"""rpc\s+(\w+)\s*\(\s*(\w+)\s*\)\s*returns\s*\(\s*(\w+)\s*\)""",
)


class BackendGoExtractor(Extractor):
    """Extractor for Go backend service repositories."""

    type = "backend-go"

    def extract(self, repo_path: Path, service_yaml: ServiceYaml) -> ServiceManifest:
        """Extract metadata from a Go backend repo."""
        # Respect extractor_hints.project_root
        effective_root = repo_path
        if service_yaml.extractor_hints and service_yaml.extractor_hints.project_root:
            effective_root = repo_path / service_yaml.extractor_hints.project_root

        # 1. Parse go.mod
        go_mod = parse_go_mod(effective_root)
        module_name = go_mod["module"] if go_mod else None
        go_version = go_mod["go_version"] if go_mod else None
        dependencies: list[Dependency] = go_mod["dependencies"] if go_mod else []

        # 2. Detect HTTP framework
        framework = self._detect_framework(dependencies)

        # 3. Scan HTTP handler registrations
        endpoints = self._scan_http_routes(effective_root, framework)

        # 4. Scan gRPC service definitions
        grpc_contracts = self._scan_grpc_services(effective_root)

        # 5. Detect database type
        database_type = detect_database_type(dependencies)

        # 6. Detect cache type
        cache_type = detect_cache_type(dependencies)

        # 7. Scan Kafka topics
        kafka_produces, kafka_consumes = scan_kafka_topics(effective_root, extensions=(".go",))

        # 8. Scan outbound HTTP calls
        outbound_urls = scan_outbound_urls(effective_root, extensions=(".go",))

        # 9. Parse Dockerfile
        runtime = parse_dockerfile(effective_root)

        # 10. Build API contracts
        api_contracts = self.find_api_contracts(effective_root)

        # Build the manifest
        manifest = ServiceManifest(
            name=service_yaml.name,
            type=service_yaml.type,
            owner=service_yaml.owner,
            domain=service_yaml.domain,
            tier=service_yaml.tier,
            status=service_yaml.status,
            purpose=service_yaml.purpose,
            keywords=service_yaml.keywords,
            language="Go",
            language_version=go_version,
            slack=service_yaml.slack,
            runbook=service_yaml.runbook,
            jira_component=service_yaml.jira_component,
            framework=framework,
            database_type=database_type,
            cache_type=cache_type,
            kafka_produces=kafka_produces,
            kafka_consumes=kafka_consumes,
            dependencies=dependencies,
            api_contracts=api_contracts,
            runtime=runtime,
            integration_notes=service_yaml.integration_notes,
            swagger_url=service_yaml.swagger_url,
            extracted_at=datetime.now(UTC),
            extractor_version="1.0.0",
        )

        # 11. Enrich with context and source repo
        self._enrich_with_context(manifest, repo_path)
        manifest.source_repo = self._get_source_repo(repo_path)

        return manifest

    def find_api_contracts(self, repo_path: Path) -> list[ApiContract]:
        """Find API contract files (OpenAPI specs and gRPC proto definitions)."""
        contracts: list[ApiContract] = []

        # Check for OpenAPI spec
        openapi_path = find_openapi_spec(repo_path)
        if openapi_path:
            contracts.append(
                ApiContract(
                    kind="openapi",
                    path=str(openapi_path.relative_to(repo_path)),
                )
            )

        # gRPC proto contracts
        grpc_contracts = self._scan_grpc_services(repo_path)
        contracts.extend(grpc_contracts)

        return contracts

    # --- Private helpers ---

    def _detect_framework(self, dependencies: list[Dependency]) -> str | None:
        """Detect the primary HTTP framework from go.mod dependencies."""
        dep_names = {d.name for d in dependencies}

        for marker, framework in _FRAMEWORK_MARKERS.items():
            if any(marker in name for name in dep_names):
                return framework

        # Fallback: net/http is always available (standard library)
        return None

    def _scan_http_routes(self, repo_path: Path, framework: str | None) -> list[EndpointIndex]:
        """Scan .go files for HTTP handler registrations."""
        endpoints: list[EndpointIndex] = []
        seen: set[tuple[str, str]] = set()

        for go_file in repo_path.rglob("*.go"):
            if _should_skip_path(go_file):
                continue
            try:
                content = go_file.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue

            file_endpoints = self._extract_routes_from_content(content, framework)
            for ep in file_endpoints:
                key = (ep.method or "", ep.path or "")
                if key not in seen:
                    seen.add(key)
                    endpoints.append(ep)

        return endpoints

    def _extract_routes_from_content(
        self, content: str, framework: str | None
    ) -> list[EndpointIndex]:
        """Extract route registrations from a single file's content."""
        endpoints: list[EndpointIndex] = []

        if framework == "chi":
            endpoints.extend(self._extract_chi_routes(content))
        elif framework == "gin":
            endpoints.extend(self._extract_gin_routes(content))
        elif framework == "echo":
            endpoints.extend(self._extract_echo_routes(content))
        elif framework == "gorilla/mux":
            # gorilla/mux uses same pattern as chi: r.HandleFunc("/path", handler).Methods("GET")
            endpoints.extend(self._extract_gorilla_routes(content))
        else:
            # net/http fallback
            endpoints.extend(self._extract_net_http_routes(content))

        # Always try net/http patterns as a fallback for mixed codebases
        if framework and framework != "net/http":
            endpoints.extend(self._extract_net_http_routes(content))

        return endpoints

    def _extract_chi_routes(self, content: str) -> list[EndpointIndex]:
        """Extract chi-style routes: r.Get("/path", handler).

        Handles nested r.Route("/prefix", func(r chi.Router) { ... }) groups
        by tracking brace depth and prefix context.
        """
        endpoints: list[EndpointIndex] = []
        lines = content.splitlines()

        # Stack of (prefix, brace_depth_when_entered)
        prefix_stack: list[tuple[str, int]] = []
        brace_depth = 0

        for line in lines:
            stripped = line.strip()

            # Track brace depth
            brace_depth += stripped.count("{") - stripped.count("}")

            # Detect r.Route("/prefix", func...) — push prefix
            route_match = re.search(
                r"""\.\s*Route\s*\(\s*["'`]([^"'`]+)["'`]\s*,\s*func""", stripped
            )
            if route_match:
                prefix = route_match.group(1).rstrip("/")
                prefix_stack.append((prefix, brace_depth))
                continue

            # Pop prefix when we exit the brace scope
            while prefix_stack and brace_depth < prefix_stack[-1][1]:
                prefix_stack.pop()

            # Detect handler registrations
            handler_match = _CHI_ROUTE_RE.search(stripped)
            if handler_match:
                method = handler_match.group(1).upper()
                path = handler_match.group(2)

                # Build full path from prefix stack
                full_prefix = "".join(p for p, _ in prefix_stack)
                if path == "/":
                    full_path = full_prefix or "/"
                else:
                    full_path = full_prefix + path

                endpoints.append(EndpointIndex(method=method, path=full_path))

        return endpoints

    def _extract_gin_routes(self, content: str) -> list[EndpointIndex]:
        """Extract gin-style routes: r.GET("/path", handler)."""
        endpoints: list[EndpointIndex] = []
        for match in _GIN_ROUTE_RE.finditer(content):
            method = match.group(1).upper()
            path = match.group(2)
            endpoints.append(EndpointIndex(method=method, path=path))
        return endpoints

    def _extract_echo_routes(self, content: str) -> list[EndpointIndex]:
        """Extract echo-style routes: e.GET("/path", handler)."""
        endpoints: list[EndpointIndex] = []
        for match in _ECHO_ROUTE_RE.finditer(content):
            method = match.group(1).upper()
            path = match.group(2)
            endpoints.append(EndpointIndex(method=method, path=path))
        return endpoints

    def _extract_gorilla_routes(self, content: str) -> list[EndpointIndex]:
        """Extract gorilla/mux routes: r.HandleFunc("/path", handler).Methods("GET")."""
        endpoints: list[EndpointIndex] = []
        # Pattern: r.HandleFunc("/path", handler).Methods("GET", "POST")
        gorilla_re = re.compile(
            r"""HandleFunc\s*\(\s*["'`]([^"'`]+)["'`][^)]*\)\s*\.\s*Methods\s*\(\s*["'`](\w+)["'`]"""
        )
        for match in gorilla_re.finditer(content):
            path = match.group(1)
            method = match.group(2).upper()
            endpoints.append(EndpointIndex(method=method, path=path))
        return endpoints

    def _extract_net_http_routes(self, content: str) -> list[EndpointIndex]:
        """Extract net/http routes: http.HandleFunc("/path", handler)."""
        endpoints: list[EndpointIndex] = []
        for match in _NET_HTTP_ROUTE_RE.finditer(content):
            path = match.group(1)
            # net/http HandleFunc doesn't specify method; defaults to all
            endpoints.append(EndpointIndex(method=None, path=path))
        return endpoints

    def _scan_grpc_services(self, repo_path: Path) -> list[ApiContract]:
        """Scan .proto files for gRPC service definitions."""
        contracts: list[ApiContract] = []

        for proto_file in repo_path.rglob("*.proto"):
            if _should_skip_path(proto_file):
                continue
            try:
                content = proto_file.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue

            # Find service blocks
            for service_match in _GRPC_SERVICE_RE.finditer(content):
                service_name = service_match.group(1)
                # Find all RPCs within this service block
                service_start = service_match.start()
                # Find the closing brace for this service
                brace_depth = 0
                service_end = len(content)
                for i in range(service_match.end() - 1, len(content)):
                    if content[i] == "{":
                        brace_depth += 1
                    elif content[i] == "}":
                        brace_depth -= 1
                        if brace_depth == 0:
                            service_end = i
                            break

                service_block = content[service_start:service_end]
                endpoints: list[EndpointIndex] = []

                for rpc_match in _GRPC_RPC_RE.finditer(service_block):
                    rpc_name = rpc_match.group(1)
                    request_type = rpc_match.group(2)
                    response_type = rpc_match.group(3)
                    endpoints.append(
                        EndpointIndex(
                            method="gRPC",
                            path=f"/{service_name}/{rpc_name}",
                            summary=f"{request_type} -> {response_type}",
                        )
                    )

                if endpoints:
                    contracts.append(
                        ApiContract(
                            kind="grpc",
                            path=str(proto_file.relative_to(repo_path)),
                            endpoints=endpoints,
                        )
                    )

        return contracts


def _should_skip_path(path: Path) -> bool:
    """Return True if the path should be skipped during scanning."""
    parts = path.parts
    skip_dirs = {"vendor", "node_modules", ".git", "testdata", "third_party"}
    return bool(skip_dirs.intersection(parts))
