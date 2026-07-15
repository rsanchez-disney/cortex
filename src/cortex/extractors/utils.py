"""Shared utilities for Cortex extractors.

Reusable parsing logic that applies across multiple ecosystems:
- Package file parsing (package.json, go.mod, pyproject.toml)
- Database type detection from dependency lists
- Kafka topic detection
- Dockerfile parsing
- OpenAPI spec discovery
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import structlog

from cortex.schema import Dependency, EndpointIndex, RuntimeInfo, SourceRepo

log = structlog.get_logger()


# --- package.json parsing ---


def parse_package_json(repo_path: Path) -> dict[str, Any] | None:
    """Parse package.json and return the raw dict, or None if not found."""
    pkg_path = repo_path / "package.json"
    if not pkg_path.is_file():
        return None
    try:
        return json.loads(pkg_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        log.warning("failed_to_parse_package_json", path=str(pkg_path), error=str(e))
        return None


def extract_npm_dependencies(pkg: dict[str, Any]) -> list[Dependency]:
    """Extract dependencies from a parsed package.json."""
    deps: list[Dependency] = []

    for name, version in (pkg.get("dependencies") or {}).items():
        deps.append(Dependency(name=name, version=version, source="npm", category="runtime"))

    for name, version in (pkg.get("devDependencies") or {}).items():
        deps.append(Dependency(name=name, version=version, source="npm", category="dev", direct=True))

    return deps


def detect_node_framework(pkg: dict[str, Any]) -> str | None:
    """Detect the primary framework from package.json dependencies."""
    all_deps = set((pkg.get("dependencies") or {}).keys())

    if "@nestjs/core" in all_deps:
        return "NestJS"
    if "express" in all_deps:
        return "Express"
    if "fastify" in all_deps:
        return "Fastify"
    if "@hapi/hapi" in all_deps:
        return "Hapi"
    if "koa" in all_deps:
        return "Koa"
    return None


def detect_angular_version(pkg: dict[str, Any]) -> str | None:
    """Detect Angular version from package.json."""
    deps = pkg.get("dependencies") or {}
    angular_core = deps.get("@angular/core")
    if angular_core:
        # Extract major version from "^17.0.0" or "~17.2.0"
        match = re.search(r"(\d+)", angular_core)
        if match:
            return f"Angular {match.group(1)}"
    return None


# --- go.mod parsing ---


def parse_go_mod(repo_path: Path) -> dict[str, Any] | None:
    """Parse go.mod and return module name, go version, and dependencies."""
    go_mod_path = repo_path / "go.mod"
    if not go_mod_path.is_file():
        return None

    try:
        content = go_mod_path.read_text(encoding="utf-8")
    except OSError:
        return None

    result: dict[str, Any] = {"module": None, "go_version": None, "dependencies": []}

    # Module name
    module_match = re.search(r"^module\s+(.+)$", content, re.MULTILINE)
    if module_match:
        result["module"] = module_match.group(1).strip()

    # Go version
    go_match = re.search(r"^go\s+(\d+\.\d+)", content, re.MULTILINE)
    if go_match:
        result["go_version"] = go_match.group(1)

    # Dependencies (require block)
    in_require = False
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("require ("):
            in_require = True
            continue
        if in_require and stripped == ")":
            in_require = False
            continue
        if in_require and stripped and not stripped.startswith("//"):
            parts = stripped.split()
            if len(parts) >= 2:
                result["dependencies"].append(
                    Dependency(name=parts[0], version=parts[1], source="go.mod", category="runtime")
                )

    # Single-line require statements
    for match in re.finditer(r"^require\s+(\S+)\s+(\S+)", content, re.MULTILINE):
        result["dependencies"].append(
            Dependency(name=match.group(1), version=match.group(2), source="go.mod", category="runtime")
        )

    return result


# --- pyproject.toml / requirements.txt parsing ---


def parse_pyproject_toml(repo_path: Path) -> dict[str, Any] | None:
    """Parse pyproject.toml and return project metadata."""
    pyproject_path = repo_path / "pyproject.toml"
    if not pyproject_path.is_file():
        return None

    try:
        import tomli
        content = pyproject_path.read_bytes()
        return tomli.loads(content.decode("utf-8"))
    except (ImportError, OSError, Exception) as e:
        log.warning("failed_to_parse_pyproject", error=str(e))
        return None


def extract_python_dependencies(pyproject: dict[str, Any]) -> list[Dependency]:
    """Extract dependencies from pyproject.toml."""
    deps: list[Dependency] = []
    project = pyproject.get("project", {})

    for dep_str in project.get("dependencies", []):
        name, version = _parse_pep508(dep_str)
        deps.append(Dependency(name=name, version=version, source="pyproject.toml", category="runtime"))

    # Dev/test dependencies from optional-dependencies
    for group_name, group_deps in project.get("optional-dependencies", {}).items():
        category = "dev" if group_name in ("dev", "test", "testing") else "optional"
        for dep_str in group_deps:
            name, version = _parse_pep508(dep_str)
            deps.append(Dependency(name=name, version=version, source="pyproject.toml", category=category))

    return deps


def parse_requirements_txt(repo_path: Path) -> list[Dependency]:
    """Parse requirements.txt as fallback."""
    req_path = repo_path / "requirements.txt"
    if not req_path.is_file():
        return []

    deps: list[Dependency] = []
    try:
        for line in req_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("-"):
                continue
            name, version = _parse_pep508(line)
            deps.append(Dependency(name=name, version=version, source="requirements.txt", category="runtime"))
    except OSError:
        pass
    return deps


def _parse_pep508(dep_str: str) -> tuple[str, str | None]:
    """Parse a PEP 508 dependency string into (name, version_spec)."""
    # Remove extras: "package[extra1,extra2]>=1.0" → "package>=1.0"
    dep_str = re.sub(r"\[.*?\]", "", dep_str).strip()
    # Split on version specifiers
    match = re.match(r"^([a-zA-Z0-9._-]+)\s*(.*)", dep_str)
    if match:
        name = match.group(1).strip()
        version = match.group(2).strip() or None
        return name, version
    return dep_str, None


# --- Database type detection ---

_DB_PATTERNS: dict[str, list[str]] = {
    "postgresql": [
        "pg", "postgres", "postgresql", "pgx", "psycopg", "asyncpg",
        "typeorm", "prisma",  # often default to postgres
        "org.postgresql", "spring-boot-starter-data-jpa",
    ],
    "mysql": ["mysql", "mysql2", "mariadb", "pymysql"],
    "mongodb": ["mongoose", "mongodb", "mongo-driver", "pymongo", "motor"],
    "redis": ["redis", "ioredis", "aioredis", "go-redis"],
    "cosmosdb": ["cosmos", "azure-cosmos"],
    "dynamodb": ["dynamodb", "aws-sdk", "@aws-sdk/client-dynamodb"],
    "sqlite": ["sqlite", "sqlite3", "better-sqlite3"],
    "elasticsearch": ["elasticsearch", "@elastic/elasticsearch", "olivere/elastic"],
}


def detect_database_type(dependencies: list[Dependency]) -> str | None:
    """Infer the primary database type from dependency names."""
    dep_names = {d.name.lower() for d in dependencies}

    for db_type, patterns in _DB_PATTERNS.items():
        for pattern in patterns:
            if any(pattern in name for name in dep_names):
                return db_type
    return None


def detect_cache_type(dependencies: list[Dependency]) -> str | None:
    """Infer cache type from dependencies."""
    dep_names = {d.name.lower() for d in dependencies}

    if any("redis" in n or "ioredis" in n for n in dep_names):
        return "redis"
    if any("memcache" in n for n in dep_names):
        return "memcached"
    if any("caffeine" in n for n in dep_names):
        return "caffeine"
    return None


# --- Kafka topic detection ---

# Common patterns for Kafka topic references in source code
_KAFKA_TOPIC_RE = re.compile(
    r"""(?:topic|TOPIC|Topic)\s*[=:]\s*['"]([a-zA-Z0-9._-]+)['"]"""
)
_KAFKA_PRODUCE_RE = re.compile(
    r"""(?:produce|send|emit|publish)\w*\s*\(\s*['"]([a-zA-Z0-9._-]+)['"]"""
    , re.IGNORECASE
)
_KAFKA_CONSUME_RE = re.compile(
    r"""(?:subscribe|consume|listen|on)\w*\s*\(\s*['"]([a-zA-Z0-9._-]+)['"]"""
    , re.IGNORECASE
)


