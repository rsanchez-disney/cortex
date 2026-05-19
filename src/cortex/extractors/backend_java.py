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
from datetime import UTC, datetime
from pathlib import Path

import structlog

from cortex import __version__
from cortex.extractors.base import Extractor
from cortex.schema import (
    ApiContract,
    Dependency,
    DtoField,
    DtoFieldConstraint,
    DtoSchema,
    EndpointIndex,
    EndpointParameter,
    EndpointRequestBody,
    EndpointResponse,
    EntryPoint,
    OutboundCall,
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

# Reactive/async wrapper types to unwrap from return types
_RESPONSE_WRAPPERS = frozenset({
    "ResponseEntity",
    "Mono",
    "Flux",
    "CompletableFuture",
    "DeferredResult",
    "Callable",
    "ListenableFuture",
    "Future",
})

# Source directories that contain non-production code — excluded from entry point scanning
_TEST_DIR_SEGMENTS = {"test", "androidTest", "integrationTest"}

# Java/Kotlin primitive and standard library types — never try to resolve these as DTOs
_PRIMITIVE_TYPES = frozenset({
    "void", "Void", "boolean", "Boolean", "byte", "Byte", "short", "Short",
    "int", "Integer", "long", "Long", "float", "Float", "double", "Double",
    "char", "Character", "String", "BigDecimal", "BigInteger",
    "Object", "Date", "LocalDate", "LocalDateTime", "LocalTime",
    "Instant", "ZonedDateTime", "OffsetDateTime", "Duration", "Period",
    "UUID", "URI", "URL", "Map", "List", "Set", "Collection",
    "Optional", "?", "T", "E", "K", "V",
    "MultipartFile", "HttpServletRequest", "HttpServletResponse",
    "InputStream", "OutputStream", "byte[]",
})

# Maximum recursion depth for nested DTO resolution
_MAX_DTO_DEPTH = 5

# Pattern to identify URL-like config keys in YAML (base-url, base-uri, .url, etc.)
# Matches keys ending with any of:
#   base-url, base_url, base-uri, base_uri  — typical service base-URL keys
#   .url, .uri, .host, .endpoint            — dot-separated leaf keys
#   -url, _url                              — arbitrary prefix + url suffix
#                                             (covers verifications-url, wallet-decryption-url,
#                                              environment_url, environment-url, etc.)
#   url-token                               — legacy token-url variant
_URL_KEY_PATTERN = re.compile(
    r"(?:base[_-]?url|base[_-]?uri|\.url|\.host|\.endpoint|\.uri|url[_-]token|[_-]url|_url)$",
    re.IGNORECASE,
)


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
        kafka_produces = self._parse_kafka_producers(effective_root)
        kafka_consumes = self._parse_kafka_consumers(effective_root)
        # Merge any topics not already in kafka_topics (backward compat union)
        all_kafka_topic_names = set(kafka_topics)
        for t in kafka_produces + kafka_consumes:
            if t not in all_kafka_topic_names:
                all_kafka_topic_names.add(t)
                kafka_topics = kafka_topics + [t]
        outbound_calls = self._parse_outbound_service_calls(effective_root)
        database_type, secondary_databases, flyway_count = self._parse_database_info(effective_root)
        cache_type = self._detect_cache_type(effective_root)
        runtime = self._detect_runtime(effective_root)
        ci = self._detect_ci(repo_path)
        source_repo = self._get_source_repo(repo_path)
        dto_schemas = self._extract_dto_schemas(effective_root, api_contracts)

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
            dto_schemas=dto_schemas,
            runtime=runtime,
            ci=ci,
            spring_boot_version=spring_boot_version,
            java_version=java_version,
            framework=framework,
            flyway_migration_count=flyway_count,
            kafka_topics=kafka_topics,
            kafka_produces=kafka_produces,
            kafka_consumes=kafka_consumes,
            outbound_calls=outbound_calls,
            database_type=database_type,
            secondary_databases=secondary_databases,
            cache_type=cache_type,
            integration_notes=(
                [{"scope": n.scope, "note": n.note} for n in service_yaml.integration_notes]
                if service_yaml.integration_notes
                else []
            ),
            extracted_at=datetime.now(UTC),
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

    def _resolve_gradle_version(
        self, raw_version: str | None, vars_map: dict[str, str]
    ) -> str | None:
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

    def _parse_dependencies(
        self, root: Path, gradle_vars: dict[str, str] | None = None
    ) -> list[Dependency]:
        """Parse dependencies from build.gradle (Groovy or Kotlin DSL) and libs.versions.toml."""
        deps: list[Dependency] = []
        seen: set[str] = set()
        vars_map = gradle_vars or {}

        # Parse Gradle version catalog first (provides alias → coordinate mapping)
        toml_path = root / "gradle" / "libs.versions.toml"
        if toml_path.exists():
            self._parse_version_catalog(toml_path, deps, seen)

        for gf in root.rglob("build.gradle*"):
            if gf.is_file() and not any(part in _EXCLUDED_DIRS for part in gf.parts):
                content = gf.read_text(errors="replace")
                self._parse_gradle_deps(content, gf.name, deps, seen, vars_map)

        return deps

    def _parse_version_catalog(
        self, toml_path: Path, deps: list[Dependency], seen: set[str]
    ) -> None:
        """Parse dependencies from a Gradle version catalog (gradle/libs.versions.toml).

        Handles both dict form (module/group+name keys) and string form
        (``"group:artifact:version"``). Version refs are resolved from the
        [versions] table.
        """
        try:
            import tomli
        except ImportError:
            logger.warning("tomli not installed, skipping version catalog parsing")
            return

        try:
            with open(toml_path, "rb") as f:
                catalog = tomli.load(f)
        except Exception as e:
            logger.warning("Failed to parse libs.versions.toml", path=str(toml_path), error=str(e))
            return

        versions = catalog.get("versions", {})
        libraries = catalog.get("libraries", {})

        # Category is always "runtime" here because the TOML itself carries no scope
        # information — scope (testImplementation, ksp, etc.) is declared in build.gradle
        # at the point where `libs.xxx` aliases are used, which is not parsed here.
        for _alias, lib_def in libraries.items():
            if isinstance(lib_def, str):
                # Simple string form: "group:artifact:version"
                parts = lib_def.split(":")
                if len(parts) >= 2:
                    name = f"{parts[0]}:{parts[1]}"
                    if name not in seen:
                        seen.add(name)
                        deps.append(
                            Dependency(
                                name=name,
                                version=parts[2] if len(parts) > 2 else None,
                                source="libs.versions.toml",
                                direct=True,
                                category="runtime",
                            )
                        )
            elif isinstance(lib_def, dict):
                module = lib_def.get("module")
                group = lib_def.get("group")
                artifact_name = lib_def.get("name")

                if module:
                    dep_name = module
                elif group and artifact_name:
                    dep_name = f"{group}:{artifact_name}"
                else:
                    continue

                version: str | None = None
                ver_ref = lib_def.get("version")
                if isinstance(ver_ref, str):
                    version = ver_ref
                elif isinstance(ver_ref, dict):
                    ref = ver_ref.get("ref")
                    if ref and ref in versions:
                        version = str(versions[ref])

                if dep_name not in seen:
                    seen.add(dep_name)
                    deps.append(
                        Dependency(
                            name=dep_name,
                            version=version,
                            source="libs.versions.toml",
                            direct=True,
                            category="runtime",
                        )
                    )

    def _parse_gradle_deps(
        self, content: str, source: str, deps: list[Dependency], seen: set[str],
        vars_map: dict[str, str] | None = None,
    ) -> None:
        """Parse dependency declarations from Gradle content.

        Handles Groovy and Kotlin DSL patterns:
            implementation 'group:artifact:version'
            implementation("group:artifact:version")
            testImplementation 'group:artifact:version'
            implementation group: 'x', name: 'y', version: 'z'  (map notation)

        Version strings containing Gradle variable references (${varName}) are
        resolved against vars_map when provided.
        """
        if vars_map is None:
            vars_map = {}

        # String notation: implementation 'group:artifact:version'
        # Negative lookbehind prevents matching config names embedded in artifact names
        # (e.g. "junit-api" must not be parsed as the "api" configuration keyword).
        string_pattern = rf"""(?<![a-zA-Z0-9_\-])({_ALL_CONFIGS})\s*[\(]?\s*["']([^"']+)["']"""
        for m in re.finditer(string_pattern, content):
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

        # Map notation: implementation group: 'x', name: 'y', version: 'z'
        map_pattern = re.compile(
            rf"""(?<![a-zA-Z0-9_\-])({_ALL_CONFIGS})\s+group\s*:\s*['"]([^'"]+)['"]\s*,\s*name\s*:\s*['"]([^'"]+)['"]"""
            rf"""(?:\s*,\s*version\s*:\s*['"]([^'"]+)['"])?""",
        )
        for m in map_pattern.finditer(content):
            config_name = m.group(1)
            group = m.group(2)
            artifact = m.group(3)
            raw_version = m.group(4)
            name = f"{group}:{artifact}"
            if name not in seen:
                seen.add(name)
                category = _DEP_CONFIG_CATEGORY.get(config_name)
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

        # Build a single repo-wide index of all Java files (stem → list[Path]) so that
        # _extract_endpoints_from_implemented_interfaces can resolve interface names without
        # triggering a separate rglob per interface name.
        java_file_index: dict[str, list[Path]] = {}
        controller_files: list[Path] = []
        for java_file in root.rglob("*.java"):
            if any(part in _EXCLUDED_DIRS for part in java_file.parts):
                continue
            # Add to the flat index (multiple files may share a stem in different packages)
            java_file_index.setdefault(java_file.stem, []).append(java_file)
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

            # API interface pattern: controller has @RestController but no @*Mapping methods
            # (only @Override). Look for `implements XxxApi` and extract endpoints from the
            # interface file instead (e.g. loyalty-microservice pattern).
            if not file_endpoints and "@RestController" in content:
                interface_endpoints = self._extract_endpoints_from_implemented_interfaces(
                    content, controller_file, java_file_index
                )
                file_endpoints = interface_endpoints

            endpoints.extend(file_endpoints)

        # Deduplicate by (method, path) — keep the first occurrence (retains summary/tags)
        # Also filter Spring Actuator management endpoints (/actuator/*) — these are
        # infrastructure endpoints, not public API surface.
        seen_ep: set[tuple[str | None, str | None]] = set()
        deduped: list[EndpointIndex] = []
        for ep in endpoints:
            if ep.path and ep.path.startswith("/actuator"):
                continue
            key = (ep.method, ep.path)
            if key not in seen_ep:
                seen_ep.add(key)
                deduped.append(ep)

        return deduped

    def _extract_endpoints_from_controller(
        self, content: str, source_file: Path
    ) -> list[EndpointIndex]:
        """Extract endpoints from a single controller file.

        Strategy: scan for @*Mapping annotations. For each, search two windows
        for @Operation summary and @Tag:

        1. Pre-window: from the end of the previous @*Mapping (or a lookback limit)
           to the start of this @*Mapping.  This is the common case where annotations
           appear *above* the mapping decorator in the source order:
               @Operation(summary = "...")
               @GetMapping("/path")

        2. Post-window: from the end of this @*Mapping annotation to the start of
           the next @*Mapping (bounded to a reasonable size).  This handles the less
           common but valid order where annotations appear *after* the mapping decorator:
               @DeleteMapping("/path")
               @Operation(summary = "...")
               public ResponseEntity<...> method() { ... }

        The pre-window result takes priority; the post-window is used only when the
        pre-window yields no summary/tag.
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

            # --- Build the annotation cluster windows for this mapping ---
            #
            # Annotations may appear either BEFORE or AFTER the @*Mapping decorator.
            # Both patterns are valid Spring Boot code:
            #
            #   Pattern A (annotations before):      Pattern B (annotations after):
            #     @Operation(summary = "...")           @DeleteMapping("/path")
            #     @GetMapping("/path")                  @Operation(summary = "...")
            #     public ResponseEntity<...> m() {}     public ResponseEntity<...> m() {}
            #
            # Strategy: use the first `{` AFTER the @*Mapping as the boundary of the
            # post-annotation cluster.  Everything between the mapping and the first `{`
            # belongs to THIS method's annotation block; everything after `{` is the
            # method body (followed by the next method's annotations).
            #
            # Similarly, trim the pre-window at the LAST `}` (closing of the previous
            # method body) so we don't pick up annotations from the previous method's
            # post-mapping cluster.

            # Post-window: from the end of the @*Mapping annotation's full argument list
            # (including parentheses) to the first `{` that opens the method body.
            # Bound to 600 chars to avoid pathological cases.
            next_start = (
                all_matches[i + 1].start()
                if i + 1 < len(all_matches)
                else len(content)
            )
            # _extract_mapping_path already locates the closing paren of the annotation.
            # We need to find the character position just after the annotation's closing
            # paren to avoid treating path-parameter braces (e.g. {userId}) as the
            # method-body opening brace.
            # Strategy: scan forward from method_match.end() for the closing ')' of the
            # annotation arguments, then look for the first '{' that is NOT inside a
            # string literal or path template (we use brace-depth counting).
            ann_args_end = method_match.end()
            # Skip optional whitespace, then check if there is an opening paren
            pos = ann_args_end
            while pos < len(content) and content[pos] in " \t":
                pos += 1
            if pos < len(content) and content[pos] == "(":
                depth = 0
                for j in range(pos, min(pos + 400, len(content))):
                    if content[j] == "(":
                        depth += 1
                    elif content[j] == ")":
                        depth -= 1
                        if depth == 0:
                            ann_args_end = j + 1
                            break

            search_end = min(ann_args_end + 600, next_start)
            segment_after = content[ann_args_end : search_end]

            # Find the first '{' that is a method-body open brace, not a path parameter.
            # The method signature contains () for params; the body opens with '{'.
            # We detect the method body brace by finding the first '{' that comes AFTER
            # closing ')' of the method signature.
            method_sig_close = segment_after.find(")")
            if method_sig_close >= 0:
                body_candidate = segment_after.find("{", method_sig_close)
            else:
                body_candidate = segment_after.find("{")

            if body_candidate >= 0:
                post_window = segment_after[:body_candidate]
            else:
                post_window = segment_after

            # Pre-window: from the end of the previous @*Mapping (or lookback limit)
            # to the start of this @*Mapping, trimmed at the last `}` (previous method
            # body close) to exclude annotations that belong to the previous method.
            if i == 0:
                window_start = max(0, method_match.start() - 300)
            else:
                prev_match = all_matches[i - 1]
                window_start = prev_match.end()

            pre_window_raw = content[window_start : method_match.start()]
            last_brace = pre_window_raw.rfind("}")
            if last_brace >= 0:
                pre_window = pre_window_raw[last_brace + 1:]
            else:
                pre_window = pre_window_raw

            # Search pre-window first (most common pattern), then post-window as fallback.
            summary = self._extract_operation_summary(pre_window)
            method_tag = self._extract_tag_name(pre_window, scope="method")

            if summary is None:
                summary = self._extract_operation_summary(post_window)
            if method_tag is None:
                method_tag = self._extract_tag_name(post_window, scope="method")

            tag = method_tag or class_tag

            # Combine base path + method path
            full_path = self._combine_paths(class_base_path, method_path)

            # Extract method signature for parameter/body/response parsing
            method_sig = self._extract_method_signature(post_window)
            parameters = (
                self._extract_parameters_from_signature(method_sig)
                if method_sig else []
            )
            request_body = (
                self._extract_request_body_from_signature(method_sig)
                if method_sig else None
            )
            response = (
                self._extract_return_type_from_signature(method_sig)
                if method_sig else None
            )

            endpoint = EndpointIndex(
                method=http_verb,
                path=full_path,
                summary=summary,
                tags=[tag] if tag else [],
                parameters=parameters,
                request_body=request_body,
                response=response,
            )
            endpoints.append(endpoint)

        return endpoints

    def _extract_endpoints_from_implemented_interfaces(
        self,
        controller_content: str,
        controller_file: Path,
        java_file_index: dict[str, list[Path]],
    ) -> list[EndpointIndex]:
        """Extract endpoints from API interfaces implemented by a @RestController.

        Some projects follow a pattern where route annotations (@RequestMapping,
        @GetMapping, etc.) are placed on an interface (e.g. ``RedeemRewardsApi``)
        and the controller only has ``@RestController`` + ``@Override`` methods.
        In this case, the interface file holds all endpoint metadata.

        Strategy:
        1. Parse ``implements Foo, Bar`` from the controller class declaration.
        2. Look up each interface name in ``java_file_index`` (pre-built by the
           caller — avoids a separate rglob per interface name).
        3. Extract endpoints from each matching interface file that contains HTTP
           mapping annotations.
        """
        endpoints: list[EndpointIndex] = []

        # Find all interface names from `implements` clause.
        # Use re.DOTALL so the pattern matches across line breaks (e.g. multi-line
        # implements clauses) and (?=\s*\{) anchors to the opening brace.
        impl_m = re.search(
            r"\bclass\s+\w+(?:\s+extends\s+\w+)?\s+implements\s+([\w,\s<>]+?)(?=\s*\{)",
            controller_content,
            re.DOTALL,
        )
        if not impl_m:
            return endpoints

        # Extract individual interface names (strip generics and whitespace)
        raw_interfaces = impl_m.group(1)
        interface_names = [
            re.sub(r"<[^>]*>", "", name).strip()
            for name in raw_interfaces.split(",")
            if name.strip()
        ]

        http_annotation_re = re.compile(
            r"@(?:RequestMapping|GetMapping|PostMapping|PutMapping|DeleteMapping|PatchMapping)\b"
        )

        for iface_name in interface_names:
            if not iface_name:
                continue
            # Look up candidates in the pre-built index (O(1) instead of rglob)
            candidates = java_file_index.get(iface_name, [])
            for candidate in candidates:
                try:
                    iface_content = candidate.read_text(errors="replace")
                except OSError:
                    continue
                # Only process if the interface has HTTP mapping annotations
                if not http_annotation_re.search(iface_content):
                    continue
                iface_endpoints = self._extract_endpoints_from_controller(iface_content, candidate)
                endpoints.extend(iface_endpoints)
                break  # Use the first matching file per interface name

        return endpoints

    def _extract_request_mapping_path(self, content: str, scope: str = "class") -> str | None:
        """Extract the path from @RequestMapping annotation.

        For class scope: looks for @RequestMapping before the class declaration.
        For method scope: looks for the nearest @RequestMapping.
        """
        # Match @RequestMapping("/path") or @RequestMapping(value = "/path")
        # Handle both single and double quotes, and optional 'value ='
        pattern = re.compile(
            r'@RequestMapping\s*\(\s*(?:(?:value|path)\s*=\s*)?["\']([^"\']+)["\']',
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
        """Extract @Operation(summary = "...") or @Operation(description = "...") from text.

        Tries ``summary`` first; falls back to ``description`` if summary is absent.

        Discards values that look like URL paths — these are copy-paste noise
        from codebases that use the summary/description field as a route reference
        instead of a human-readable description. Only returns genuine descriptions.

        Patterns that are discarded:
        - Absolute paths: "/accounts/redeem", "/shopping-cart/tickets"
        - Version-prefixed paths: "v1/accounts/redeem", "v2/accounts/seats"
        - Resource-path fragments: "tm-events/active", "games/{gameId}/confirm"
        - Path parameter fragments: "{tmId}/lite", "{gameId}/guest"
        - Single-segment paths: "/status", "/health", "/availability"
        - Query-string URLs: "/accounts/validate?email=..."
        """
        # Try summary first, then fall back to description
        raw = self._try_extract_op_attr(text, "summary") or self._try_extract_op_attr(
            text, "description"
        )
        if not raw:
            return None

        summary = raw.strip()
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

    def _try_extract_op_attr(self, text: str, attr: str) -> str | None:
        """Extract a specific attribute from @Operation(...) in text.

        Handles both @Operation(attr = "value") and multi-attribute forms like
        @Operation(summary = "x", description = "y").
        """
        m = re.search(
            rf'@Operation\s*\(\s*(?:[^)]*?\s)?{re.escape(attr)}\s*=\s*["\']([^"\']+)["\']',
            text,
            re.DOTALL,
        )
        if m:
            return m.group(1)
        return None

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

    def _extract_method_signature(self, text: str) -> str | None:
        """Extract Java method signature from text following a @*Mapping annotation.

        Returns the signature from return type through closing ')' of parameter list,
        or None if no valid signature is found.
        """
        # Strategy: scan for the first '(' that's part of a method declaration (not an annotation)
        # Then find the matching ')' using depth counting

        # Find method declaration: look for pattern like "public Type method(" or "Type method("
        # Skip annotation lines
        m = re.search(
            r'(?:(?:public|protected|private|default)\s+)?'  # optional access modifier
            r'(?:static\s+)?'  # optional static
            r'(?:final\s+)?'  # optional final
            r'(?:synchronized\s+)?'  # optional synchronized
            r'((?:[\w\.<>,\?\[\]\s]|(?:extends\s)|(?:super\s))+?)\s+'  # return type
            r'(\w+)\s*\(',  # method name + opening paren
            text,
            re.DOTALL,
        )
        if not m:
            return None

        # Get the full match start (including modifiers)
        paren_start = m.end() - 1  # position of '('

        # Find matching ')' using depth counting
        depth = 0
        for i in range(paren_start, len(text)):
            if text[i] == '(':
                depth += 1
            elif text[i] == ')':
                depth -= 1
                if depth == 0:
                    # Return from return type through ')'
                    return text[m.start(1):i + 1].strip()

        return None

    def _split_params_respecting_generics(self, param_str: str) -> list[str]:
        """Split comma-separated params respecting <> generic depth and () annotation depth."""
        params: list[str] = []
        angle_depth = 0
        paren_depth = 0
        in_string = False
        current: list[str] = []
        for i, ch in enumerate(param_str):
            # Track string literals to avoid counting brackets inside strings
            if ch == '"' and (i == 0 or param_str[i - 1] != '\\'):
                in_string = not in_string
                current.append(ch)
            elif in_string:
                current.append(ch)
            elif ch == '<':
                angle_depth += 1
                current.append(ch)
            elif ch == '>':
                angle_depth -= 1
                current.append(ch)
            elif ch == '(':
                paren_depth += 1
                current.append(ch)
            elif ch == ')':
                paren_depth -= 1
                current.append(ch)
            elif ch == ',' and angle_depth == 0 and paren_depth == 0:
                token = ''.join(current).strip()
                if token:
                    params.append(token)
                current = []
            else:
                current.append(ch)
        token = ''.join(current).strip()
        if token:
            params.append(token)
        return params

    def _extract_parameters_from_signature(self, signature: str) -> list[EndpointParameter]:
        """Extract request parameters from Spring annotations in a method signature."""
        # Extract just the parameter list (between parens)
        paren_start = signature.find('(')
        paren_end = signature.rfind(')')
        if paren_start < 0 or paren_end < 0:
            return []

        param_str = signature[paren_start + 1:paren_end].strip()
        if not param_str:
            return []

        params = self._split_params_respecting_generics(param_str)
        result: list[EndpointParameter] = []

        # Annotation pattern for @RequestParam, @PathVariable, @RequestHeader
        annotation_re = re.compile(
            r'@(RequestParam|PathVariable|RequestHeader)\b'
        )

        for param_token in params:
            param_token = param_token.strip()
            # Normalize whitespace (multi-line signatures)
            param_token = re.sub(r'\s+', ' ', param_token)

            m = annotation_re.search(param_token)
            if not m:
                continue  # Skip unannotated params (framework-injected)

            ann_type = m.group(1)
            location_map = {
                'RequestParam': 'query',
                'PathVariable': 'path',
                'RequestHeader': 'header',
            }
            location = location_map[ann_type]

            # Extract annotation attributes
            ann_end = m.end()

            # Check for annotation arguments: @RequestParam(...) or @RequestParam
            attr_name: str | None = None
            attr_required: bool | None = None
            attr_default: str | None = None

            rest_after_ann = param_token[ann_end:]
            # Check if there are parenthesized attributes
            paren_m = re.match(r'\s*\(([^)]*)\)', rest_after_ann)
            if paren_m:
                attrs_str = paren_m.group(1).strip()
                # Check for named attributes: value="x", name="x",
                # required=false, defaultValue="x"
                attr_pat = r'(\w+)\s*=\s*(?:"([^"]*)"|\'([^\']*)\'|(\w+))'
                for attr_m in re.finditer(attr_pat, attrs_str):
                    attr_key = attr_m.group(1)
                    attr_val = attr_m.group(2) or attr_m.group(3) or attr_m.group(4)
                    if attr_key in ('value', 'name'):
                        attr_name = attr_val
                    elif attr_key == 'required':
                        attr_required = attr_val.lower() == 'true'
                    elif attr_key == 'defaultValue':
                        attr_default = attr_val

                # Check for positional string: @RequestParam("name")
                if attr_name is None:
                    pos_m = re.match(r'\s*"([^"]*)"', attrs_str)
                    if not pos_m:
                        pos_m = re.match(r"\s*'([^']*)'", attrs_str)
                    if pos_m:
                        attr_name = pos_m.group(1)

                # Remove the annotation + attrs from the token to get type + name
                remaining = param_token[:m.start()] + rest_after_ann[paren_m.end():]
            else:
                remaining = param_token[:m.start()] + rest_after_ann

            # Also remove any other annotations (like @Valid, @NotNull, etc.)
            remaining = re.sub(r'@\w+(?:\([^)]*\))?\s*', '', remaining).strip()

            # Parse remaining as "Type paramName" or "Type... paramName"
            remaining_tokens = remaining.split()
            java_type: str | None = None
            java_name: str | None = None

            if len(remaining_tokens) >= 2:
                java_name = remaining_tokens[-1]
                java_type = ' '.join(remaining_tokens[:-1])
                # Clean up varargs
                if java_type.endswith('...'):
                    java_type = java_type[:-3].strip() + '[]'
            elif len(remaining_tokens) == 1:
                java_name = remaining_tokens[0]

            # Use annotation name if specified, otherwise Java param name
            param_name = attr_name or java_name or 'unknown'

            result.append(EndpointParameter(
                name=param_name,
                location=location,
                type=java_type,
                required=attr_required,
                default_value=attr_default,
            ))

        return result

    def _extract_request_body_from_signature(self, signature: str) -> EndpointRequestBody | None:
        """Extract @RequestBody type from a method signature."""
        paren_start = signature.find('(')
        paren_end = signature.rfind(')')
        if paren_start < 0 or paren_end < 0:
            return None

        param_str = signature[paren_start + 1:paren_end].strip()
        if not param_str:
            return None

        params = self._split_params_respecting_generics(param_str)

        for param_token in params:
            param_token = re.sub(r'\s+', ' ', param_token.strip())

            m = re.search(r'@RequestBody\b', param_token)
            if not m:
                continue

            # Check for required attribute
            required = True
            rest = param_token[m.end():]
            paren_m = re.match(r'\s*\(([^)]*)\)', rest)
            if paren_m:
                attrs_str = paren_m.group(1)
                req_m = re.search(r'required\s*=\s*(\w+)', attrs_str)
                if req_m:
                    required = req_m.group(1).lower() != 'false'
                rest = rest[paren_m.end():]

            # Remove other annotations
            remaining = param_token[:m.start()] + rest
            remaining = re.sub(r'@\w+(?:\([^)]*\))?\s*', '', remaining).strip()

            # Parse "Type paramName"
            tokens = remaining.split()
            if len(tokens) >= 2:
                # Type is everything except the last token (param name)
                body_type = ' '.join(tokens[:-1])
                return EndpointRequestBody(type=body_type, required=required)
            elif len(tokens) == 1:
                return EndpointRequestBody(type=tokens[0], required=required)

        return None

    def _extract_return_type_from_signature(self, signature: str) -> EndpointResponse | None:
        """Extract return type from a method signature and unwrap common wrappers."""
        # The signature looks like: "ReturnType methodName(params)"
        # Find the method name + opening paren
        paren_pos = signature.find('(')
        if paren_pos < 0:
            return None

        before_paren = signature[:paren_pos].strip()
        # Remove access modifiers
        before_paren = re.sub(
            r'\b(?:public|protected|private|default|static|final|synchronized|abstract)\b\s*',
            '', before_paren
        ).strip()

        # Now we have "ReturnType methodName"
        # Split off the method name (last token, but must handle generics in return type)
        # Find the last whitespace that's not inside <>
        depth = 0
        last_space = -1
        for i, ch in enumerate(before_paren):
            if ch == '<':
                depth += 1
            elif ch == '>':
                depth -= 1
            elif ch == ' ' and depth == 0:
                last_space = i

        if last_space < 0:
            return None  # Can't separate return type from method name

        return_type = before_paren[:last_space].strip()
        if not return_type or return_type == 'void':
            return EndpointResponse(type='void', wrapper=None) if return_type == 'void' else None

        # Check if outer type is a known wrapper
        wrapper, inner = self._unwrap_response_type(return_type)

        return EndpointResponse(type=inner, wrapper=wrapper)

    def _unwrap_response_type(self, type_str: str) -> tuple[str | None, str]:
        """Unwrap response wrapper types like ResponseEntity<T>, Mono<T>, etc.

        Returns (wrapper, inner_type). If no wrapper, returns (None, type_str).
        Handles nested wrappers like Mono<ResponseEntity<T>> → ("Mono<ResponseEntity>", "T").
        """
        # Find the outer type name
        angle_pos = type_str.find('<')
        if angle_pos < 0:
            # No generics — check if it's a wrapper type itself (e.g., just "ResponseEntity")
            if type_str in _RESPONSE_WRAPPERS:
                return (type_str, "?")
            return (None, type_str)

        outer = type_str[:angle_pos].strip()

        if outer not in _RESPONSE_WRAPPERS:
            return (None, type_str)

        # Extract inner type (between first < and last >)
        # Use depth counting for correctness
        depth = 0
        inner_start = angle_pos + 1
        inner_end = len(type_str)
        for i in range(angle_pos, len(type_str)):
            if type_str[i] == '<':
                depth += 1
            elif type_str[i] == '>':
                depth -= 1
                if depth == 0:
                    inner_end = i
                    break

        inner = type_str[inner_start:inner_end].strip()

        # Check for nested wrapper: Mono<ResponseEntity<T>>
        nested_angle = inner.find('<')
        if nested_angle >= 0:
            nested_outer = inner[:nested_angle].strip()
            if nested_outer in _RESPONSE_WRAPPERS:
                # Recursively unwrap
                nested_wrapper, nested_inner = self._unwrap_response_type(inner)
                # Combine wrappers: Mono<ResponseEntity>
                combined_wrapper = f"{outer}<{nested_outer}>"
                return (combined_wrapper, nested_inner)

        return (outer, inner)

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
            has_kafka_registry = (
                "KafkaListenerEndpointRegistry" in content
                or "MethodKafkaListenerEndpoint" in content
            )
            if has_kafka_registry:
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

    def _scan_java_for_static_string_constants(self, root: Path) -> dict[str, str]:
        """Scan Java source files for static final String constant declarations.

        Returns a dict mapping qualified name (ClassName.CONSTANT or just CONSTANT) → value.
        Used to resolve references like KafkaTopics.ORDER_CREATED in producer/consumer code.
        Skips test directories and excluded directories.
        """
        constants: dict[str, str] = {}
        for java_file in root.rglob("*.java"):
            parts = java_file.parts
            if any(part in _EXCLUDED_DIRS for part in parts):
                continue
            if any(part in _TEST_DIR_SEGMENTS for part in parts):
                continue
            try:
                content = java_file.read_text(errors="replace")
            except OSError:
                continue

            # Match: public static final String SOME_CONST = "value";
            for m in re.finditer(
                r"""public\s+static\s+final\s+String\s+(\w+)\s*=\s*["']([^"']+)["']""",
                content,
            ):
                const_name = m.group(1)
                const_value = m.group(2)
                constants[const_name] = const_value

                # Also try to extract class name for qualified lookup
                class_m = re.search(
                    r"(?:public\s+)?(?:final\s+)?class\s+(\w+)", content, re.MULTILINE
                )
                if class_m:
                    qualified = f"{class_m.group(1)}.{const_name}"
                    constants[qualified] = const_value

        return constants

    def _resolve_kafka_topic_ref(
        self, raw: str, constants: dict[str, str], yaml_props: dict[str, str]
    ) -> str | None:
        """Resolve a raw topic reference to an actual topic name.

        Handles:
        - String literals: "topic-name" → "topic-name"
        - Spring EL: "${kafka.topics.order-created}" → resolved via yaml_props
        - Constant refs: "KafkaTopics.ORDER_SHIPPED" → resolved via constants map
        - Plain env var: "${MY_TOPIC}" → returned as "MY_TOPIC" (no default)
        """
        raw = raw.strip().strip('"').strip("'")
        if not raw:
            return None

        # Spring EL expression: resolve via YAML or existing resolver
        if raw.startswith("${"):
            inner_m = re.fullmatch(r"\$\{([^}]+)\}", raw.strip())
            if inner_m:
                inner = inner_m.group(1)
                prop_key, _, default_value = inner.partition(":")

                if default_value:
                    # ${KEY:default} — use the embedded default directly
                    return default_value

                # ${KEY} with no default — try to resolve via flat YAML props first
                if prop_key in yaml_props:
                    yaml_val = yaml_props[prop_key]
                    # YAML value may itself be a Spring EL expression (e.g. ${ENV:default})
                    # Resolve it a second time to extract the concrete topic name.
                    if yaml_val.startswith("${"):
                        return self._resolve_spring_el_topic(yaml_val)
                    return yaml_val

                # Fall back to _resolve_spring_el_topic which returns the env var name
                return self._resolve_spring_el_topic(raw)

            # Malformed EL expression — fall through to plain string handling
            return self._resolve_spring_el_topic(raw)

        # Constant reference (e.g., KafkaTopics.ORDER_SHIPPED or ORDER_SHIPPED)
        # Also handles @Value-injected field names (camelCase identifiers)
        if raw in constants:
            return constants[raw]
        if "." in raw or raw.isupper():
            # Try the last segment (e.g., MyClass.CONSTANT → look up CONSTANT)
            last = raw.split(".")[-1]
            if last in constants:
                return constants[last]

        # Plain string literal — return as-is if it looks like a topic name
        # (contains hyphens/dots typical of Kafka topic naming conventions)
        if re.match(r"^[a-zA-Z0-9._\-]+$", raw) and len(raw) > 3:
            # Reject plain camelCase identifiers that are likely variable names,
            # not actual topic strings. Topic names typically contain hyphens or dots.
            # Only allow if it has a separator or is UPPER_SNAKE_CASE.
            if "-" in raw or "." in raw or "_" in raw or raw.isupper():
                return raw

        return None

    def _scan_value_injected_fields(self, root: Path) -> dict[str, str]:
        """Scan Java source for @Value-annotated String fields and resolve them.

        Handles patterns like:
            @Value("${kafka.topics.order-created:order-created-topic}")
            private String topicName;

        Returns a dict mapping field name → resolved topic/value string.
        Uses yaml_props for resolution when no default is present.
        """
        fields: dict[str, str] = {}
        yaml_props = self._load_yaml_flat_props(root)

        value_field_pattern = re.compile(
            r'@Value\s*\(\s*["\']\$\{([^}]+)\}["\']\s*\)\s*'
            r'(?:private\s+|protected\s+|public\s+)?'
            r'(?:final\s+)?String\s+(\w+)\s*;',
            re.DOTALL,
        )

        for java_file in root.rglob("*.java"):
            if any(part in _EXCLUDED_DIRS for part in java_file.parts):
                continue
            if any(part in _TEST_DIR_SEGMENTS for part in java_file.parts):
                continue
            try:
                content = java_file.read_text(errors="replace")
            except OSError:
                continue
            if "@Value" not in content:
                continue

            for m in value_field_pattern.finditer(content):
                el_expr = m.group(1)   # e.g. "kafka.topics.order-created:order-created-topic"
                field_name = m.group(2)
                prop_key, _, default_val = el_expr.partition(":")
                if default_val:
                    fields[field_name] = default_val
                elif prop_key in yaml_props:
                    yaml_val = yaml_props[prop_key]
                    # YAML value may itself be a Spring EL expression (e.g. ${ENV:default})
                    # Resolve it to extract the concrete value.
                    if yaml_val.startswith("${"):
                        yaml_val = self._resolve_spring_el_topic(yaml_val)
                    fields[field_name] = yaml_val
                # If no default and not in YAML, skip — unresolvable at static analysis time

        return fields

    # Regex to identify Java constant names (ALL_CAPS_WITH_UNDERSCORES)
    # These are unresolved constant refs that should NOT be emitted as topic names.
    _JAVA_CONSTANT_NAME_RE = re.compile(r'^[A-Z][A-Z0-9_]*$')

    def _is_java_constant_name(self, value: str) -> bool:
        """Return True if value looks like an unresolved Java constant (e.g. TOPIC_NAME)."""
        return bool(self._JAVA_CONSTANT_NAME_RE.match(value)) and len(value) > 2

    def _scan_value_injected_fields_in_file(
        self, content: str, yaml_props: dict[str, str]
    ) -> dict[str, str]:
        """Extract @Value-injected String field declarations from a single file's content.

        Returns a dict mapping field name → resolved value for that file only.
        Unlike _scan_value_injected_fields (which scans the whole codebase into one
        dict and loses entries when multiple files share the same field name), this
        per-file variant is used to resolve kafkaTemplate.send(fieldName, ...) against
        the @Value declarations in the same class — matching real Spring injection scope.
        """
        fields: dict[str, str] = {}
        value_field_pattern = re.compile(
            r'@Value\s*\(\s*["\']\$\{([^}]+)\}["\']\s*\)\s*'
            r'(?:private\s+|protected\s+|public\s+)?'
            r'(?:final\s+)?String\s+(\w+)\s*;',
            re.DOTALL,
        )
        for m in value_field_pattern.finditer(content):
            el_expr = m.group(1)
            field_name = m.group(2)
            prop_key, _, default_val = el_expr.partition(":")
            if default_val:
                fields[field_name] = default_val
            elif prop_key in yaml_props:
                yaml_val = yaml_props[prop_key]
                if yaml_val.startswith("${"):
                    yaml_val = self._resolve_spring_el_topic(yaml_val)
                fields[field_name] = yaml_val
        return fields

    def _parse_kafka_producers(self, root: Path) -> list[str]:
        """Scan Java source and application.yml for Kafka producer patterns.

        Detects:
        1. kafkaTemplate.send("topic", ...) — string literal topic
        2. kafkaTemplate.send(Constants.TOPIC, ...) — constant reference
        3. kafkaTemplate.send(topicField, ...) — @Value-injected String field
        4. new ProducerRecord<>(topicField, ...) where topicField is @Value-injected
        5. @SendTo("topic") — reply topics on @KafkaListener methods

        For pattern 3, per-file @Value resolution is used first so that multiple
        publisher classes sharing the same field name (e.g. `topic`) are each resolved
        against their own @Value declaration, not a codebase-wide last-write-wins dict.

        Fallback: When kafkaTemplate.send() uses a method parameter that cannot be
        resolved at the call site (common in publisher service classes), collect all
        @Value-injected kafka/topic fields from the entire codebase and emit them.
        This handles the pattern where a use-case/aspect injects the topic via @Value
        and passes it as a String argument to a shared publisher method.

        Skips kafkaTemplate.send(producerRecord, ...) where the first arg is a
        ProducerRecord variable (detected by camelCase without dots/quotes and the
        variable name matching a local ProducerRecord construction in the same file).
        """
        produces: list[str] = []
        seen: set[str] = set()

        # Build resolution helpers
        constants = self._scan_java_for_static_string_constants(root)
        yaml_props = self._load_yaml_flat_props(root)
        value_fields = self._scan_value_injected_fields(root)
        # Merge value_fields into constants for fallback resolution.
        # NOTE: this dict has last-write-wins semantics for field names shared across
        # files (e.g. all publishers using "topic").  Per-file resolution below takes
        # priority for the common single-publisher-per-file pattern.
        merged = {**constants, **value_fields}

        # Pattern: kafkaTemplate.send(<arg>, ...)
        # Argument can be a string literal, a constant ref, or a field/variable name.
        # Also handles the multi-line case where kafkaTemplate and .send() are on
        # separate lines (e.g. kafkaTemplate\n    .send(topic, key, msg)).
        send_pattern = re.compile(
            r"""kafkaTemplate\s*\.\s*send\(\s*(["']([^"']+)["']|\$\{[^}]+\}|[\w.]+)\s*[,)]""",
            re.DOTALL,
        )
        # Pattern to detect ProducerRecord variable names in the file
        # e.g.: ProducerRecord<...> myVar = new ProducerRecord<>(...)
        producer_record_var_pattern = re.compile(
            r'ProducerRecord\s*(?:<[^>]*>)?\s+(\w+)\s*='
        )
        # Pattern for new ProducerRecord<>(topicArg, ...) to extract topic arg
        producer_record_ctor_pattern = re.compile(
            r'new\s+ProducerRecord\s*(?:<[^>]*>)?\s*\(\s*(["\']\S+["\']|[\w.]+)\s*[,)]'
        )
        # Pattern: @SendTo("topic") or @SendTo({"topic1", "topic2"})
        sendto_pattern = re.compile(
            r"""@SendTo\(\s*(?:\{)?["']([^"']+)["']""",
        )
        # @Value pattern to detect kafka/topic-related injected fields
        # e.g.: @Value("${spring.kafka.topic.my-topic}") private String myTopicField;
        kafka_value_field_pattern = re.compile(
            r'@Value\s*\(\s*["\'](\$\{[^}]*(?:kafka|topic|TOPIC)[^}]*\})["\']',
            re.IGNORECASE,
        )

        # Track files that contain kafkaTemplate.send() but have unresolvable topic args
        # (method parameters that aren't fields/constants in the same file)
        files_with_unresolved_sends: list[Path] = []

        for java_file in root.rglob("*.java"):
            parts = java_file.parts
            if any(part in _EXCLUDED_DIRS for part in parts):
                continue
            if any(part in _TEST_DIR_SEGMENTS for part in parts):
                continue
            try:
                content = java_file.read_text(errors="replace")
            except OSError:
                continue

            if "kafkaTemplate" not in content and "@SendTo" not in content:
                continue

            # Build a per-file @Value field map so that multiple publisher classes
            # using the same field name (e.g. `topic`) each resolve to their own topic.
            per_file_value_fields = self._scan_value_injected_fields_in_file(
                content, yaml_props
            )
            # Per-file resolution takes priority over the codebase-wide merged dict.
            file_merged = {**merged, **per_file_value_fields}

            # Collect names of ProducerRecord variables in this file so we can skip them
            # when they appear as the first arg to kafkaTemplate.send()
            producer_record_vars = set(producer_record_var_pattern.findall(content))

            # Extract topics from new ProducerRecord<>(topicArg, ...) ctors
            for m in producer_record_ctor_pattern.finditer(content):
                raw = m.group(1)
                resolved = self._resolve_kafka_topic_ref(raw, file_merged, yaml_props)
                if resolved and not self._is_java_constant_name(resolved) and resolved not in seen:
                    seen.add(resolved)
                    produces.append(resolved)

            had_unresolved = False
            for m in send_pattern.finditer(content):
                raw = m.group(1)
                # Skip ProducerRecord variables — topic already extracted from ctor above
                raw_stripped = raw.strip().strip('"').strip("'")
                if raw_stripped in producer_record_vars:
                    continue

                # Determine if this arg was resolved via per-file @Value injection.
                # When resolved via @Value, the result is a legitimate topic reference
                # even if it looks like an ALL_CAPS env-var name (e.g. PAYMENTS_TOPIC).
                # In that case we skip the _is_java_constant_name filter which would
                # otherwise discard env-var-named topics.
                resolved_from_value_field = (
                    raw_stripped in per_file_value_fields
                    and not raw_stripped.startswith(("'", '"', "${"))
                    and "." not in raw_stripped
                )

                resolved = self._resolve_kafka_topic_ref(raw, file_merged, yaml_props)
                if resolved and resolved not in seen:
                    # Skip unresolved Java constant names UNLESS the value was obtained
                    # directly from a @Value-injected field (where ALL_CAPS means env var).
                    if not resolved_from_value_field and self._is_java_constant_name(resolved):
                        pass  # discard — looks like an unresolved constant name
                    else:
                        seen.add(resolved)
                        produces.append(resolved)
                elif resolved is None:
                    # Topic arg is unresolvable at call site (likely a method parameter)
                    had_unresolved = True

            if had_unresolved:
                files_with_unresolved_sends.append(java_file)

            for m in sendto_pattern.finditer(content):
                topic = m.group(1).strip()
                resolved = self._resolve_kafka_topic_ref(topic, file_merged, yaml_props)
                if resolved and not self._is_java_constant_name(resolved) and resolved not in seen:
                    seen.add(resolved)
                    produces.append(resolved)

        # Fallback: if any files had unresolvable kafkaTemplate.send() topic args,
        # collect all @Value-injected kafka/topic fields from the ENTIRE codebase.
        # This handles the common pattern where a publisher service receives the topic
        # as a method parameter that was @Value-injected in a use-case/aspect class.
        if files_with_unresolved_sends:
            for java_file in root.rglob("*.java"):
                parts = java_file.parts
                if any(part in _EXCLUDED_DIRS for part in parts):
                    continue
                if any(part in _TEST_DIR_SEGMENTS for part in parts):
                    continue
                try:
                    content = java_file.read_text(errors="replace")
                except OSError:
                    continue
                if "@Value" not in content:
                    continue
                for m in kafka_value_field_pattern.finditer(content):
                    el_expr = m.group(1)  # e.g. "${spring.kafka.topic.locations-config-changed}"
                    resolved = self._resolve_kafka_topic_ref(el_expr, merged, yaml_props)
                    is_valid = (
                        resolved
                        and not self._is_java_constant_name(resolved)
                        and resolved not in seen
                    )
                    if is_valid:
                        seen.add(resolved)
                        produces.append(resolved)

        return produces

    def _parse_kafka_consumers(self, root: Path) -> list[str]:
        """Scan Java source files for @KafkaListener(topics=...) annotations.

        Detects:
        1. @KafkaListener(topics = "topic-name") — string literal
        2. @KafkaListener(topics = "${kafka.topics.order-created}") — Spring EL
        3. @KafkaListener(topics = {TopicConstants.A, TopicConstants.B}) — constant array
        4. @KafkaListener(topics = {"topic1", "topic2"}) — string array
        """
        consumes: list[str] = []
        seen: set[str] = set()

        constants = self._scan_java_for_static_string_constants(root)
        yaml_props = self._load_yaml_flat_props(root)

        # Match topics=... in @KafkaListener
        # Handles: topics = "x", topics = {"x","y"}, topics = ${...}, topics = Const.TOPIC
        topics_pattern = re.compile(
            r"""@KafkaListener\s*\([^)]*topics\s*=\s*(\{[^}]+\}|["'][^"']+["']|\$\{[^}]+\}|[\w.]+)""",
            re.DOTALL,
        )

        for java_file in root.rglob("*.java"):
            parts = java_file.parts
            if any(part in _EXCLUDED_DIRS for part in parts):
                continue
            if any(part in _TEST_DIR_SEGMENTS for part in parts):
                continue
            try:
                content = java_file.read_text(errors="replace")
            except OSError:
                continue

            if "@KafkaListener" not in content:
                continue

            for m in topics_pattern.finditer(content):
                topics_arg = m.group(1).strip()
                # Could be an array: {"topic1", TopicConst.TOPIC2}
                if topics_arg.startswith("{"):
                    inner = topics_arg[1:-1]
                    # Split on commas (that aren't inside strings)
                    for part in re.split(r",\s*", inner):
                        part = part.strip()
                        resolved = self._resolve_kafka_topic_ref(part, constants, yaml_props)
                        if resolved and resolved not in seen:
                            seen.add(resolved)
                            consumes.append(resolved)
                else:
                    resolved = self._resolve_kafka_topic_ref(topics_arg, constants, yaml_props)
                    if resolved and resolved not in seen:
                        seen.add(resolved)
                        consumes.append(resolved)

        return consumes

    def _load_yaml_flat_props(self, root: Path) -> dict[str, str]:
        """Load application.yml as a flat dict of dot-notation key → string value.

        Used for resolving ${spring.el.property} references in Java source.
        E.g., "kafka.topics.order-created" → "demo.orders.created"
        """
        flat: dict[str, str] = {}

        for yml_file in root.rglob("application*.yml"):
            if any(part in _EXCLUDED_DIRS for part in yml_file.parts):
                continue
            try:
                content = yml_file.read_text(errors="replace")
            except OSError:
                continue

            try:
                import yaml

                data = yaml.safe_load(content)
                if isinstance(data, dict):
                    self._flatten_yaml(data, "", flat)
            except Exception:
                pass

        return flat

    def _flatten_yaml(self, data: dict | list | str, prefix: str, flat: dict[str, str]) -> None:
        """Recursively flatten a YAML dict into dot-notation keys."""
        if isinstance(data, dict):
            for key, value in data.items():
                child_key = f"{prefix}.{key}" if prefix else str(key)
                self._flatten_yaml(value, child_key, flat)
        elif isinstance(data, str):
            flat[prefix] = data
        elif isinstance(data, (int, float, bool)):
            flat[prefix] = str(data)

    def _parse_outbound_service_calls(self, root: Path) -> list[OutboundCall]:
        """Scan application.yml and Java source for outbound HTTP service calls.

        Phase A — Config scanning (application.yml):
            Looks for keys matching *.base-url, *.url, *.host, *.endpoint, *.uri
            that contain HTTP URL values, excluding known infrastructure URLs
            (JDBC, Kafka bootstrap, Cosmos endpoints, Redis hosts, Azure App Config).
            Also detects env-var-only Spring EL values (e.g., ``${NBA_BASE_URL:}``)
            and emits outbound calls with ``env:<ENV_VAR>`` as the target URL.

        Phase B — Java source scanning:
            Looks for WebClient.builder().baseUrl() patterns, @Value injection
            (both field-level and method-parameter-level) to confirm and link
            config keys to service calls.

        Phase C — @HttpExchange interface scanning:
            Scans for Spring 6 declarative HTTP client interfaces annotated with
            @HttpExchange and enriches outbound calls with client interface names
            and endpoint paths.

        Returns a list of OutboundCall objects with target_url and config_key set.
        """
        calls: list[OutboundCall] = []
        seen_keys: set[str] = set()

        url_key_pattern = _URL_KEY_PATTERN

        # Excluded URL patterns (infrastructure or non-HTTP targets, not service calls)
        excluded_url_patterns = [
            re.compile(p, re.IGNORECASE)
            for p in [
                r"^jdbc:",
                r"localhost",
                r"kafka\.bootstrap",
                r"bootstrap[_-]servers",
                r"\.documents\.azure\.com",
                r"documents\.azure",
                r"blob[_-]?storage",     # Azure Blob Storage URLs/config keys
                r"\.blob\.core\.windows",
                r"blob\.core",
                r"redis",
                r"appconfig\.azure",
                r"app-configuration",
                r"application-configuration",
                # Custom-scheme URIs (e.g. intuitdome://, myapp://)
                r"^(?!https?://)[a-zA-Z][a-zA-Z0-9+\-.]+://",
            ]
        ]

        # Infrastructure YAML key patterns — keys under these prefixes are
        # infrastructure config, not outbound service integrations
        excluded_key_patterns = [
            re.compile(p, re.IGNORECASE)
            for p in [
                r"^spring\.datasource\b",
                r"^spring\.data\.redis\b",
                r"^spring\.redis\b",
                r"^spring\.cloud\.azure\.cosmos\b",
                r"^spring\.kafka\b",
                r"^spring\.cloud\.azure\.storage\b",
                r"^spring\.cloud\.azure\.appconfiguration\b",
                r"^management\.",
                r"^eureka\.",
                r"^server\.",
                r"^logging\.",
                r"deeplink",             # Mobile deeplink URL config keys (not HTTP services)
            ]
        ]

        def _is_excluded_url(url: str) -> bool:
            return any(p.search(url) for p in excluded_url_patterns)

        def _is_excluded_key(key: str) -> bool:
            return any(p.search(key) for p in excluded_key_patterns)

        # ---- Phase A: Config scanning ----
        flat_props = self._load_yaml_flat_props(root)
        for key, value in flat_props.items():
            if not url_key_pattern.search(key):
                continue

            if _is_excluded_key(key) or _is_excluded_url(key):
                continue

            # Resolve Spring EL expressions like ${ENV_VAR:https://default-url}
            resolved_value = value
            raw_has_spring_el = value.startswith("${")
            if raw_has_spring_el:
                resolved_value = self._resolve_spring_el_topic(value)

            if resolved_value.startswith(("http://", "https://")):
                if _is_excluded_url(resolved_value):
                    continue
                if key not in seen_keys:
                    seen_keys.add(key)
                    calls.append(
                        OutboundCall(
                            target_url=resolved_value,
                            config_key=key,
                            protocol="http",
                        )
                    )
            elif raw_has_spring_el and not _is_excluded_url(resolved_value):
                # Change 1: env-var-only Spring EL value (e.g., "${NBA_BASE_URL:}"
                # resolves to "" or env var name).  Still emit the outbound call
                # with an env: prefixed target so the graph captures connectivity.
                env_var_name = self._extract_env_var_from_spring_el(value)
                if env_var_name and key not in seen_keys:
                    seen_keys.add(key)
                    calls.append(
                        OutboundCall(
                            target_url=f"env:{env_var_name}",
                            config_key=key,
                            protocol="http",
                        )
                    )

        # ---- Phase B: Java source scanning ----
        http_exchange_baseurl_pattern = re.compile(
            r"""\.baseUrl\s*\(\s*(["']([^"']+)["']|\$\{[^}]+\}|[\w.]+)\s*\)""",
        )

        # Change 2: Pattern for @Value on method parameters in @Bean methods.
        # Matches: @Value("${config.key}") String paramName
        # in method signatures (not just field declarations).
        bean_method_value_pattern = re.compile(
            r'@Value\s*\(\s*["\'](\$\{[^}]+\})["\']'
            r'\s*\)\s*String\s+(\w+)',
        )

        for java_file in root.rglob("*.java"):
            parts = java_file.parts
            if any(part in _EXCLUDED_DIRS for part in parts):
                continue
            if any(part in _TEST_DIR_SEGMENTS for part in parts):
                continue
            try:
                content = java_file.read_text(errors="replace")
            except OSError:
                continue

            if "WebClient" not in content and "HttpServiceProxyFactory" not in content:
                continue

            # ---- Phase B.1: .baseUrl() detection (existing logic) ----
            for m in http_exchange_baseurl_pattern.finditer(content):
                raw = m.group(1).strip().strip('"').strip("'")
                if raw.startswith("${"):
                    # Inline Spring EL: .baseUrl("${device-twins.base-url}")
                    inner = re.fullmatch(r"\$\{([^}]+)\}", raw)
                    if inner:
                        prop_key = inner.group(1).split(":")[0]
                        inner_val = inner.group(1)
                        default_url = (
                            inner_val.split(":", 1)[1]
                            if ":" in inner_val
                            else None
                        )
                        url_value = flat_props.get(prop_key) or default_url
                        resolved_el = self._resolve_spring_el_topic(
                            url_value
                        ) if url_value else ""
                        if url_value and not resolved_el.startswith("http"):
                            url_value = self._resolve_spring_el_topic(raw)
                        is_candidate = (
                            url_value
                            and prop_key not in seen_keys
                            and not _is_excluded_url(prop_key)
                        )
                        if is_candidate:
                            resolved_url = (
                                self._resolve_spring_el_topic(url_value)
                                if url_value.startswith("${")
                                else url_value
                            )
                            if resolved_url.startswith("http"):
                                seen_keys.add(prop_key)
                                calls.append(
                                    OutboundCall(
                                        target_url=resolved_url,
                                        config_key=prop_key,
                                        protocol="http",
                                    )
                                )
                elif raw.startswith("http"):
                    if raw not in seen_keys and not _is_excluded_url(raw):
                        seen_keys.add(raw)
                        calls.append(
                            OutboundCall(
                                target_url=raw,
                                config_key=None,
                                protocol="http",
                            )
                        )
                else:
                    # Variable name — try to resolve from @Value fields in the same file
                    # Look for: @Value("${some.key}") ... String rawVar; / String raw = env...
                    value_ref_pattern = re.compile(
                        rf'@Value\s*\(\s*["\'](\$\{{[^}}]+\}})["\'].*?(?:String\s+{re.escape(raw)}\b)',
                        re.DOTALL,
                    )
                    for vm in value_ref_pattern.finditer(content):
                        el_expr = vm.group(1)
                        resolved_url = self._resolve_spring_el_topic(el_expr)
                        if resolved_url.startswith("http"):
                            inner_key = re.fullmatch(r"\$\{([^}]+)\}", el_expr)
                            prop_key = inner_key.group(1).split(":")[0] if inner_key else el_expr
                            if prop_key not in seen_keys and not _is_excluded_url(prop_key):
                                seen_keys.add(prop_key)
                                calls.append(
                                    OutboundCall(
                                        target_url=resolved_url,
                                        config_key=prop_key,
                                        protocol="http",
                                    )
                                )

            # ---- Phase B.2: @Value on method parameters (Change 2) ----
            # Detect @Value("${config.key}") String paramName on @Bean method
            # parameters, resolving the config key via YAML flat props.
            for vm in bean_method_value_pattern.finditer(content):
                el_expr = vm.group(1)  # e.g., "${nba.default.base-uri}"
                inner = re.fullmatch(r"\$\{([^}]+)\}", el_expr)
                if not inner:
                    continue
                prop_key = inner.group(1).split(":")[0]
                is_excluded = (
                    prop_key in seen_keys
                    or _is_excluded_key(prop_key)
                    or _is_excluded_url(prop_key)
                )
                if is_excluded:
                    continue
                if not url_key_pattern.search(prop_key):
                    continue

                # Try to resolve the config key to a URL
                yaml_value = flat_props.get(prop_key)
                target_url: str | None = None
                if yaml_value:
                    is_el = yaml_value.startswith("${")
                    resolved = (
                        self._resolve_spring_el_topic(yaml_value)
                        if is_el else yaml_value
                    )
                    is_http = resolved.startswith(("http://", "https://"))
                    if is_http and not _is_excluded_url(resolved):
                        target_url = resolved
                    else:
                        # Env-var-only value — use env: prefix
                        env_var = (
                            self._extract_env_var_from_spring_el(yaml_value)
                            if is_el else None
                        )
                        if env_var:
                            target_url = f"env:{env_var}"

                # Also check for inline default in @Value expression
                if target_url is None:
                    inner_val = inner.group(1)
                    default_val = (
                        inner_val.split(":", 1)[1]
                        if ":" in inner_val else None
                    )
                    is_http_default = (
                        default_val
                        and default_val.startswith(("http://", "https://"))
                        and not _is_excluded_url(default_val)
                    )
                    if is_http_default:
                        target_url = default_val
                    elif not default_val:
                        # No YAML value, no inline default
                        env_var = (
                            self._extract_env_var_from_spring_el(yaml_value)
                            if yaml_value and yaml_value.startswith("${")
                            else None
                        )
                        if env_var:
                            target_url = f"env:{env_var}"

                if target_url and not _is_excluded_url(target_url):
                    seen_keys.add(prop_key)
                    calls.append(
                        OutboundCall(
                            target_url=target_url,
                            config_key=prop_key,
                            protocol="http",
                        )
                    )

        # ---- Phase C: @HttpExchange interface scanning (Change 3) ----
        # Build a map of config_key → OutboundCall index for enrichment
        call_by_config_key = {c.config_key: c for c in calls if c.config_key}

        # Scan for @HttpExchange interfaces and their instantiation sites
        interface_metadata = self._scan_http_exchange_interfaces(root)
        bean_to_config_key = self._scan_http_exchange_bean_config_keys(root)

        for iface_name, endpoints in interface_metadata.items():
            # Find which config key this interface is bound to via bean registration
            config_key = bean_to_config_key.get(iface_name)
            if config_key and config_key in call_by_config_key:
                call = call_by_config_key[config_key]
                if iface_name not in call.client_interfaces:
                    call.client_interfaces.append(iface_name)
                for ep in endpoints:
                    # Avoid duplicates
                    if not any(e.method == ep.method and e.path == ep.path for e in call.endpoints):
                        call.endpoints.append(ep)

        return calls

    def _extract_env_var_from_spring_el(self, value: str) -> str | None:
        """Extract the environment variable name from a Spring EL expression.

        Examples:
            "${NBA_BASE_URL:}"  → "NBA_BASE_URL"
            "${NBA_BASE_URL}"   → "NBA_BASE_URL"
            "${some.prop:default}" → "some.prop" (not an env var, but the prop key)
            "plain-value"       → None
        """
        m = re.fullmatch(r"\$\{([^}]+)\}", value.strip())
        if not m:
            return None
        inner = m.group(1)
        # Split on ":" to get the var name (before the default)
        var_name = inner.split(":")[0]
        return var_name if var_name else None

    def _scan_http_exchange_interfaces(self, root: Path) -> dict[str, list[EndpointIndex]]:
        """Scan for @HttpExchange-annotated interfaces and extract their endpoint paths.

        Returns a dict mapping interface name → list of EndpointIndex objects
        representing the @GetExchange, @PostExchange, etc. methods.
        """
        interfaces: dict[str, list[EndpointIndex]] = {}

        exchange_method_pattern = re.compile(
            r"@(Get|Post|Put|Delete|Patch)Exchange\b"
        )
        exchange_path_pattern = re.compile(
            r'@(?:Get|Post|Put|Delete|Patch)Exchange\s*\(\s*(?:url\s*=\s*)?["\']([^"\']+)["\']',
        )
        # Class-level @HttpExchange("base-path")
        class_exchange_path_pattern = re.compile(
            r'@HttpExchange\s*\(\s*(?:url\s*=\s*)?["\']([^"\']+)["\']',
        )

        for java_file in root.rglob("*.java"):
            parts = java_file.parts
            if any(part in _EXCLUDED_DIRS for part in parts):
                continue
            if any(part in _TEST_DIR_SEGMENTS for part in parts):
                continue
            try:
                content = java_file.read_text(errors="replace")
            except OSError:
                continue

            if "@HttpExchange" not in content:
                continue

            # Check this is an interface (not a class)
            iface_m = re.search(
                r"(?:public\s+)?interface\s+(\w+)", content, re.MULTILINE
            )
            if not iface_m:
                continue

            iface_name = iface_m.group(1)

            # Check for class-level base path
            class_base_path: str | None = None
            class_path_m = class_exchange_path_pattern.search(content)
            if class_path_m:
                class_base_path = class_path_m.group(1)

            endpoints: list[EndpointIndex] = []
            for method_m in exchange_method_pattern.finditer(content):
                http_verb = method_m.group(1).upper()

                # Extract the path from the annotation
                # Look at the full annotation starting from this match position
                annotation_start = method_m.start()
                path_m = exchange_path_pattern.search(content, annotation_start)
                method_path: str | None = None
                if path_m and path_m.start() == annotation_start:
                    method_path = path_m.group(1)

                # Combine class-level + method-level paths
                full_path = self._combine_paths(class_base_path, method_path)

                endpoints.append(
                    EndpointIndex(
                        method=http_verb,
                        path=full_path,
                    )
                )

            if endpoints:
                interfaces[iface_name] = endpoints

        return interfaces

    def _scan_http_exchange_bean_config_keys(self, root: Path) -> dict[str, str]:
        """Scan @Configuration classes for @HttpExchange client bean registrations.

        Detects the following patterns:
        1. Direct: HttpServiceProxyFactory.createClient(XxxClient.class) in a @Bean method
        2. Delegated: createWebClient(url, XxxClient.class) where createWebClient is a
           private helper that internally calls factory.createClient(clientType)
        3. Concatenated @Value: @Value("${" + STRING_CONST + "}") where STRING_CONST is a
           private static final String declared in the same class (resolves the constant
           to extract the real config key).

        In all cases, the @Value annotation on the enclosing @Bean method parameter
        provides the config key.

        Returns a dict mapping interface name → config key.
        """
        result: dict[str, str] = {}

        # Pattern: createClient(XxxClient.class) or .createClient(XxxClient.class)
        create_client_pattern = re.compile(
            r"(?:createClient|build\(\)\.createClient)\s*\(\s*(\w+)\.class\s*\)"
        )
        # Pattern: delegated helper call like createWebClient(url, XxxClient.class)
        # or any method call ending with (someVar, XxxClient.class)
        delegated_client_pattern = re.compile(
            r"\bcreate\w*\s*\(\s*\w+\s*,\s*(\w+)\.class\s*\)"
        )
        # Pattern for @Value on method parameters (literal Spring EL)
        value_param_pattern = re.compile(
            r'@Value\s*\(\s*["\'](\$\{[^}]+\})["\']'
            r'\s*\)\s*String\s+(\w+)',
        )
        # Pattern to extract private static final String constants from the same file
        string_const_pattern = re.compile(
            r'(?:private|protected|public)\s+static\s+final\s+String\s+(\w+)\s*=\s*["\']([^"\']+)["\']',
        )

        for java_file in root.rglob("*.java"):
            parts = java_file.parts
            if any(part in _EXCLUDED_DIRS for part in parts):
                continue
            if any(part in _TEST_DIR_SEGMENTS for part in parts):
                continue
            try:
                content = java_file.read_text(errors="replace")
            except OSError:
                continue

            if "HttpServiceProxyFactory" not in content and "createClient" not in content:
                continue

            # Build a map of String constants defined in this file
            # (used to resolve concatenated @Value expressions)
            file_string_consts: dict[str, str] = {
                m.group(1): m.group(2)
                for m in string_const_pattern.finditer(content)
            }

            # Find all client references — both direct and delegated patterns
            client_refs: dict[str, int] = {}  # iface_name → position
            for cm in create_client_pattern.finditer(content):
                client_refs[cm.group(1)] = cm.start()
            for cm in delegated_client_pattern.finditer(content):
                iface_name = cm.group(1)
                if iface_name not in client_refs:
                    client_refs[iface_name] = cm.start()

            if not client_refs:
                continue

            # For each client ref, find the enclosing method and its @Value parameter
            # Strategy: look backwards from createClient() to find the method signature
            # with @Value parameter
            for iface_name, pos in client_refs.items():
                self._link_client_to_config_key(
                    content, iface_name, pos, result, file_string_consts
                )

            # Also handle the case where @Value params are not directly linkable
            # to a specific createClient call — use proximity matching
            if not result:
                # Collect all @Value params and createClient calls
                value_params = list(value_param_pattern.finditer(content))
                if value_params and client_refs:
                    self._link_by_method_boundaries(
                        content, value_params, client_refs, result
                    )

        return result

    def _link_client_to_config_key(
        self,
        content: str,
        iface_name: str,
        create_client_pos: int,
        result: dict[str, str],
        file_string_consts: dict[str, str] | None = None,
    ) -> None:
        """Link a createClient(XxxClient.class) call to its @Value config key.

        Searches backwards from the createClient position to find the enclosing
        method's @Value parameter annotation.

        Handles two @Value forms:
        1. Literal: @Value("${some.config.key}") String url
        2. Concatenated: @Value("${" + STRING_CONST + "}") String url
           where STRING_CONST is resolved via file_string_consts.
        """
        url_key_re = _URL_KEY_PATTERN
        if file_string_consts is None:
            file_string_consts = {}

        # Look backwards for the nearest @Value("${...}") String param
        # within a reasonable distance (up to the previous @Bean or start)
        search_start = max(0, create_client_pos - 1500)
        window = content[search_start:create_client_pos]

        # Pattern 1: literal Spring EL @Value
        value_param_re = re.compile(
            r'@Value\s*\(\s*["\'](\$\{([^}]+)\})["\']'
            r'\s*\)\s*String\s+\w+',
        )
        # Pattern 2: concatenated form — @Value("${" + CONST_NAME + "}") String param
        value_param_concat_re = re.compile(
            r'@Value\s*\(\s*"\$\{"\s*\+\s*(\w+)\s*\+\s*"\}"\s*\)'
            r'\s*String\s+\w+',
        )

        # Find the LAST @Value match in the window (closest to createClient)
        last_match = None
        last_match_pos = -1
        for m in value_param_re.finditer(window):
            if m.start() > last_match_pos:
                last_match = m
                last_match_pos = m.start()

        last_concat_match = None
        last_concat_pos = -1
        for m in value_param_concat_re.finditer(window):
            if m.start() > last_concat_pos:
                last_concat_match = m
                last_concat_pos = m.start()

        # Use whichever match is closest to createClient (highest position)
        if last_match and last_match_pos >= last_concat_pos:
            inner = last_match.group(2)
            prop_key = inner.split(":")[0]
            if url_key_re.search(prop_key):
                result[iface_name] = prop_key
        elif last_concat_match:
            const_name = last_concat_match.group(1)
            prop_key = file_string_consts.get(const_name, "")
            if prop_key and url_key_re.search(prop_key):
                result[iface_name] = prop_key

    def _link_by_method_boundaries(
        self,
        content: str,
        value_params: list,
        client_refs: dict[str, int],
        result: dict[str, str],
    ) -> None:
        """Link @Value params to createClient calls using method boundaries.

        Groups @Value annotations and createClient calls by their enclosing
        method (delimited by @Bean annotations), then maps them.
        """
        url_key_re = _URL_KEY_PATTERN

        # Find all @Bean positions to establish method boundaries
        bean_positions = [m.start() for m in re.finditer(r"@Bean\b", content)]
        if not bean_positions:
            return

        # Add sentinel at end
        bean_positions.append(len(content))

        for i in range(len(bean_positions) - 1):
            method_start = bean_positions[i]
            method_end = bean_positions[i + 1]

            # Find @Value params in this method
            method_values = [
                vp for vp in value_params
                if method_start <= vp.start() < method_end
            ]
            # Find createClient calls in this method
            method_clients = [
                (name, pos) for name, pos in client_refs.items()
                if method_start <= pos < method_end
            ]

            if method_values and method_clients:
                # Take the first URL-like @Value and first client
                for vp in method_values:
                    inner = re.fullmatch(r"\$\{([^}]+)\}", vp.group(1))
                    if inner:
                        prop_key = inner.group(1).split(":")[0]
                        if url_key_re.search(prop_key):
                            for client_name, _ in method_clients:
                                if client_name not in result:
                                    result[client_name] = prop_key
                            break

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
                db_re = rf"(?:url|driver[_-]class[_-]name|datasource)\s*:.*{pattern}"
                if re.search(db_re, content):
                    detected.append(db_type)
                elif re.search(rf"jdbc:{pattern}", content):
                    detected.append(db_type)
                elif db_type == "cosmos" and re.search(
                    r"(?:^|\n)\s*cosmos\s*:", content
                ):
                    # Cosmos DB configured via spring.cloud.azure.cosmos.* keys.
                    # Match "cosmos:" as a standalone YAML key (indented on its own line)
                    # to avoid false positives like "no-cosmos:" or comments.
                    detected.append(db_type)

        # --- Scan build.gradle for database connector dependencies ---
        dep_db_signals = [
            (r"org\.postgresql:postgresql|jdbc:postgresql", "postgresql"),
            (r"mysql:mysql-connector|com\.mysql:mysql-connector", "mysql"),
            (r"org\.mariadb", "mariadb"),
            (r"com\.microsoft\.sqlserver", "sqlserver"),
            (r"com\.oracle", "oracle"),
            (r"com\.h2database:h2", "h2"),
            (
                r"com\.azure:azure-spring-data-cosmos"
                r"|spring-cloud-azure-starter-data-cosmos|azure-cosmos",
                "cosmos",
            ),
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

    # --- DTO Schema Extraction ---

    def _extract_dto_schemas(
        self, root: Path, api_contracts: list[ApiContract]
    ) -> dict[str, DtoSchema]:
        """Extract DTO schemas for all types referenced in API contracts.

        1. Collect type names from request bodies and responses
        2. Build class name → file path index
        3. Parse each referenced class
        4. Recursively resolve nested DTO types (up to _MAX_DTO_DEPTH)
        """
        # Step 1: Collect referenced type names
        type_names = self._collect_dto_type_names(api_contracts)
        if not type_names:
            return {}

        # Step 2: Build class index
        class_index = self._build_class_index(root)

        # Step 3 & 4: Parse classes with recursive resolution
        schemas: dict[str, DtoSchema] = {}
        self._resolve_dto_types(type_names, class_index, root, schemas, depth=0)

        return schemas

    def _resolve_dto_types(
        self,
        type_names: set[str],
        class_index: dict[str, Path],
        root: Path,
        schemas: dict[str, DtoSchema],
        depth: int,
    ) -> None:
        """Recursively resolve DTO types up to _MAX_DTO_DEPTH."""
        if depth >= _MAX_DTO_DEPTH:
            return

        new_types: set[str] = set()

        for name in type_names:
            if name in schemas or name in _PRIMITIVE_TYPES:
                continue

            file_path = class_index.get(name)
            if file_path is None:
                logger.debug("DTO class not found in repo", class_name=name)
                continue

            schema = self._parse_java_class(file_path, root)
            if schema is None:
                continue

            schemas[name] = schema

            # Collect nested type references from fields
            for field in schema.fields:
                self._add_dto_type_names(field.type, new_types)

            # Also resolve parent class
            if schema.parent and schema.parent not in _PRIMITIVE_TYPES:
                new_types.add(schema.parent)

        # Remove already-resolved types
        new_types -= set(schemas.keys())
        new_types -= _PRIMITIVE_TYPES

        if new_types:
            self._resolve_dto_types(new_types, class_index, root, schemas, depth + 1)

    def _build_class_index(self, root: Path) -> dict[str, Path]:
        """Build a mapping of simple class name → source file path."""
        index: dict[str, Path] = {}
        for ext in ("*.java", "*.kt"):
            for f in root.rglob(ext):
                # Skip excluded directories
                if any(part in _EXCLUDED_DIRS for part in f.parts):
                    continue
                # Skip test directories
                if any(part in _TEST_DIR_SEGMENTS for part in f.parts):
                    continue
                # Extract class name from filename (ClassName.java → ClassName)
                class_name = f.stem
                if class_name not in index:
                    index[class_name] = f
        return index

    def _collect_dto_type_names(self, api_contracts: list[ApiContract]) -> set[str]:
        """Collect all DTO type names referenced in endpoint contracts."""
        type_names: set[str] = set()
        for contract in api_contracts:
            for ep in contract.endpoints:
                if ep.request_body and ep.request_body.type:
                    self._add_dto_type_names(ep.request_body.type, type_names)
                if ep.response and ep.response.type:
                    self._add_dto_type_names(ep.response.type, type_names)
        return type_names

    def _add_dto_type_names(self, type_str: str, names: set[str]) -> None:
        """Extract concrete DTO type names from a type string, stripping generics."""
        # Handle generic types: List<OrderDto> → OrderDto
        # Handle nested generics: Map<String, List<OrderDto>> → OrderDto
        # Find all type names inside angle brackets
        inner = re.findall(r'[A-Z]\w+', type_str)
        for name in inner:
            if name not in _PRIMITIVE_TYPES:
                names.add(name)
        # Also check the outer type itself
        base = type_str.split('<')[0].strip()
        if base and base[0].isupper() and base not in _PRIMITIVE_TYPES:
            names.add(base)

    def _parse_java_class(self, file_path: Path, root: Path) -> DtoSchema | None:
        """Parse a Java/Kotlin source file to extract DTO schema."""
        try:
            content = file_path.read_text(errors="replace")
        except Exception:
            return None

        class_name = file_path.stem
        relative_path = str(file_path.relative_to(root))

        # Detect kind and parent
        kind, parent = self._detect_class_kind(content, class_name)
        if kind is None:
            return None

        # Extract fields based on kind
        if kind == "enum":
            enum_values = self._extract_enum_values(content, class_name)
            return DtoSchema(
                name=class_name,
                kind="enum",
                enum_values=enum_values,
                source_file=relative_path,
            )

        if kind == "record":
            fields = self._extract_record_fields(content, class_name)
        elif kind == "data_class":
            fields = self._extract_kotlin_data_class_fields(content, class_name)
        else:
            # Regular class — extract from field declarations
            fields = self._extract_class_fields(content, class_name)

        return DtoSchema(
            name=class_name,
            kind=kind if kind != "data_class" else "class",
            fields=fields,
            parent=parent,
            source_file=relative_path,
        )

    def _detect_class_kind(
        self, content: str, class_name: str
    ) -> tuple[str | None, str | None]:
        """Detect class kind and parent class name.

        Returns (kind, parent) where kind is one of:
        "class", "record", "enum", "interface", "data_class", or None if not found.
        """
        # Java record: public record ClassName(...)
        if re.search(rf'\brecord\s+{re.escape(class_name)}\s*\(', content):
            return "record", None

        # Kotlin data class: data class ClassName(...)
        if re.search(rf'\bdata\s+class\s+{re.escape(class_name)}\s*\(', content):
            return "data_class", None

        # Java enum
        if re.search(rf'\benum\s+{re.escape(class_name)}\b', content):
            return "enum", None

        # Java interface
        if re.search(rf'\binterface\s+{re.escape(class_name)}\b', content):
            return "interface", None

        # Java class with optional extends
        m = re.search(
            rf'\bclass\s+{re.escape(class_name)}\b'
            rf'(?:\s+extends\s+(\w+))?',
            content,
        )
        if m:
            parent = m.group(1)
            return "class", parent

        return None, None

    def _extract_class_fields(
        self, content: str, class_name: str
    ) -> list[DtoField]:
        """Extract fields from a Java class body.

        Handles:
        - private/protected/public Type fieldName;
        - Annotations: @NotNull, @NotBlank, @NotEmpty, @JsonProperty, @JsonIgnore
        - Annotations: @Size, @Min, @Max, @Pattern, @Email
        - @Schema(description="...")
        """
        fields: list[DtoField] = []

        # Find the class body — match from class declaration to end
        # We need to find the opening brace of the class
        class_pattern = re.compile(
            rf'\bclass\s+{re.escape(class_name)}\b[^{{]*\{{',
            re.DOTALL,
        )
        class_match = class_pattern.search(content)
        if not class_match:
            return fields

        class_body_start = class_match.end()
        # Find matching closing brace (track depth)
        class_body = self._extract_brace_block(content, class_body_start)
        if not class_body:
            return fields

        # Split into lines and process field declarations
        # Pattern: optional annotations on preceding lines, then field declaration
        lines = class_body.split('\n')
        pending_annotations: list[str] = []

        for line in lines:
            stripped = line.strip()

            # Collect annotations
            if stripped.startswith('@'):
                pending_annotations.append(stripped)
                continue

            # Try to match a field declaration
            field = self._parse_field_declaration(stripped, pending_annotations)
            if field:
                fields.append(field)
                pending_annotations = []
            elif (
                stripped
                and not stripped.startswith('//')
                and not stripped.startswith('/*')
                and not stripped.startswith('*')
            ):
                # Non-annotation, non-field line — reset annotations
                if not self._is_method_or_constructor(stripped, class_name):
                    pass  # Could be inner class, etc.
                pending_annotations = []

        return fields

    def _parse_field_declaration(
        self, line: str, annotations: list[str]
    ) -> DtoField | None:
        """Parse a Java field declaration line into a DtoField.

        Matches patterns like:
            private String email;
            private final List<String> items;
            protected BigDecimal amount;
            String name;  (package-private)
        """
        # Skip if it looks like a method, constructor, or class
        if '(' in line and ')' in line:
            return None
        if line.startswith('class ') or line.startswith('interface ') or line.startswith('enum '):
            return None
        if line.startswith('return ') or line.startswith('if ') or line.startswith('for '):
            return None

        # Field pattern: [access] [static] [final] Type fieldName [= value];
        field_pattern = re.compile(
            r'^(?:(?:private|protected|public)\s+)?'
            r'(?:static\s+)?'
            r'(?:final\s+)?'
            r'(?:transient\s+)?'
            r'(?:volatile\s+)?'
            r'([\w<>,\s\?]+?)\s+'  # Type (including generics)
            r'(\w+)\s*'  # Field name
            r'(?:=\s*[^;]*)?\s*;'  # Optional initializer
        )
        m = field_pattern.match(line)
        if not m:
            return None

        field_type = m.group(1).strip()
        field_name = m.group(2).strip()

        # Skip static fields (constants)
        if 'static ' in line and line.index('static') < line.index(field_name):
            return None

        # Check for @JsonIgnore
        if any('@JsonIgnore' in a for a in annotations):
            return None

        # Determine required from annotations
        required = any(
            ann_name in a
            for a in annotations
            for ann_name in ('@NotNull', '@NotBlank', '@NotEmpty', '@NonNull')
        )

        # Check for @JsonProperty name override
        json_name = None
        for a in annotations:
            jp_match = re.search(r'@JsonProperty\s*\(\s*["\'](\w+)["\']', a)
            if jp_match:
                json_name = jp_match.group(1)
                break
            jp_match = re.search(r'@JsonProperty\s*\(\s*value\s*=\s*["\'](\w+)["\']', a)
            if jp_match:
                json_name = jp_match.group(1)
                break

        # Extract description from @Schema
        description = None
        for a in annotations:
            schema_match = re.search(r'@Schema\s*\([^)]*description\s*=\s*["\']([^"\']+)["\']', a)
            if schema_match:
                description = schema_match.group(1)
                break

        # Extract constraints
        constraints = self._extract_field_constraints(annotations)

        return DtoField(
            name=field_name,
            type=field_type,
            required=required,
            json_name=json_name,
            constraints=constraints,
            description=description,
        )

    def _extract_field_constraints(
        self, annotations: list[str]
    ) -> list[DtoFieldConstraint]:
        """Extract validation constraints from field annotations."""
        constraints: list[DtoFieldConstraint] = []

        for a in annotations:
            # @Size(min=1, max=100)
            size_match = re.search(r'@Size\s*\(([^)]+)\)', a)
            if size_match:
                attrs = size_match.group(1)
                c = DtoFieldConstraint(kind="size")
                min_m = re.search(r'min\s*=\s*(\d+)', attrs)
                max_m = re.search(r'max\s*=\s*(\d+)', attrs)
                if min_m:
                    c.min = int(min_m.group(1))
                if max_m:
                    c.max = int(max_m.group(1))
                constraints.append(c)

            # @Min(0)
            min_match = re.search(r'@Min\s*\(\s*(\d+)\s*\)', a)
            if min_match:
                constraints.append(DtoFieldConstraint(kind="min", value=min_match.group(1)))

            # @Max(1000)
            max_match = re.search(r'@Max\s*\(\s*(\d+)\s*\)', a)
            if max_match:
                constraints.append(DtoFieldConstraint(kind="max", value=max_match.group(1)))

            # @Pattern(regexp="...")
            pattern_match = re.search(r'@Pattern\s*\([^)]*regexp\s*=\s*["\']([^"\']+)["\']', a)
            if pattern_match:
                constraints.append(DtoFieldConstraint(kind="pattern", value=pattern_match.group(1)))

            # @Email
            if '@Email' in a:
                constraints.append(DtoFieldConstraint(kind="email"))

        return constraints

    def _extract_record_fields(
        self, content: str, class_name: str
    ) -> list[DtoField]:
        """Extract fields from a Java record's component list.

        Matches: record ClassName(@NotNull String name, int age) { ... }
        """
        # Find the opening paren of the record component list
        header_match = re.search(
            rf'\brecord\s+{re.escape(class_name)}\s*\(',
            content,
            re.DOTALL,
        )
        if not header_match:
            return []

        # Walk forward from the opening paren to find the matching close,
        # respecting nested parentheses (e.g. inside @Size(min=1, max=50)).
        open_pos = header_match.end() - 1  # index of '('
        depth = 0
        close_pos = -1
        for i in range(open_pos, len(content)):
            if content[i] == '(':
                depth += 1
            elif content[i] == ')':
                depth -= 1
                if depth == 0:
                    close_pos = i
                    break
        if close_pos == -1:
            return []

        params_str = content[open_pos + 1:close_pos].strip()
        if not params_str:
            return []

        fields: list[DtoField] = []
        # Split respecting generics
        params = self._split_params_respecting_generics(params_str)

        for param in params:
            param = param.strip()
            if not param:
                continue

            # Extract annotations from the parameter
            annotations: list[str] = []
            remaining = param
            while remaining.lstrip().startswith('@'):
                # Extract annotation
                ann_match = re.match(r'\s*(@\w+(?:\s*\([^)]*\))?)\s*', remaining)
                if ann_match:
                    annotations.append(ann_match.group(1))
                    remaining = remaining[ann_match.end():]
                else:
                    break

            # Remaining should be "Type name"
            parts = remaining.strip().rsplit(None, 1)
            if len(parts) == 2:
                field_type, field_name = parts
                required = any(
                    ann_name in a
                    for a in annotations
                    for ann_name in ('@NotNull', '@NotBlank', '@NotEmpty', '@NonNull')
                )
                json_name = None
                for a in annotations:
                    jp_match = re.search(r'@JsonProperty\s*\(\s*["\'](\w+)["\']', a)
                    if jp_match:
                        json_name = jp_match.group(1)
                        break
                constraints = self._extract_field_constraints(annotations)
                fields.append(DtoField(
                    name=field_name,
                    type=field_type.strip(),
                    required=required,
                    json_name=json_name,
                    constraints=constraints,
                ))

        return fields

    def _extract_kotlin_data_class_fields(
        self, content: str, class_name: str
    ) -> list[DtoField]:
        """Extract fields from a Kotlin data class constructor.

        Matches: data class ClassName(val name: String, val age: Int)
        """
        m = re.search(
            rf'\bdata\s+class\s+{re.escape(class_name)}\s*\(([^)]*)\)',
            content,
            re.DOTALL,
        )
        if not m:
            return []

        params_str = m.group(1).strip()
        if not params_str:
            return []

        fields: list[DtoField] = []
        params = self._split_params_respecting_generics(params_str)

        for param in params:
            param = param.strip()
            if not param:
                continue

            # Kotlin pattern: [val/var] name: Type [= default]
            kt_match = re.match(
                r'(?:val|var)\s+(\w+)\s*:\s*([\w<>,\s\?]+?)(?:\s*=\s*[^,]*)?$',
                param.strip(),
            )
            if kt_match:
                field_name = kt_match.group(1)
                field_type = kt_match.group(2).strip()
                # Kotlin nullable types: String? → not required
                required = not field_type.endswith('?')
                if field_type.endswith('?'):
                    field_type = field_type[:-1]
                fields.append(DtoField(
                    name=field_name,
                    type=field_type,
                    required=required,
                ))

        return fields

    def _extract_enum_values(self, content: str, class_name: str) -> list[str]:
        """Extract enum constant names from a Java enum."""
        # Find enum body
        enum_pattern = re.compile(
            rf'\benum\s+{re.escape(class_name)}\b[^{{]*\{{([^}}]*)',
            re.DOTALL,
        )
        m = enum_pattern.search(content)
        if not m:
            return []

        body = m.group(1)
        # Enum constants are before the first semicolon or method
        # Split on semicolon to get just the constants part
        constants_part = body.split(';')[0]

        # Extract constant names (uppercase identifiers, possibly with constructor args)
        values: list[str] = []
        for const_match in re.finditer(r'\b([A-Z][A-Z0-9_]*)\b', constants_part):
            val = const_match.group(1)
            if val not in values:
                values.append(val)

        return values

    def _extract_brace_block(self, content: str, start: int) -> str | None:
        """Extract content from start position to matching closing brace."""
        depth = 1
        i = start
        while i < len(content) and depth > 0:
            if content[i] == '{':
                depth += 1
            elif content[i] == '}':
                depth -= 1
            i += 1
        if depth == 0:
            return content[start:i - 1]
        return None

    def _is_method_or_constructor(self, line: str, class_name: str) -> bool:
        """Check if a line looks like a method or constructor declaration."""
        # Constructor: ClassName(
        if re.match(
            rf'\s*(?:public|protected|private)?\s*{re.escape(class_name)}\s*\(',
            line,
        ):
            return True
        # Method: returnType methodName(
        if re.match(
            r'\s*(?:public|protected|private)?\s*(?:static\s+)?(?:final\s+)?[\w<>,\[\]\s]+\s+\w+\s*\(',
            line,
        ):
            return True
        return False
