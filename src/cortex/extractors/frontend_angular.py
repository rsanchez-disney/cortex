"""Frontend Angular extractor — parses Angular project files to produce a ServiceManifest.

Extracts:
- Angular version from package.json (@angular/core)
- Project structure from angular.json (projects, defaultProject)
- Route definitions from *-routing.module.ts or app.routes.ts (paths, lazy modules, components)
- HttpClient calls from *.service.ts files (method + URL path → outbound_calls)
- Modules from route structure → ModuleInfo entries
- npm dependencies from package.json
- Dockerfile runtime info
- CI system detection
- Source repo git info (remote URL + HEAD commit)
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

import structlog

from cortex.extractors.base import Extractor
from cortex.extractors.utils import (
    detect_angular_version,
    extract_npm_dependencies,
    find_openapi_spec,
    parse_dockerfile,
    parse_package_json,
)
from cortex.schema import (
    ApiContract,
    EndpointIndex,
    ModuleInfo,
    OutboundCall,
    ServiceManifest,
    ServiceYaml,
)

logger = structlog.get_logger()

# Regex to match route definitions: { path: '...', loadChildren/component: ... }
_ROUTE_PATH_RE = re.compile(
    r"""\{\s*path:\s*['"]([^'"]*)['"]\s*,\s*"""
    r"""(?:loadChildren:\s*\(\)\s*=>\s*import\(\s*['"]([^'"]+)['"]\s*\)"""
    r"""|component:\s*(\w+))""",
    re.MULTILINE,
)

# Regex to match HttpClient method calls:
#   this.http.get<Type>('url') or this.http.get<Type>(`url`)
# Uses a non-greedy match for the generic type to handle nested generics
# like Record<string, boolean>
_HTTP_CALL_RE = re.compile(
    r"""this\.http\.(get|post|put|delete|patch)(?:<.+?>)?\s*\(\s*"""
    r"""(?:[`'"]([^`'"]*)[`'"]|`\$\{[^}]*\}([^`]*)`)\s*""",
    re.IGNORECASE | re.DOTALL,
)

# Regex to extract the path portion from template literals like `${environment.apiUrl}/api/v1/foo`
_TEMPLATE_URL_RE = re.compile(
    r"""\$\{[^}]+\}(/[^`'"]*)""",
)


class FrontendAngularExtractor(Extractor):
    """Extractor for Angular frontend repositories."""

    type = "frontend-angular"

    def extract(self, repo_path: Path, service_yaml: ServiceYaml) -> ServiceManifest:
        """Extract metadata from an Angular frontend repo."""
        # Respect extractor_hints.project_root
        effective_root = repo_path
        if service_yaml.extractor_hints and service_yaml.extractor_hints.project_root:
            effective_root = repo_path / service_yaml.extractor_hints.project_root

        # 1. Parse package.json
        pkg = parse_package_json(effective_root)
        dependencies = extract_npm_dependencies(pkg) if pkg else []

        # 2. Detect Angular version
        angular_version = detect_angular_version(pkg) if pkg else None

        # 3. Parse angular.json for project structure
        angular_config = self._parse_angular_json(effective_root)
        _default_project = angular_config.get("defaultProject") if angular_config else None

        # 4. Scan for route definitions
        routes = self._scan_routes(effective_root)

        # 5. Scan for HttpClient calls → outbound_calls
        outbound_calls = self._scan_http_calls(effective_root)

        # 6. Detect modules from route structure
        modules = self._build_modules_from_routes(routes)

        # 7. Parse Dockerfile
        runtime = parse_dockerfile(effective_root)

        # 8. Detect CI
        ci = self._detect_ci(repo_path)

        # 9. Find API contracts
        api_contracts = self.find_api_contracts(effective_root)

        # Build manifest
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
            language_version=angular_version,
            slack=service_yaml.slack,
            runbook=service_yaml.runbook,
            jira_component=service_yaml.jira_component,
            framework=f"Angular ({angular_version})" if angular_version else "Angular",
            modules=modules,
            outbound_calls=outbound_calls,
            dependencies=dependencies,
            api_contracts=api_contracts,
            runtime=runtime,
            ci=ci,
            integration_notes=service_yaml.integration_notes,
            swagger_url=service_yaml.swagger_url,
            extracted_at=datetime.now(UTC),
            extractor_version="1.0.0",
        )

        # 8. Enrich with AI context
        self._enrich_with_context(manifest, repo_path)

        # 9. Set source_repo
        manifest.source_repo = self._get_source_repo(repo_path)

        return manifest

    def find_api_contracts(self, repo_path: Path) -> list[ApiContract]:
        """Find OpenAPI/Swagger spec files in the repo."""
        spec_path = find_openapi_spec(repo_path)
        if spec_path is None:
            return []

        return [
            ApiContract(
                kind="openapi",
                path=str(spec_path.relative_to(repo_path)),
            )
        ]

    # --- Private helpers ---

    def _parse_angular_json(self, repo_path: Path) -> dict | None:
        """Parse angular.json and return its content."""
        angular_json_path = repo_path / "angular.json"
        if not angular_json_path.is_file():
            return None
        try:
            content = json.loads(angular_json_path.read_text(encoding="utf-8"))
            return content
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(
                "failed_to_parse_angular_json", path=str(angular_json_path), error=str(e)
            )
            return None

    def _scan_routes(self, repo_path: Path) -> list[dict]:
        """Scan for route definitions in routing modules and standalone route files.

        Returns a list of dicts with keys: path, load_children, component.
        """
        routes: list[dict] = []
        route_files = list(repo_path.rglob("*-routing.module.ts"))
        route_files.extend(repo_path.rglob("app.routes.ts"))
        route_files.extend(repo_path.rglob("*.routes.ts"))

        for route_file in route_files:
            if "node_modules" in str(route_file):
                continue
            try:
                content = route_file.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue

            for match in _ROUTE_PATH_RE.finditer(content):
                route_path = match.group(1)
                load_children = match.group(2)
                component = match.group(3)
                routes.append(
                    {
                        "path": route_path,
                        "load_children": load_children,
                        "component": component,
                        "source_file": str(route_file.relative_to(repo_path)),
                    }
                )

        return routes

    def _scan_http_calls(self, repo_path: Path) -> list[OutboundCall]:
        """Scan *.service.ts files for HttpClient calls.

        Returns a list of OutboundCall with method and URL extracted.
        """
        outbound_calls: list[OutboundCall] = []
        seen: set[tuple[str, str]] = set()

        for service_file in repo_path.rglob("*.service.ts"):
            if "node_modules" in str(service_file):
                continue
            try:
                content = service_file.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue

            for match in _HTTP_CALL_RE.finditer(content):
                method = match.group(1).upper()
                # Direct URL string or template literal path
                url = match.group(2) or match.group(3) or ""

                # If url is empty, try to extract from full match context using template pattern
                if not url:
                    # Try to find template literal pattern in surrounding context
                    continue

                # Extract path from template literals like ${environment.apiUrl}/api/v1/foo
                if "${" in url:
                    template_match = _TEMPLATE_URL_RE.search(url)
                    if template_match:
                        url = template_match.group(1)
                    else:
                        continue

                # Clean up URL — keep only the path portion
                path = url.strip()
                if not path:
                    continue

                key = (method, path)
                if key in seen:
                    continue
                seen.add(key)

                outbound_calls.append(
                    OutboundCall(
                        target_url=path,
                        protocol="http",
                        endpoints=[
                            EndpointIndex(method=method, path=path),
                        ],
                    )
                )

            # Also handle full template literal patterns:
            # this.http.get<Type>(`${environment.apiUrl}/api/v1/endpoint`)
            for match in re.finditer(
                r"""this\.http\.(get|post|put|delete|patch)(?:<.+?>)?\s*\(\s*`\$\{[^}]+\}(/[^`]+)`""",
                content,
                re.IGNORECASE | re.DOTALL,
            ):
                method = match.group(1).upper()
                path = match.group(2).strip()
                if not path:
                    continue

                key = (method, path)
                if key in seen:
                    continue
                seen.add(key)

                outbound_calls.append(
                    OutboundCall(
                        target_url=path,
                        protocol="http",
                        endpoints=[
                            EndpointIndex(method=method, path=path),
                        ],
                    )
                )

        return outbound_calls

    def _build_modules_from_routes(self, routes: list[dict]) -> list[ModuleInfo]:
        """Build ModuleInfo entries from extracted routes.

        Each lazy-loaded module (loadChildren) becomes a module entry.
        Eagerly loaded components are grouped under an 'app' module.
        """
        modules: list[ModuleInfo] = []
        seen_modules: set[str] = set()

        for route in routes:
            if route.get("load_children"):
                # Extract module name from import path
                # e.g., './payments/payments.module' → 'payments'
                import_path = route["load_children"]
                module_name = self._extract_module_name(import_path)
                if module_name and module_name not in seen_modules:
                    seen_modules.add(module_name)
                    modules.append(
                        ModuleInfo(
                            name=module_name,
                            type="lazy-module",
                            dependencies=[],
                        )
                    )
            elif route.get("component"):
                # Eagerly loaded components belong to the root/app module
                if "app" not in seen_modules:
                    seen_modules.add("app")
                    modules.append(
                        ModuleInfo(
                            name="app",
                            type="application",
                            dependencies=[],
                        )
                    )

        return modules

    def _extract_module_name(self, import_path: str) -> str | None:
        """Extract a clean module name from a loadChildren import path.

        Examples:
            './payments/payments.module' → 'payments'
            './payments/payments.routes' → 'payments'
            '../features/config/config.module' → 'config'
        """
        # Remove leading './' or '../' segments
        clean = import_path.split("/")
        if not clean:
            return None

        # Get the second-to-last segment (directory name) or last segment
        # For './payments/payments.module' → segments are ['.', 'payments', 'payments.module']
        # We want 'payments' (the directory)
        non_dot_segments = [s for s in clean if s and not s.startswith(".")]
        if len(non_dot_segments) >= 2:
            return non_dot_segments[-2]
        if non_dot_segments:
            # Single segment: extract name before .module/.routes
            name = non_dot_segments[0]
            name = re.sub(r"\.(module|routes)$", "", name)
            return name
        return None

    def _detect_ci(self, repo_path: Path) -> str | None:
        """Detect CI system from config files at repo root."""
        if (repo_path / ".github" / "workflows").is_dir():
            return "github-actions"
        if (repo_path / "azure-pipelines.yml").is_file():
            return "azure-devops"
        if (repo_path / ".gitlab-ci.yml").is_file():
            return "gitlab-ci"
        if (repo_path / "Jenkinsfile").is_file():
            return "jenkins"
        if (repo_path / "bitbucket-pipelines.yml").is_file():
            return "bitbucket"
        return None