def scan_kafka_topics(repo_path: Path, extensions: tuple[str, ...] = (".ts", ".js", ".go", ".py")) -> tuple[list[str], list[str]]:
    """Scan source files for Kafka topic references.

    Returns:
        (produces, consumes) — lists of topic names
    """
    produces: set[str] = set()
    consumes: set[str] = set()

    for ext in extensions:
        for f in repo_path.rglob(f"*{ext}"):
            if "node_modules" in str(f) or "vendor" in str(f) or ".git" in str(f):
                continue
            try:
                content = f.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue

            for m in _KAFKA_PRODUCE_RE.finditer(content):
                produces.add(m.group(1))
            for m in _KAFKA_CONSUME_RE.finditer(content):
                consumes.add(m.group(1))

    return sorted(produces), sorted(consumes)


# --- Dockerfile parsing ---


def parse_dockerfile(repo_path: Path) -> RuntimeInfo | None:
    """Parse Dockerfile for runtime info."""
    dockerfile = repo_path / "Dockerfile"
    if not dockerfile.is_file():
        return None

    try:
        content = dockerfile.read_text(encoding="utf-8")
    except OSError:
        return None

    return RuntimeInfo(docker=True)


# --- OpenAPI spec discovery ---


def find_openapi_spec(repo_path: Path) -> Path | None:
    """Find an OpenAPI/Swagger spec file in the repo."""
    candidates = [
        "openapi.yaml", "openapi.yml", "openapi.json",
        "swagger.yaml", "swagger.yml", "swagger.json",
        "api/openapi.yaml", "api/openapi.yml",
        "docs/openapi.yaml", "docs/swagger.json",
    ]
    for candidate in candidates:
        path = repo_path / candidate
        if path.is_file():
            return path
    return None


# --- Outbound HTTP call detection ---

# Patterns for common HTTP client calls across languages
_HTTP_URL_RE = re.compile(
    r"""(?:get|post|put|delete|patch|request)\s*\(\s*[`'"](https?://[^'"` ]+|/api/[^'"` ]+)['"`]""",
    re.IGNORECASE,
)

_ENV_URL_RE = re.compile(
    r"""(?:API_URL|BASE_URL|SERVICE_URL|BACKEND_URL)\s*[=:]\s*['"]([^'"]+)['"]""",
    re.IGNORECASE,
)


def scan_outbound_urls(repo_path: Path, extensions: tuple[str, ...] = (".ts", ".js", ".go", ".py")) -> list[str]:
    """Scan source files for outbound HTTP call URL patterns.

    Returns a list of unique URL patterns found.
    """
    urls: set[str] = set()

    for ext in extensions:
        for f in repo_path.rglob(f"*{ext}"):
            if "node_modules" in str(f) or "vendor" in str(f) or ".git" in str(f) or "test" in str(f).lower():
                continue
            try:
                content = f.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue

            for m in _HTTP_URL_RE.finditer(content):
                urls.add(m.group(1))

    return sorted(urls)
