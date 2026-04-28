"""Backend Java (Spring Boot) extractor — parses Java/Spring Boot repositories.

Extracts:
- Language (Java/Kotlin) from source file counts (excluding build/ directories)
- Java version from sourceCompatibility in build.gradle
- Spring Boot version from plugins block in build.gradle
- Framework detection (spring-boot, micronaut, quarkus)
- Gradle plugins from plugins { } block
- Dependencies from build.gradle (Groovy and Kotlin DSL), with ext{} variable resolution
- Spring annotation-based API endpoint extraction (@RequestMapping, @GetMapping, etc.)
- Entry points: @SpringBootApplication, @KafkaListener, @Scheduled, programmatic Kafka listeners
- Kafka topics from application.yml, with Spring EL ${VAR:default} resolution
- Database type (primary + secondary), cache type, and Flyway migration count
- Runtime info (Docker, k8s)
- CI system detection (GitHub Actions, Azure Pipelines, GitLab CI, Jenkins)
- Source repo git info
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

import structlog

from atlas import __version__
from atlas.extractors.base import Extractor
from atlas.schema import (
    ApiContract,
    Dependency,
    EndpointIndex,
    EntryPoint,
    RuntimeInfo,
    ServiceManifest,
    ServiceYaml,
)

logger = structlog.get_logger()

# Gradle dependency configurations → categories (Groovy + Kotlin DSL)
_DEP_CONFIG_CATEGORY: dict[str, str] = {
    "implementation": "runtime",
    "api": "runtime",
    "runtimeOnly": "runtime",
    "compileOnly": "runtime",
    "testImplementation": "test",
    "testRuntimeOnly": "test",
    "testCompileOnly": "test",
    "annotationProcessor": "build",
    "kapt": "build",
    "ksp": "build",
    "developmentOnly": "debug",
}

# All configs for regex alternation (longest first to avoid prefix shadowing)
_ALL_CONFIGS = "|".join(re.escape(k) for k in sorted(_DEP_CONFIG_CATEGORY, key=len, reverse=True))

# Spring HTTP method annotation names
_HTTP_METHODS = ["Get", "Post", "Put", "Delete", "Patch"]

# Directories to exclude when counting source files
_EXCLUDED_DIRS = {"build", "generated", ".gradle", "out", "target"}

# Source directories that contain non-production code — excluded from entry point scanning
_TEST_DIR_SEGMENTS = {"test", "androidTest", "integrationTest"}


class BackendJavaExtractor(Extractor):
    """Extractor for Java/Spring Boot repositories."""

    type = "backend-java"

    def extract(self, repo_path: Path, service_yaml: ServiceYaml) -> ServiceManifest:
        """Extract metadata from a Java/Spring Boot repo."""
        # Respect extractor_hints.project_root
        effective_root = repo_path
        if service_yaml.extractor_hints and service_yaml.extractor_hints.project_root:
            effective_root = repo_path / service_yaml.extractor_hints.project_root

        language, java_version = self._detect_language(effective_root)
        gradle_meta = self._parse_gradle_metadata(effective_root)
        spring_boot_version = gradle_meta.get("spring_boot_version")
        gradle_plugins = gradle_meta.get("plugins", [])

        # Use java_version from language detection first, then from Gradle metadata
        if java_version is None:
            java_version = gradle_meta.get("java_version")

        framework = self._detect_framework(effective_root)
        # Parse ext{} variable map first so dependency version resolution can use it
        gradle_vars = self._parse_gradle_ext_vars(effective_root)
        dependencies = self._parse_dependencies(effective_root, gradle_vars)
        api_contracts = self.find_api_contracts(effective_root)
        entry_points = self._parse_entry_points(effective_root)
        kafka_topics = self._parse_kafka_topics(effective_root)
        database_type, secondary_databases, flyway_count = self._parse_database_info(effective_root)
        cache_type = self._detect_cache_type(effective_root)
        runtime = self._detect_runtime(effective_root)
        ci = self._detect_ci(repo_path)
        source_repo = self._get_source_repo(repo_path)

        return ServiceManifest(
            name=service_yaml.name,
            type=service_yaml.type,
            owner=service_yaml.owner,
            domain=service_yaml.domain,
            tier=service_yaml.tier,
            status=service_yaml.status,
            purpose=service_yaml.purpose,
            keywords=service_yaml.keywords,
            language=language,
            language_version=java_version,
            slack=service_yaml.slack,
            runbook=service_yaml.runbook,
            jira_component=service_yaml.jira_component,
            gradle_plugins=gradle_plugins,
            dependencies=dependencies,
            entry_points=entry_points,
            api_contracts=api_contracts,
            runtime=runtime,
            ci=ci,
            spring_boot_version=spring_boot_version,
            java_version=java_version,
            framework=framework,
            flyway_migration_count=flyway_count,
            kafka_topics=kafka_topics,
            database_type=database_type,
            secondary_databases=secondary_databases,
            cache_type=cache_type,
            integration_notes=(
                [{"scope": n.scope, "note": n.note} for n in service_yaml.integration_notes]
                if service_yaml.integration_notes
                else []
            ),
            extracted_at=datetime.now(timezone.utc),
            extractor_version=__version__,
            source_repo=source_repo,
        )

    def find_api_contracts(self, repo_path: Path) -> list[ApiContract]:
        """Extract API contracts from Spring controller annotations."""
        endpoints = self._parse_spring_endpoints(repo_path)
        if not endpoints:
            return []
        return [ApiContract(kind="spring-annotations", endpoints=endpoints)]

    # --- Private parsing methods ---

    def _detect_language(self, root: Path) -> tuple[str, str | None]:
        """Detect primary language by counting .java vs .kt source files.

        Excludes build/, generated/, target/, out/, and .gradle/ directories.
        Also attempts to parse Java version from build.gradle.
        """

        def _count_files(ext: str) -> int:
            count = 0
            for p in root.rglob(f"*{ext}"):
                if not any(part in _EXCLUDED_DIRS for part in p.parts):
                    count += 1
            return count

        java_count = _count_files(".java")
        kt_count = _count_files(".kt")

        # Parse Java version from build.gradle
        java_version = self._parse_java_version(root)

        if java_count >= kt_count:
            return "java", java_version
        return "kotlin", java_version

    def _parse_java_version(self, root: Path) -> str | None:
        """Parse Java source compatibility version from build.gradle."""
        for name in ["build.gradle", "build.gradle.kts"]:
            p = root / name
            if not p.exists():
                continue
            content = p.read_text(errors="replace")
            # Groovy: sourceCompatibility = '17' or sourceCompatibility = JavaVersion.VERSION_17
            # Kotlin DSL: sourceCompatibility = JavaVersion.VERSION_17
            m = re.search(
                r"sourceCompatibility\s*[=:]\s*(?:JavaVersion\.VERSION_)?['\"]?(\d+)['\"]?",
                content,
            )
            if m:
                return m.group(1)
            # java { sourceCompatibility = JavaVersion.VERSION_17 }
            m = re.search(
                r"sourceCompatibility\s*=\s*JavaVersion\.VERSION_(\d+)",
                content,
            )
            if m:
                return m.group(1)
        return None

    def _parse_gradle_metadata(self, root: Path) -> dict:
        """Parse build.gradle (Groovy or Kotlin DSL) for project metadata.

        Returns a dict with:
            - spring_boot_version: str | None
            - java_version: str | None
            - plugins: list[str]
        """
        result: dict = {
            "spring_boot_version": None,
            "java_version": None,
            "plugins": [],
        }

        for name in ["build.gradle", "build.gradle.kts"]:
            p = root / name
            if not p.exists():
                continue
            content = p.read_text(errors="replace")

            # Spring Boot version from plugins block
            # Groovy: id 'org.springframework.boot' version '3.1.10'
            # Kotlin DSL: id("org.springframework.boot") version "3.1.10"
            m = re.search(
                r"""id\s*[\(]?\s*['"]org\.springframework\.boot['"]\s*[\)]?\s*version\s*['"]([^'"]+)['"]""",
                content,
            )
            if m and result["spring_boot_version"] is None:
                result["spring_boot_version"] = m.group(1)

            # Java version from sourceCompatibility
            if result["java_version"] is None:
                m = re.search(
                    r"sourceCompatibility\s*[=:]\s*(?:JavaVersion\.VERSION_)?['\"]?(\d+)['\"]?",
                    content,
                )
                if m:
                    result["java_version"] = m.group(1)

            # Extract all plugin IDs from plugins block
            plugins_block_m = re.search(r"plugins\s*\{([^}]+)\}", content, re.DOTALL)
            if plugins_block_m:
                plugins_block = plugins_block_m.group(1)
                # Match: id 'plugin.id' or id("plugin.id") or id 'plugin.id' version '...'
                for pm in re.finditer(r"""id\s*[\(]?\s*['"]([^'"]+)['"]""", plugins_block):
                    pid = pm.group(1)
                    if pid not in result["plugins"]:
                        result["plugins"].append(pid)

            break  # Use root-level build.gradle only

        return result

    def _detect_framework(self, root: Path) -> str | None:
        """Detect backend framework from build.gradle plugins block."""
        for name in ["build.gradle", "build.gradle.kts"]:
            p = root / name
            if not p.exists():
                continue
            content = p.read_text(errors="replace")
            if "org.springframework.boot" in content:
                return "spring-boot"
            if "io.micronaut" in content:
                return "micronaut"
            if "io.quarkus" in content:
                return "quarkus"
        return None

    def _parse_gradle_ext_vars(self, root: Path) -> dict[str, str]:
        """Parse variable definitions from the Gradle ext { } block.

        Handles Groovy-style:
            ext {
                lombokVersion = '1.18.26'
                springBootVersion = '3.1.0'
            }

        Returns a dict mapping variable name → resolved string value, e.g.:
            {"lombokVersion": "1.18.26", "springBootVersion": "3.1.0"}
        """
        vars_map: dict[str, str] = {}

        for name in ["build.gradle", "build.gradle.kts"]:
            p = root / name
            if not p.exists():
                continue
            content = p.read_text(errors="replace")

            # Find ext { } block (may appear multiple times; collect all)
            for block_m in re.finditer(r"\bext\s*\{([^}]+)\}", content, re.DOTALL):
                block = block_m.group(1)
                # Match: varName = 'value'  or  varName = "value"
                for var_m in re.finditer(r"""(\w+)\s*=\s*['"]([^'"]+)['"]""", block):
                    var_name = var_m.group(1)
                    var_val = var_m.group(2)
                    # Last definition wins (same as Gradle evaluation order)
                    vars_map[var_name] = var_val

            break  # Root build.gradle only

        return vars_map

    def _resolve_gradle_version(self, raw_version: str | None, vars_map: dict[str, str]) -> str | None:
        """Resolve a version string that may contain a Gradle variable reference.

        Handles:
            "${springBootVersion}" → looks up "springBootVersion" in vars_map
            "3.1.10"              → returned as-is
            None                  → None
        """
        if raw_version is None:
            return None
        m = re.fullmatch(r"\$\{(\w+)\}", raw_version)
        if m:
            return vars_map.get(m.group(1), raw_version)
        return raw_version

    def _parse_dependencies(self, root: Path, gradle_vars: dict[str, str] | None = None) -> list[Dependency]:
        """Parse dependencies from build.gradle (Groovy or Kotlin DSL)."""
        deps: list[Dependency] = []
        seen: set[str] = set()
        vars_map = gradle_vars or {}

        for gf in root.rglob("build.gradle*"):
            if gf.is_file() and not any(part in _EXCLUDED_DIRS for part in gf.parts):
                content = gf.read_text(errors="replace")
                self._parse_gradle_deps(content, gf.name, deps, seen, vars_map)

        return deps

    def _parse_gradle_deps(
        self, content: str, source: str, deps: list[Dependency], seen: set[str],
        vars_map: dict[str, str] | None = None,
    ) -> None:
        """Parse dependency declarations from Gradle content.

        Handles Groovy and Kotlin DSL patterns:
            implementation 'group:artifact:version'
            implementation("group:artifact:version")
            testImplementation 'group:artifact:version'

        Version strings containing Gradle variable references (${varName}) are
        resolved against vars_map when provided.
        """
        if vars_map is None:
            vars_map = {}

        pattern = rf"""({_ALL_CONFIGS})\s*[\(]?\s*["']([^"']+)["']"""
        for m in re.finditer(pattern, content):
            config_name = m.group(1)
            dep_str = m.group(2)
            parts = dep_str.split(":")
            if len(parts) >= 2:
                name = f"{parts[0]}:{parts[1]}"
                if name not in seen:
                    seen.add(name)
                    category = _DEP_CONFIG_CATEGORY.get(config_name)
                    raw_version = parts[2] if len(parts) > 2 else None
                    resolved_version = self._resolve_gradle_version(raw_version, vars_map)
                    deps.append(
                        Dependency(
                            name=name,
                            version=resolved_version,
                            source=source,
                            direct=True,
                            category=category,
                        )
                    )

    def _parse_spring_endpoints(self, root: Path) -> list[EndpointIndex]:
        """Extract API endpoints from Spring controller annotations.

        Parses:
        - @RestController / @Controller classes
        - Class-level @RequestMapping for base path
        - Class-level @Tag for OpenAPI tag
        - Method-level @GetMapping, @PostMapping, @PutMapping, @DeleteMapping, @PatchMapping
        - Method-level @Operation(summary = "...")
        - Method-level @Tag (overrides class-level)

        Handles two patterns:
        A) @RequestMapping("/v1/events") at class level + relative paths at method level
        B) @RequestMapping("/v1") at class level + full paths at method level
        C) No class-level @RequestMapping — uses method paths as-is
        """
        endpoints: list[EndpointIndex] = []

        # Find all Java controller files
        controller_files: list[Path] = []
        for java_file in root.rglob("*.java"):
            if any(part in _EXCLUDED_DIRS for part in java_file.parts):
                continue
            # Check if it's in a controllers/controller directory or has Controller in name
            parts_lower = [p.lower() for p in java_file.parts]
            if any("controller" in p for p in parts_lower) or java_file.stem.endswith(
                "Controller"
            ):
                controller_files.append(java_file)

        for controller_file in controller_files:
            try:
                content = controller_file.read_text(errors="replace")
            except OSError:
                logger.warning("Failed to read controller file", path=str(controller_file))
                continue

            # Skip non-controller Java files (no @RestController or @Controller annotation)
            if "@RestController" not in content and "@Controller" not in content:
                continue

            file_endpoints = self._extract_endpoints_from_controller(content, controller_file)
            endpoints.extend(file_endpoints)

        # Deduplicate by (method, path) — keep the first occurrence (retains summary/tags)
        seen_ep: set[tuple[str | None, str | None]] = set()
        deduped: list[EndpointIndex] = []
        for ep in endpoints:
            key = (ep.method, ep.path)
            if key not in seen_ep:
                seen_ep.add(key)
                deduped.append(ep)

        return deduped

    def _extract_endpoints_from_controller(
        self, content: str, source_file: Path
    ) -> list[EndpointIndex]:
        """Extract endpoints from a single controller file.

        Strategy: scan for @*Mapping annotations line by line. For each, look
        in a bounded pre-annotation window (between this mapping and the previous
        mapping) for @Operation summary and @Tag. Extract path from the annotation
        itself using a targeted approach.
        """
        endpoints: list[EndpointIndex] = []

        # --- Class-level metadata ---
        class_base_path = self._extract_request_mapping_path(content, scope="class")
        class_tag = self._extract_tag_name(content, scope="class")

        # --- Find all HTTP mapping annotations with their positions ---
        method_pattern = re.compile(
            r"@(Get|Post|Put|Delete|Patch)Mapping\b",
            re.MULTILINE,
        )

        all_matches = list(method_pattern.finditer(content))

        for i, method_match in enumerate(all_matches):
            http_verb = method_match.group(1).upper()

            # Extract path from the mapping annotation arguments
            method_path = self._extract_mapping_path(content, method_match.start())

            # The pre-annotation window: from the end of the previous @*Mapping annotation
            # (or the class @RequestMapping) to the start of this @*Mapping.
            # This isolates @Operation and @Tag for THIS method only.
            if i == 0:
                # Before the first method mapping — start from after class-level annotations
                # Use a reasonable lookback but not too far
                window_start = max(0, method_match.start() - 300)
            else:
                # Start from the end of the previous method mapping annotation
                prev_match = all_matches[i - 1]
                window_start = prev_match.end()

            pre_window = content[window_start : method_match.start()]

            summary = self._extract_operation_summary(pre_window)

            # Look for method-level @Tag (overrides class-level)
            method_tag = self._extract_tag_name(pre_window, scope="method")
            tag = method_tag or class_tag

            # Combine base path + method path
            full_path = self._combine_paths(class_base_path, method_path)

            endpoint = EndpointIndex(
                method=http_verb,
                path=full_path,
                summary=summary,
                tags=[tag] if tag else [],
            )
            endpoints.append(endpoint)

        return endpoints

    def _extract_request_mapping_path(self, content: str, scope: str = "class") -> str | None:
        """Extract the path from @RequestMapping annotation.

        For class scope: looks for @RequestMapping before the class declaration.
        For method scope: looks for the nearest @RequestMapping.
        """
        # Match @RequestMapping("/path") or @RequestMapping(value = "/path")
        # Handle both single and double quotes, and optional 'value ='
        pattern = re.compile(
            r'@RequestMapping\s*\(\s*(?:value\s*=\s*)?["\']([^"\']+)["\']',
            re.MULTILINE,
        )
        m = pattern.search(content)
        if m:
            path = m.group(1)
            # Normalize: ensure leading slash
            if path and not path.startswith("/"):
                path = "/" + path
            return path
        return None

    def _extract_mapping_path(self, content: str, annotation_start: int) -> str | None:
        """Extract the path argument from a @*Mapping annotation starting at annotation_start.

        Handles:
        - @GetMapping → no path (returns None)
        - @GetMapping() → no path (returns None)
        - @GetMapping("/path") → "/path"
        - @GetMapping(value = "/path") → "/path"
        - @PostMapping("/{id}") → "/{id}"

        Critically: only looks inside the annotation's own parentheses, not beyond.
        """
        # Advance past the annotation name to find the char immediately after @*Mapping
        # The annotation name ends at the next non-word character
        name_end = annotation_start
        while name_end < len(content) and (content[name_end] == "@" or content[name_end].isalnum()):
            name_end += 1

        # Skip whitespace
        pos = name_end
        while pos < len(content) and content[pos] in " \t":
            pos += 1

        # Check if there's a parenthesis
        if pos >= len(content) or content[pos] != "(":
            # No parenthesis — bare annotation like @GetMapping followed by newline
            return None

        # Find the matching close paren
        paren_depth = 0
        paren_start = pos
        paren_end = pos
        for j in range(pos, min(pos + 400, len(content))):
            ch = content[j]
            if ch == "(":
                paren_depth += 1
            elif ch == ")":
                paren_depth -= 1
                if paren_depth == 0:
                    paren_end = j
                    break

        # Extract only the contents inside the parens
        inner = content[paren_start + 1 : paren_end]

        # Empty parens: @GetMapping()
        if not inner.strip():
            return None

        # Match value = "/path" or just "/path"
        path_m = re.search(
            r'(?:value\s*=\s*)?["\']([^"\']*)["\']',
            inner,
        )
        if path_m:
            path = path_m.group(1)
            if path and not path.startswith("/"):
                path = "/" + path
            return path or None
        return None

    def _extract_operation_summary(self, text: str) -> str | None:
        """Extract @Operation(summary = "...") from text.

        Discards summaries that look like URL paths — these are copy-paste noise
        from codebases that use the summary field as a route reference instead of
        a human-readable description. Only returns genuine descriptions.

        Patterns that are discarded:
        - Absolute paths: "/accounts/redeem", "/shopping-cart/tickets"
        - Version-prefixed paths: "v1/accounts/redeem", "v2/accounts/seats"
        - Resource-path fragments: "tm-events/active", "games/{gameId}/confirm"
        - Path parameter fragments: "{tmId}/lite", "{gameId}/guest"
        - Single-segment paths: "/status", "/health", "/availability"
        - Query-string URLs: "/accounts/validate?email=..."
        """
        m = re.search(
            r'@Operation\s*\(\s*(?:[^)]*?\s)?summary\s*=\s*["\']([^"\']+)["\']',
            text,
            re.DOTALL,
        )
        if not m:
            return None

        summary = m.group(1).strip()
        if not summary:
            return None

        # Rule 1: starts with "/" (absolute path)
        if summary.startswith("/"):
            return None

        # Rule 2: starts with version prefix "v1/", "v2/", etc.
        if re.match(r"v\d+[/\s]", summary):
            return None

        # Rule 3: looks like a path fragment — contains "/" and starts with a
        # lowercase word or "{" (path parameter). Human descriptions don't start with "{".
        if summary.startswith("{"):
            return None

        # Rule 4: contains "/" and the first segment looks like a resource name
        # (lowercase-kebab or path param), not a sentence.
        # e.g. "tm-events/active", "games/{gameId}/confirm", "seats/import"
        # A real description might be "List all orders" (capital letter, no slashes at start)
        if "/" in summary:
            first_segment = summary.split("/")[0]
            # If first segment is lowercase-only or kebab-case (no spaces, no capitals)
            # → it's a resource path fragment, not a sentence
            if re.match(r"^[a-z0-9{][a-z0-9\-{}]*$", first_segment):
                return None

        # Rule 5: query-string URL
        if "?" in summary and "=" in summary and " " not in summary.split("?")[0]:
            return None

        return summary

    def _extract_tag_name(self, text: str, scope: str = "class") -> str | None:
        """Extract @Tag(name = "...") from text."""
        m = re.search(
            r'@Tag\s*\(\s*name\s*=\s*["\']([^"\']+)["\']',
            text,
        )
        if m:
            return m.group(1)
        return None

    def _combine_paths(self, base: str | None, method: str | None) -> str | None:
        """Combine class-level base path with method-level path."""
        if base is None and method is None:
            return None
        if base is None:
            return method
        if method is None:
            return base

        # Normalize: ensure base doesn't end with /, method starts with /
        base = base.rstrip("/")
        if method and not method.startswith("/"):
            method = "/" + method

        return base + method

    def _parse_entry_points(self, root: Path) -> list[EntryPoint]:
        """Find Spring entry points.

        Detects:
        - @SpringBootApplication — application main class
        - @KafkaListener — annotation-based Kafka consumers
        - Programmatic Kafka listener registration (KafkaListenerEndpointRegistry,
          MethodKafkaListenerEndpoint) — for services that register listeners in code
        - @KafkaHandler — handler methods on @KafkaListener classes
        - @Scheduled — scheduled/cron job classes

        A single class can have multiple entry point kinds. Deduplication is per
        (kind, ref) pair.
        """
        entry_points: list[EntryPoint] = []
        seen_pairs: set[tuple[str, str]] = set()

        def _add(kind: str, ref: str) -> None:
            pair = (kind, ref)
            if pair not in seen_pairs:
                seen_pairs.add(pair)
                entry_points.append(EntryPoint(kind=kind, ref=ref))

        for java_file in root.rglob("*.java"):
            parts = java_file.parts
            if any(part in _EXCLUDED_DIRS for part in parts):
                continue
            # Skip test source trees (src/test/java, src/androidTest/java, etc.)
            if any(part in _TEST_DIR_SEGMENTS for part in parts):
                continue
            try:
                content = java_file.read_text(errors="replace")
            except OSError:
                continue

            class_name = None  # Lazy-resolve per file

            if "@SpringBootApplication" in content:
                if class_name is None:
                    class_name = self._extract_class_name(content, java_file)
                if class_name:
                    _add("spring-boot-application", class_name)

            # Annotation-based Kafka consumers
            if "@KafkaListener" in content or "@KafkaHandler" in content:
                if class_name is None:
                    class_name = self._extract_class_name(content, java_file)
                if class_name:
                    _add("kafka-consumer", class_name)

            # Programmatic Kafka listener registration
            if "KafkaListenerEndpointRegistry" in content or "MethodKafkaListenerEndpoint" in content:
                if class_name is None:
                    class_name = self._extract_class_name(content, java_file)
                if class_name:
                    _add("kafka-consumer", class_name)

            if "@Scheduled" in content:
                if class_name is None:
                    class_name = self._extract_class_name(content, java_file)
                if class_name:
                    _add("scheduled-job", class_name)

        return entry_points

    def _extract_class_name(self, content: str, java_file: Path) -> str | None:
        """Extract the fully-qualified class name from Java file content.

        Combines package declaration + class name from the file.
        Falls back to the filename stem if parsing fails.
        """
        package_m = re.search(r"^package\s+([\w.]+)\s*;", content, re.MULTILINE)
        class_m = re.search(
            r"(?:public\s+)?(?:abstract\s+)?class\s+(\w+)", content, re.MULTILINE
        )
        if package_m and class_m:
            return f"{package_m.group(1)}.{class_m.group(1)}"
        if class_m:
            return class_m.group(1)
        return java_file.stem

    def _parse_kafka_topics(self, root: Path) -> list[str]:
        """Parse Kafka topic names from application.yml.

        Looks for string values that look like topic names under common Kafka config keys.
        Uses PyYAML for structured parsing, falls back to regex if unavailable.
        """
        topics: list[str] = []
        seen: set[str] = set()

        for yml_file in root.rglob("application*.yml"):
            if any(part in _EXCLUDED_DIRS for part in yml_file.parts):
                continue
            try:
                content = yml_file.read_text(errors="replace")
            except OSError:
                continue

            self._extract_kafka_topics_from_yaml(content, topics, seen)

        return topics

    def _resolve_spring_el_topic(self, raw: str) -> str:
        """Resolve a Spring EL expression to a usable topic name.

        Handles:
            "${DATA_TICKET_STATUS_EVENT:ticketing-data-ticket-status}"
                → "ticketing-data-ticket-status"  (use default value)
            "${DATA_PURCHASE_EVENT_TOPIC}"
                → "DATA_PURCHASE_EVENT_TOPIC"  (strip ${} wrapper, keep env var name)
            "plain-topic-name"
                → "plain-topic-name"  (returned as-is)
        """
        m = re.fullmatch(r"\$\{([^}]+)\}", raw.strip())
        if not m:
            return raw

        inner = m.group(1)
        # Spring EL default syntax: VAR_NAME:default-value
        if ":" in inner:
            default = inner.split(":", 1)[1]
            return default if default else inner.split(":", 1)[0]

        # No default — return the env var name without ${} so it's still readable
        return inner

    def _extract_kafka_topics_from_yaml(
        self, content: str, topics: list[str], seen: set[str]
    ) -> None:
        """Extract Kafka topic names from YAML content.

        Tries PyYAML structured parsing first; falls back to regex pattern matching.
        Resolves Spring EL expressions (${VAR:default}) to their default values.
        """
        try:
            import yaml

            data = yaml.safe_load(content)
            if isinstance(data, dict):
                self._walk_yaml_for_topics(data, topics, seen)
            return
        except Exception:
            pass

        # Regex fallback: look for values that look like topic names under topic-ish keys
        topic_pattern = re.compile(
            r"""(?:topic|topics?)\s*:\s*['"]?([a-zA-Z0-9._\-${}:]+)['"]?""",
            re.IGNORECASE,
        )
        for m in topic_pattern.finditer(content):
            candidate = self._resolve_spring_el_topic(m.group(1).strip())
            if candidate and candidate not in seen and len(candidate) > 3:
                seen.add(candidate)
                topics.append(candidate)

    def _walk_yaml_for_topics(
        self, data: dict | list | str, topics: list[str], seen: set[str], key_path: str = ""
    ) -> None:
        """Recursively walk YAML structure looking for Kafka topic values.

        Heuristic: collect string values under keys that contain 'topic'.
        Spring EL expressions (${VAR:default}) are resolved to their default values.
        """
        if isinstance(data, dict):
            for key, value in data.items():
                lower_key = str(key).lower()
                child_path = f"{key_path}.{key}" if key_path else str(key)
                if isinstance(value, str):
                    # Collect if: key contains 'topic', or we're under a 'topics' parent
                    if "topic" in lower_key or (
                        "kafka" in key_path.lower() and "topic" in key_path.lower()
                    ):
                        resolved = self._resolve_spring_el_topic(value)
                        if resolved and len(resolved) > 3 and resolved not in seen:
                            seen.add(resolved)
                            topics.append(resolved)
                else:
                    self._walk_yaml_for_topics(value, topics, seen, child_path)
        elif isinstance(data, list):
            for item in data:
                if isinstance(item, str) and "topic" in key_path.lower() and len(item) > 3:
                    resolved = self._resolve_spring_el_topic(item)
                    if resolved and resolved not in seen:
                        seen.add(resolved)
                        topics.append(resolved)
                elif isinstance(item, dict):
                    self._walk_yaml_for_topics(item, topics, seen, key_path)

    def _parse_database_info(self, root: Path) -> tuple[str | None, list[str], int | None]:
        """Detect primary/secondary databases and count Flyway migrations.

        Returns:
            (primary_database_type, secondary_databases, flyway_migration_count)
        """
        primary, secondary = self._detect_database_types(root)
        flyway_count = self._count_flyway_migrations(root)
        return primary, secondary, flyway_count

    def _detect_database_types(self, root: Path) -> tuple[str | None, list[str]]:
        """Detect all database types from application.yml and build.gradle dependencies.

        Scans both application config (for datasource URLs) and build.gradle (for
        database driver/connector dependencies) to find all databases in use.

        Returns (primary_db, secondary_dbs) where primary is the first JDBC datasource
        found, and secondary_dbs contains any additional detected databases.
        """
        db_patterns = [
            (r"postgresql|postgres", "postgresql"),
            (r"mysql", "mysql"),
            (r"mariadb", "mariadb"),
            (r"sqlserver|mssql", "sqlserver"),
            (r"oracle", "oracle"),
            (r"h2", "h2"),
            (r"cosmos|cosmosdb", "cosmos"),
            (r"mongodb", "mongodb"),
            (r"cassandra", "cassandra"),
            (r"dynamodb", "dynamodb"),
        ]

        detected: list[str] = []

        # --- Scan application*.yml for datasource URLs ---
        for yml_file in root.rglob("application*.yml"):
            if any(part in _EXCLUDED_DIRS for part in yml_file.parts):
                continue
            try:
                content = yml_file.read_text(errors="replace").lower()
            except OSError:
                continue

            for pattern, db_type in db_patterns:
                if db_type in detected:
                    continue
                if re.search(rf"(?:url|driver[_-]class[_-]name|datasource)\s*:.*{pattern}", content):
                    detected.append(db_type)
                elif re.search(rf"jdbc:{pattern}", content):
                    detected.append(db_type)

        # --- Scan build.gradle for database connector dependencies ---
        dep_db_signals = [
            (r"org\.postgresql:postgresql|jdbc:postgresql", "postgresql"),
            (r"mysql:mysql-connector|com\.mysql:mysql-connector", "mysql"),
            (r"org\.mariadb", "mariadb"),
            (r"com\.microsoft\.sqlserver", "sqlserver"),
            (r"com\.oracle", "oracle"),
            (r"com\.h2database:h2", "h2"),
            (r"com\.azure:azure-spring-data-cosmos|azure-cosmos", "cosmos"),
            (r"org\.springframework\.data:spring-data-mongodb|de\.flapdoodle", "mongodb"),
            (r"com\.datastax|spring-data-cassandra", "cassandra"),
        ]

        for name in ["build.gradle", "build.gradle.kts"]:
            p = root / name
            if not p.exists():
                continue
            try:
                content = p.read_text(errors="replace").lower()
            except OSError:
                continue

            for pattern, db_type in dep_db_signals:
                if db_type not in detected and re.search(pattern, content):
                    detected.append(db_type)

        if not detected:
            return None, []

        # H2 is only ever a test database — move it to secondary if a real DB was found
        real_dbs = [d for d in detected if d != "h2"]
        test_dbs = [d for d in detected if d == "h2"]

        if real_dbs:
            primary = real_dbs[0]
            secondary = real_dbs[1:] + test_dbs
        else:
            primary = test_dbs[0]
            secondary = test_dbs[1:]

        return primary, secondary

    def _detect_cache_type(self, root: Path) -> str | None:
        """Detect caching technology from dependencies and application config.

        Checks build.gradle for cache library dependencies and application.yml
        for spring.cache / spring.data.redis configuration.
        """
        cache_signals = [
            (r"spring-boot-starter-data-redis|redisson|lettuce|jedis", "redis"),
            (r"spring-boot-starter-cache.*caffeine|caffeine", "caffeine"),
            (r"memcached|spymemcached|xmemcached", "memcached"),
            (r"hazelcast", "hazelcast"),
            (r"ehcache", "ehcache"),
        ]

        for name in ["build.gradle", "build.gradle.kts"]:
            p = root / name
            if not p.exists():
                continue
            try:
                content = p.read_text(errors="replace").lower()
            except OSError:
                continue
            for pattern, cache_type in cache_signals:
                if re.search(pattern, content):
                    return cache_type

        # Also check application.yml for spring.data.redis / spring.cache keys
        for yml_file in root.rglob("application*.yml"):
            if any(part in _EXCLUDED_DIRS for part in yml_file.parts):
                continue
            try:
                content = yml_file.read_text(errors="replace").lower()
            except OSError:
                continue
            if re.search(r"spring[.\s]+(?:data[.\s]+)?redis", content):
                return "redis"
            if re.search(r"spring[.\s]+cache[.\s]+type\s*:\s*caffeine", content):
                return "caffeine"

        return None

    def _count_flyway_migrations(self, root: Path) -> int | None:
        """Count Flyway migration scripts (V*.sql files) in db/migration directories."""
        count = 0
        for migration_dir in root.rglob("db/migration"):
            if not migration_dir.is_dir():
                continue
            if any(part in _EXCLUDED_DIRS for part in migration_dir.parts):
                continue
            for sql_file in migration_dir.iterdir():
                if sql_file.is_file() and re.match(r"V\d+", sql_file.name):
                    count += 1

        return count if count > 0 else None

    def _detect_runtime(self, root: Path) -> RuntimeInfo | None:
        """Detect runtime environment from Dockerfile and k8s manifests."""
        docker = (root / "Dockerfile").exists() or (root / "docker" / "Dockerfile").exists()

        k8s_manifests: str | None = None
        for k8s_dir in ["k8s", "kubernetes", "helm", "deploy", "devops/k8s", "devops/kubernetes"]:
            p = root / k8s_dir
            if p.is_dir():
                k8s_manifests = str(p)
                break

        if docker or k8s_manifests:
            return RuntimeInfo(docker=docker, k8s_manifests=k8s_manifests)
        return None

    def _detect_ci(self, root: Path) -> str | None:
        """Detect CI system from well-known config file locations.

        Checks:
        - .github/workflows/ → github-actions
        - azure-pipelines.yml (root or devops/) → azure-pipelines
        - .gitlab-ci.yml → gitlab-ci
        - Jenkinsfile → jenkins
        """
        if (root / ".github" / "workflows").is_dir():
            return "github-actions"

        # Azure Pipelines — may be at root or in devops/
        if (root / "azure-pipelines.yml").exists():
            return "azure-pipelines"
        for azure_candidate in root.rglob("azure-pipelines.yml"):
            if any(part in _EXCLUDED_DIRS for part in azure_candidate.parts):
                continue
            return "azure-pipelines"

        if (root / ".gitlab-ci.yml").exists():
            return "gitlab-ci"
        if (root / "Jenkinsfile").exists():
            return "jenkins"
        return None
