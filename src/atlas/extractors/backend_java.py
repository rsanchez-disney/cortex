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
                    return yaml_props[prop_key]

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
                    fields[field_name] = yaml_props[prop_key]
                # If no default and not in YAML, skip — unresolvable at static analysis time

        return fields

    def _parse_kafka_producers(self, root: Path) -> list[str]:
        """Scan Java source and application.yml for Kafka producer patterns.

        Detects:
        1. kafkaTemplate.send("topic", ...) — string literal topic
        2. kafkaTemplate.send(Constants.TOPIC, ...) — constant reference
        3. kafkaTemplate.send(topicField, ...) — @Value-injected String field
        4. new ProducerRecord<>(topicField, ...) where topicField is @Value-injected
        5. @SendTo("topic") — reply topics on @KafkaListener methods

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
        # Merge value_fields into constants so _resolve_kafka_topic_ref can use them
        merged = {**constants, **value_fields}

        # Pattern: kafkaTemplate.send(<arg>, ...)
        # Argument can be a string literal, a constant ref, or a field/variable name
        send_pattern = re.compile(
            r"""kafkaTemplate\.send\(\s*(["']([^"']+)["']|\$\{[^}]+\}|[\w.]+)\s*[,)]""",
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

            # Collect names of ProducerRecord variables in this file so we can skip them
            # when they appear as the first arg to kafkaTemplate.send()
            producer_record_vars = set(producer_record_var_pattern.findall(content))

            # Extract topics from new ProducerRecord<>(topicArg, ...) ctors
            for m in producer_record_ctor_pattern.finditer(content):
                raw = m.group(1)
                resolved = self._resolve_kafka_topic_ref(raw, merged, yaml_props)
                if resolved and resolved not in seen:
                    seen.add(resolved)
                    produces.append(resolved)

            for m in send_pattern.finditer(content):
                raw = m.group(1)
                # Skip ProducerRecord variables — topic already extracted from ctor above
                raw_stripped = raw.strip().strip('"').strip("'")
                if raw_stripped in producer_record_vars:
                    continue
                resolved = self._resolve_kafka_topic_ref(raw, merged, yaml_props)
                if resolved and resolved not in seen:
                    seen.add(resolved)
                    produces.append(resolved)

            for m in sendto_pattern.finditer(content):
                topic = m.group(1).strip()
                resolved = self._resolve_kafka_topic_ref(topic, merged, yaml_props)
                if resolved and resolved not in seen:
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

        Phase B — Java source scanning:
            Looks for WebClient.builder().baseUrl() patterns and @Value injection
            to confirm and link config keys to service calls.

        Returns a list of OutboundCall objects with target_url and config_key set.
        """
        calls: list[OutboundCall] = []
        seen_keys: set[str] = set()

        # URL-like keys in YAML config
        url_key_pattern = re.compile(
            r"(?:base[_-]?url|\.url|\.host|\.endpoint|\.uri)$",
            re.IGNORECASE,
        )

        # Excluded URL patterns (infrastructure, not services)
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
            ]
        ]

        def _is_excluded_url(url: str) -> bool:
            return any(p.search(url) for p in excluded_url_patterns)

        # Load YAML config and scan for URL keys
        flat_props = self._load_yaml_flat_props(root)
        for key, value in flat_props.items():
            if not url_key_pattern.search(key):
                continue
            if not value.startswith(("http://", "https://", "${", "http")):
                # Also allow Spring EL like ${SOME_URL:https://...}
                resolved = self._resolve_spring_el_topic(value)
                if not resolved.startswith(("http://", "https://")):
                    continue
                value = resolved

            if _is_excluded_url(value) or _is_excluded_url(key):
                continue

            if key not in seen_keys:
                seen_keys.add(key)
                calls.append(
                    OutboundCall(
                        target_url=value if value.startswith("http") else None,
                        config_key=key,
                        protocol="http",
                    )
                )

        # Phase B: scan Java source for WebClient.builder().baseUrl() patterns
        # to find additional URL references not in YAML
        webclient_pattern = re.compile(
            r"""WebClient\.builder\(\)\s*\.baseUrl\(\s*(["']([^"']+)["']|\$\{[^}]+\}|[\w.]+)\s*\)""",
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

            if "WebClient" not in content:
                continue

            for m in webclient_pattern.finditer(content):
                raw = m.group(1).strip().strip('"').strip("'")
                if raw.startswith("${"):
                    # Resolve from flat props
                    inner = re.fullmatch(r"\$\{([^}]+)\}", raw)
                    if inner:
                        prop_key = inner.group(1).split(":")[0]
                        if prop_key in flat_props:
                            url_value = flat_props[prop_key]
                            if prop_key not in seen_keys and not _is_excluded_url(url_value):
                                seen_keys.add(prop_key)
                                calls.append(
                                    OutboundCall(
                                        target_url=url_value if url_value.startswith("http") else None,
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

        return calls

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
            (r"com\.azure:azure-spring-data-cosmos|spring-cloud-azure-starter-data-cosmos|azure-cosmos", "cosmos"),
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
