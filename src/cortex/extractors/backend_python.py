"""Backend Python extractor — parses Python backend repositories.

Extracts:
- Python version from pyproject.toml requires-python field
- Framework detection (FastAPI, Django, Flask, Starlette) from dependencies
- Dependencies from pyproject.toml or requirements.txt fallback
- FastAPI/Flask/Django endpoint extraction from decorator patterns
- Pydantic BaseModel classes as DTO schemas
- Database type from dependencies (SQLAlchemy, Django ORM, pymongo, etc.)
- Celery tasks as async entry points (@app.task, @shared_task)
- Kafka topics from source files
- Runtime info (Docker)
- Source repo git info
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

from cortex.extractors.base import Extractor
from cortex.extractors.utils import (
    detect_cache_type,
    detect_database_type,
    extract_python_dependencies,
    find_openapi_spec,
    parse_dockerfile,
    parse_pyproject_toml,
    parse_requirements_txt,
    scan_kafka_topics,
    scan_outbound_urls,
)
from cortex.schema import (
    ApiContract,
    DtoField,
    DtoSchema,
    EndpointIndex,
    EndpointRequestBody,
    EndpointResponse,
    EntryPoint,
    ServiceManifest,
    ServiceYaml,
)

logger = structlog.get_logger()

# Framework detection mapping: dependency name → framework label
_FRAMEWORK_DEPS: dict[str, str] = {
    "fastapi": "FastAPI",
    "django": "Django",
    "flask": "Flask",
    "starlette": "Starlette",
}

# FastAPI/Starlette route decorator pattern:
#   @app.get('/path') or @router.post('/path', response_model=Model)
_FASTAPI_ROUTE_RE = re.compile(
    r"@(?:\w+)\.(get|post|put|delete|patch)\(\s*['\"]([^'\"]+)['\"]"
    r"(?:.*?response_model\s*=\s*(\w+))?"
    r"[^)]*\)",
    re.DOTALL,
)

# Function definition following a route decorator
_ASYNC_DEF_RE = re.compile(
    r"(?:async\s+)?def\s+(\w+)\s*\(([^)]*)\)",
)

# Flask route decorator pattern:
#   @app.route('/path', methods=['GET', 'POST'])
_FLASK_ROUTE_RE = re.compile(
    r"@(?:\w+)\.route\(\s*['\"]([^'\"]+)['\"]"
    r"(?:.*?methods\s*=\s*\[([^\]]*)\])?"
    r"[^)]*\)",
    re.DOTALL,
)

# Django url path pattern:
#   path('api/v1/orders/', views.OrderListView.as_view(), name='order-list')
_DJANGO_PATH_RE = re.compile(
    r"path\(\s*['\"]([^'\"]+)['\"]",
)

# Pydantic BaseModel class definition
_PYDANTIC_CLASS_RE = re.compile(
    r"^class\s+(\w+)\s*\(\s*(?:BaseModel|BaseSchema)\s*\)\s*:",
    re.MULTILINE,
)

# Pydantic field definition within a class body
_PYDANTIC_FIELD_RE = re.compile(
    r"^\s{4}(\w+)\s*:\s*(.+?)(?:\s*=\s*(.+))?$",
)

# Celery task decorators
_CELERY_TASK_RE = re.compile(
    r"@(?:app\.task|shared_task|celery\.task)\s*(?:\([^)]*\))?\s*\n"
    r"(?:async\s+)?def\s+(\w+)",
    re.MULTILINE,
)

# Directories/paths to skip during file scanning
_SKIP_DIRS = {"__pycache__", ".git", "node_modules", ".venv", "venv", ".tox", ".mypy_cache"}


class BackendPythonExtractor(Extractor):
    """Extractor for Python backend services (FastAPI, Django, Flask, Starlette)."""

    type = "backend-python"

    def extract(self, repo_path: Path, service_yaml: ServiceYaml) -> ServiceManifest:
        """Parse a Python backend repo and return a structured manifest."""
        # 1. Parse pyproject.toml, fall back to requirements.txt
        pyproject = parse_pyproject_toml(repo_path)

        # 2. Extract dependencies
        if pyproject:
            dependencies = extract_python_dependencies(pyproject)
        else:
            dependencies = parse_requirements_txt(repo_path)

        # 3. Detect framework from dependencies
        framework = self._detect_framework(dependencies)

        # 4. Get Python version from pyproject.toml requires-python
        python_version = self._get_python_version(pyproject)

        # 5. Scan for endpoint definitions
        endpoints = self._scan_endpoints(repo_path, framework)

        # 6. Extract Pydantic models as DTO schemas
        dto_schemas = self._extract_dto_schemas(repo_path)

        # 7. Detect database type
        database_type = detect_database_type(dependencies)
        cache_type = detect_cache_type(dependencies)

        # 8. Detect Celery tasks as async entry points
        entry_points = self._scan_entry_points(repo_path)

        # 9. Scan Kafka topics
        kafka_produces, kafka_consumes = scan_kafka_topics(repo_path, extensions=(".py",))

        # 10. Parse Dockerfile if present
        runtime = parse_dockerfile(repo_path)

        # Scan outbound HTTP calls
        outbound_urls = scan_outbound_urls(repo_path, extensions=(".py",))

        # Build API contracts: combine OpenAPI specs + code-scanned endpoints
        api_contracts = self.find_api_contracts(repo_path)
        if endpoints:
            kind = f"{framework.lower()}-decorators" if framework else "python-decorators"
            api_contracts.append(ApiContract(kind=kind, endpoints=endpoints))

        manifest = ServiceManifest(
            name=service_yaml.name,
            type=service_yaml.type,
            owner=service_yaml.owner,
            domain=service_yaml.domain,
            tier=service_yaml.tier,
            status=service_yaml.status,
            purpose=service_yaml.purpose,
            keywords=service_yaml.keywords,
            slack=service_yaml.slack,
            runbook=service_yaml.runbook,
            jira_component=service_yaml.jira_component,
            language="Python",
            language_version=python_version,
            framework=framework,
            dependencies=dependencies,
            entry_points=entry_points,
            api_contracts=api_contracts,
            dto_schemas=dto_schemas,
            database_type=database_type,
            cache_type=cache_type,
            kafka_produces=kafka_produces,
            kafka_consumes=kafka_consumes,
            runtime=runtime,
            integration_notes=service_yaml.integration_notes,
            swagger_url=service_yaml.swagger_url,
            extracted_at=datetime.now(UTC),
            extractor_version="1.0.0",
        )

        # 11. Enrich with AI context
        self._enrich_with_context(manifest, repo_path)

        # 12. Set source repo
        manifest.source_repo = self._get_source_repo(repo_path)

        return manifest

    def find_api_contracts(self, repo_path: Path) -> list[ApiContract]:
        """Find API contract files (OpenAPI specs) in the repo."""
        contracts: list[ApiContract] = []
        spec_path = find_openapi_spec(repo_path)
        if spec_path:
            contracts.append(
                ApiContract(
                    kind="openapi",
                    path=str(spec_path.relative_to(repo_path)),
                )
            )
        return contracts

    # --- Private helpers ---

    def _detect_framework(self, dependencies: list[Any]) -> str | None:
        """Detect the primary Python web framework from dependencies."""
        dep_names = {d.name.lower() for d in dependencies}
        for dep_name, framework_label in _FRAMEWORK_DEPS.items():
            if dep_name in dep_names:
                return framework_label
        return None

    def _get_python_version(self, pyproject: dict[str, Any] | None) -> str | None:
        """Extract Python version from pyproject.toml requires-python field."""
        if pyproject is None:
            return None
        project = pyproject.get("project", {})
        requires_python = project.get("requires-python")
        if requires_python:
            # Extract version number from specifier like ">=3.11" or ">=3.12,<4"
            match = re.search(r"(\d+\.\d+)", requires_python)
            if match:
                return match.group(1)
        return None

    def _scan_endpoints(self, repo_path: Path, framework: str | None) -> list[EndpointIndex]:
        """Scan .py files for endpoint definitions based on the detected framework."""
        if framework is None:
            return []

        endpoints: list[EndpointIndex] = []

        for py_file in self._iter_python_files(repo_path):
            try:
                content = py_file.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue

            if framework in ("FastAPI", "Starlette"):
                endpoints.extend(self._parse_fastapi_endpoints(content))
            elif framework == "Flask":
                endpoints.extend(self._parse_flask_endpoints(content))
            elif framework == "Django":
                endpoints.extend(self._parse_django_endpoints(content))

        return endpoints

    def _parse_fastapi_endpoints(self, content: str) -> list[EndpointIndex]:
        """Parse FastAPI/Starlette route decorators."""
        endpoints: list[EndpointIndex] = []
        lines = content.splitlines()

        for i, line in enumerate(lines):
            match = _FASTAPI_ROUTE_RE.search(line)
            if not match:
                # Check if decorator spans multiple lines — try joining with next line
                if i + 1 < len(lines):
                    two_lines = line + " " + lines[i + 1]
                    match = _FASTAPI_ROUTE_RE.search(two_lines)
            if not match:
                continue

            method = match.group(1).upper()
            path = match.group(2)
            response_model = match.group(3)

            # Look for the function definition after the decorator
            func_name = None
            request_body = None
            for j in range(i + 1, min(i + 5, len(lines))):
                func_match = _ASYNC_DEF_RE.search(lines[j])
                if func_match:
                    func_name = func_match.group(1)
                    params = func_match.group(2)
                    # Detect request body parameter (typed parameter that isn't a primitive)
                    request_body = self._detect_request_body(params, method)
                    break

            response = None
            if response_model:
                response = EndpointResponse(type=response_model)

            endpoints.append(
                EndpointIndex(
                    method=method,
                    path=path,
                    operation_id=func_name,
                    request_body=request_body,
                    response=response,
                )
            )

        return endpoints

    def _parse_flask_endpoints(self, content: str) -> list[EndpointIndex]:
        """Parse Flask route decorators."""
        endpoints: list[EndpointIndex] = []

        for match in _FLASK_ROUTE_RE.finditer(content):
            path = match.group(1)
            methods_str = match.group(2)

            if methods_str:
                # Parse methods list: ['GET', 'POST']
                methods = re.findall(r"['\"](\w+)['\"]", methods_str)
            else:
                methods = ["GET"]

            for method in methods:
                endpoints.append(
                    EndpointIndex(
                        method=method.upper(),
                        path=path,
                    )
                )

        return endpoints

    def _parse_django_endpoints(self, content: str) -> list[EndpointIndex]:
        """Parse Django url path() patterns."""
        endpoints: list[EndpointIndex] = []

        for match in _DJANGO_PATH_RE.finditer(content):
            path = match.group(1)
            # Django doesn't embed HTTP method in URL conf, default to None
            endpoints.append(
                EndpointIndex(
                    path=f"/{path}" if not path.startswith("/") else path,
                )
            )

        return endpoints

    def _detect_request_body(self, params: str, method: str) -> EndpointRequestBody | None:
        """Detect request body type from function parameters."""
        if method in ("GET", "DELETE"):
            return None

        # Look for typed parameters like "order: CreateOrderRequest"
        # Skip common non-body params: self, request, response, db, session, etc.
        skip_params = {"self", "request", "response", "db", "session", "background_tasks"}
        skip_types = {
            "str",
            "int",
            "float",
            "bool",
            "Request",
            "Response",
            "Session",
            "BackgroundTasks",
        }

        for param in params.split(","):
            param = param.strip()
            if ":" not in param:
                continue
            parts = param.split(":", 1)
            param_name = parts[0].strip()
            param_type = parts[1].strip().split("=")[0].strip()

            if param_name in skip_params:
                continue
            if param_type in skip_types:
                continue
            # Likely a Pydantic model parameter
            if param_type and param_type[0].isupper() and param_type.isidentifier():
                return EndpointRequestBody(type=param_type)

        return None

    def _extract_dto_schemas(self, repo_path: Path) -> dict[str, DtoSchema]:
        """Extract Pydantic BaseModel classes as DTO schemas."""
        schemas: dict[str, DtoSchema] = {}

        for py_file in self._iter_python_files(repo_path):
            try:
                content = py_file.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue

            rel_path = str(py_file.relative_to(repo_path))

            for class_match in _PYDANTIC_CLASS_RE.finditer(content):
                class_name = class_match.group(1)
                class_start = class_match.end()

                # Extract fields from the class body
                fields = self._extract_pydantic_fields(content, class_start)

                if fields:
                    schemas[class_name] = DtoSchema(
                        name=class_name,
                        kind="class",
                        fields=fields,
                        source_file=rel_path,
                    )

        return schemas

    def _extract_pydantic_fields(self, content: str, class_start: int) -> list[DtoField]:
        """Extract field definitions from a Pydantic model class body."""
        fields: list[DtoField] = []
        lines = content[class_start:].splitlines()

        for line in lines:
            # Stop at next class/function definition or unindented line
            stripped = line.strip()
            if not line.startswith(" ") and not line.startswith("\t") and stripped:
                break
            if stripped.startswith("class ") or stripped.startswith("def "):
                break

            field_match = _PYDANTIC_FIELD_RE.match(line)
            if not field_match:
                continue

            field_name = field_match.group(1)
            field_type = field_match.group(2).strip()
            field_default = field_match.group(3)

            # Skip private/dunder attributes and methods
            if field_name.startswith("_"):
                continue
            # Skip class Config or model_config
            if field_name in ("Config", "model_config"):
                continue

            # Determine if required (no default value and not Optional)
            required = (
                field_default is None and "Optional" not in field_type and "None" not in field_type
            )

            # Clean up type annotation (remove trailing comments)
            if "#" in field_type:
                field_type = field_type.split("#")[0].strip()

            fields.append(
                DtoField(
                    name=field_name,
                    type=field_type,
                    required=required,
                )
            )

        return fields

    def _scan_entry_points(self, repo_path: Path) -> list[EntryPoint]:
        """Scan for Celery task decorators and other entry points."""
        entry_points: list[EntryPoint] = []

        for py_file in self._iter_python_files(repo_path):
            try:
                content = py_file.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue

            # Detect Celery tasks
            for match in _CELERY_TASK_RE.finditer(content):
                task_name = match.group(1)
                entry_points.append(EntryPoint(kind="celery-task", ref=task_name))

        return entry_points

    def _iter_python_files(self, repo_path: Path) -> list[Path]:
        """Iterate over .py files in the repo, skipping excluded directories."""
        py_files: list[Path] = []
        for py_file in repo_path.rglob("*.py"):
            # Skip excluded directories
            if any(part in _SKIP_DIRS for part in py_file.parts):
                continue
            py_files.append(py_file)
        return py_files
