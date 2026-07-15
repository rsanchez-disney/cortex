"""Backend TypeScript extractor — parses TypeScript backend repositories.

Extracts:
- Framework detection (NestJS, Express, Fastify) from package.json
- TypeScript version from devDependencies
- Dependencies from package.json (runtime + dev)
- API endpoints via decorator parsing (NestJS) or router pattern matching (Express)
- Database type from dependency analysis
- Cache type from dependency analysis
- Kafka topics from source file scanning
- Outbound HTTP calls
- Runtime info (Docker)
- Source repo git info
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
    detect_node_framework,
    extract_npm_dependencies,
    find_openapi_spec,
    parse_dockerfile,
    parse_package_json,
    scan_kafka_topics,
    scan_outbound_urls,
)
from cortex.schema import (
    ApiContract,
    EndpointIndex,
    EndpointRequestBody,
    EndpointResponse,
    ServiceManifest,
    ServiceYaml,
)

logger = structlog.get_logger()

# NestJS decorator patterns
_CONTROLLER_RE = re.compile(r"@Controller\(\s*['\"]([^'\"]*?)['\"]\s*\)", re.MULTILINE)
_NEST_METHOD_RE = re.compile(
    r"@(Get|Post|Put|Delete|Patch)\(\s*(?:['\"]([^'\"]*?)['\"])?\s*\)",
    re.MULTILINE,
)
_NEST_BODY_RE = re.compile(r"@Body\(\)\s+\w+\s*:\s*(\w+)", re.MULTILINE)
_NEST_RETURN_TYPE_RE = re.compile(r"\)\s*:\s*(?:Promise<)?(\w+)>?\s*\{", re.MULTILINE)

# Express/Fastify router patterns
_EXPRESS_ROUTE_RE = re.compile(
    r"(?:router|app)\.(get|post|put|delete|patch)\(\s*['\"]([^'\"]+)['\"]",
    re.IGNORECASE,
)


class BackendTypeScriptExtractor(Extractor):
    """Extractor for backend TypeScript services (NestJS, Express, Fastify)."""

    type = "backend-typescript"

    def extract(self, repo_path: Path, service_yaml: ServiceYaml) -> ServiceManifest:
        """Parse a TypeScript backend repo and return a structured manifest."""
        # 1. Parse package.json
        pkg = parse_package_json(repo_path)
        if pkg is None:
            logger.warning("no_package_json", repo=str(repo_path))
            pkg = {}

        dependencies = extract_npm_dependencies(pkg)

        # 2. Detect framework
        framework = detect_node_framework(pkg)

        # 3. Get TypeScript version from devDependencies
        dev_deps = pkg.get("devDependencies") or {}
        typescript_version = dev_deps.get("typescript")

        # 4. Scan for endpoints
        endpoints = self._scan_endpoints(repo_path, framework)

        # 5. Detect database type
        database_type = detect_database_type(dependencies)

        # 6. Detect cache type
        cache_type = detect_cache_type(dependencies)

        # 7. Scan Kafka topics
        kafka_produces, kafka_consumes = scan_kafka_topics(repo_path, extensions=(".ts", ".js"))

        # 8. Scan outbound URLs
        outbound_urls = scan_outbound_urls(repo_path, extensions=(".ts", ".js"))

        # 9. Parse Dockerfile
        runtime = parse_dockerfile(repo_path)

        # 10. Build API contracts
        api_contracts = self.find_api_contracts(repo_path)

        # 11. Source repo
        source_repo = self._get_source_repo(repo_path)

        manifest = ServiceManifest(
            name=service_yaml.name,
            type=service_yaml.type,
            owner=service_yaml.owner,
            domain=service_yaml.domain,
            tier=service_yaml.tier,
            status=service_yaml.status,
            purpose=service_yaml.purpose,
            keywords=service_yaml.keywords,
            language="TypeScript",
            language_version=typescript_version,
            slack=service_yaml.slack,
            runbook=service_yaml.runbook,
            jira_component=service_yaml.jira_component,
            framework=framework,
            dependencies=dependencies,
            api_contracts=api_contracts,
            database_type=database_type,
            cache_type=cache_type,
            kafka_produces=kafka_produces,
            kafka_consumes=kafka_consumes,
            runtime=runtime,
            integration_notes=service_yaml.integration_notes,
            swagger_url=service_yaml.swagger_url,
            extracted_at=datetime.now(UTC),
            extractor_version="1.0.0",
            source_repo=source_repo,
        )

        # 12. Enrich with AI context
        self._enrich_with_context(manifest, repo_path)

        return manifest

    def find_api_contracts(self, repo_path: Path) -> list[ApiContract]:
        """Find API contract files (OpenAPI specs) in the repository."""
        contracts: list[ApiContract] = []

        openapi_path = find_openapi_spec(repo_path)
        if openapi_path is not None:
            contracts.append(
                ApiContract(
                    kind="openapi",
                    path=str(openapi_path.relative_to(repo_path)),
                )
            )

        return contracts

    def _scan_endpoints(self, repo_path: Path, framework: str | None) -> list[EndpointIndex]:
        """Scan TypeScript source files for endpoint declarations."""
        if framework == "NestJS":
            return self._scan_nestjs_endpoints(repo_path)
        if framework in ("Express", "Fastify"):
            return self._scan_express_endpoints(repo_path)
        # Try both patterns as fallback
        endpoints = self._scan_nestjs_endpoints(repo_path)
        if not endpoints:
            endpoints = self._scan_express_endpoints(repo_path)
        return endpoints

    def _scan_nestjs_endpoints(self, repo_path: Path) -> list[EndpointIndex]:
        """Parse NestJS controller decorators to extract endpoints."""
        endpoints: list[EndpointIndex] = []

        for ts_file in repo_path.rglob("*.ts"):
            if "node_modules" in str(ts_file) or "dist" in str(ts_file):
                continue
            if ".spec." in ts_file.name or ".test." in ts_file.name:
                continue

            try:
                content = ts_file.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue

            # Find @Controller('base_path')
            controller_match = _CONTROLLER_RE.search(content)
            if controller_match is None:
                continue

            base_path = controller_match.group(1).strip("/")

            # Find all HTTP method decorators
            for method_match in _NEST_METHOD_RE.finditer(content):
                http_method = method_match.group(1).upper()
                sub_path = method_match.group(2) or ""
                sub_path = sub_path.strip("/")

                # Build full path
                if base_path and sub_path:
                    full_path = f"/{base_path}/{sub_path}"
                elif base_path:
                    full_path = f"/{base_path}"
                elif sub_path:
                    full_path = f"/{sub_path}"
                else:
                    full_path = "/"

                # Try to find @Body() parameter near this decorator
                request_body = None
                method_start = method_match.end()
                # Look in the next 500 chars for a @Body() annotation
                chunk = content[method_start : method_start + 500]
                body_match = _NEST_BODY_RE.search(chunk)
                if body_match:
                    request_body = EndpointRequestBody(type=body_match.group(1), required=True)

                # Try to find return type
                response = None
                return_match = _NEST_RETURN_TYPE_RE.search(chunk)
                if return_match:
                    return_type = return_match.group(1)
                    if return_type not in ("void", "any", "Promise"):
                        response = EndpointResponse(type=return_type)

                endpoints.append(
                    EndpointIndex(
                        method=http_method,
                        path=full_path,
                        request_body=request_body,
                        response=response,
                    )
                )

        return endpoints

    def _scan_express_endpoints(self, repo_path: Path) -> list[EndpointIndex]:
        """Parse Express/Fastify router patterns to extract endpoints."""
        endpoints: list[EndpointIndex] = []

        for ts_file in repo_path.rglob("*.ts"):
            if "node_modules" in str(ts_file) or "dist" in str(ts_file):
                continue
            if ".spec." in ts_file.name or ".test." in ts_file.name:
                continue

            try:
                content = ts_file.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue

            for route_match in _EXPRESS_ROUTE_RE.finditer(content):
                http_method = route_match.group(1).upper()
                path = route_match.group(2)

                endpoints.append(
                    EndpointIndex(
                        method=http_method,
                        path=path,
                    )
                )

        return endpoints
