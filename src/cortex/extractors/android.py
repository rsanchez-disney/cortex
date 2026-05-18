"""Android extractor — parses Android project files to produce a ServiceManifest.

Extracts:
- Language (Kotlin/Java) from source file counts (excluding build/ directories)
- Min/target/compile SDK from build.gradle(.kts), with buildSrc constant resolution
- Application ID from build.gradle(.kts) or AndroidManifest.xml; falls back to namespace
- Kotlin version and AGP version from libs.versions.toml [versions] section
- Gradle plugins from libs.versions.toml [plugins] section
- Build variants / product flavors from app/build.gradle.kts or properties/ directory
- Dependencies from build.gradle(.kts) and libs.versions.toml, with category tagging
- Modules from settings.gradle(.kts) with type and inter-module dependency graph
- Permissions from AndroidManifest.xml
- Entry activities from AndroidManifest.xml (deduplicated)
- CI system from .github/workflows/ or azure-pipelines.yml
- Source repo git info (remote URL + HEAD commit)
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path

import structlog

from cortex import __version__
from cortex.extractors.base import Extractor
from cortex.schema import (
    ApiCall,
    ApiContract,
    Dependency,
    EntryPoint,
    ModuleInfo,
    ServiceManifest,
    ServiceYaml,
)

logger = structlog.get_logger()

# Gradle dependency configurations and their categories
_DEP_CONFIG_CATEGORY: dict[str, str] = {
    "implementation": "runtime",
    "api": "runtime",
    "testImplementation": "test",
    "androidTestImplementation": "test",
    "debugImplementation": "debug",
    "releaseImplementation": "runtime",
    "ksp": "build",
    "kapt": "build",
    "annotationProcessor": "build",
    "coreLibraryDesugaring": "build",
}

# All configuration names to match in dependency regex
_ALL_CONFIGS = "|".join(re.escape(k) for k in sorted(_DEP_CONFIG_CATEGORY, key=len, reverse=True))

# Plugin IDs and alias fragments that indicate KMP (Kotlin Multiplatform)
_KMP_PLUGIN_IDS = {
    "kotlin-multiplatform",
    "org.jetbrains.kotlin.multiplatform",
    "kotlinMultiplatform",  # version catalog alias
    "kotlin.multiplatform",  # alternative alias form
}
# Plugin IDs that indicate Android application
_APP_PLUGIN_IDS = {"com.android.application"}
# Plugin IDs that indicate Android library
_LIB_PLUGIN_IDS = {"com.android.library"}


class AndroidExtractor(Extractor):
    """Extractor for Android (Kotlin/Java) repositories."""

    type = "android"

    def extract(self, repo_path: Path, service_yaml: ServiceYaml) -> ServiceManifest:
        """Extract metadata from an Android repo."""
        # Respect extractor_hints.project_root
        effective_root = repo_path
        if service_yaml.extractor_hints and service_yaml.extractor_hints.project_root:
            effective_root = repo_path / service_yaml.extractor_hints.project_root

        language, lang_version = self._detect_language(effective_root)
        sdk_info = self._parse_gradle_sdk(effective_root, repo_path)
        app_id = self._find_application_id(effective_root)
        dependencies = self._parse_dependencies(effective_root)
        modules = self._parse_modules(effective_root)
        permissions = self._parse_permissions(effective_root)
        entry_points = self._parse_entry_activities(effective_root)
        ci = self._detect_ci(repo_path)  # CI files are at repo root, not project root
        api_contracts = self.find_api_contracts(effective_root)
        api_calls = self._parse_ktorfit_interfaces(effective_root)
        source_repo = self._get_source_repo(repo_path)

        # Extract kotlin/agp versions, plugins, and build variants from version catalog + gradle
        catalog_info = self._parse_version_catalog_metadata(effective_root)
        kotlin_version = catalog_info.get("kotlin_version") or lang_version
        agp_version = catalog_info.get("agp_version")
        gradle_plugins = catalog_info.get("plugins", [])
        build_variants = self._parse_build_variants(effective_root)

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
            language_version=kotlin_version,
            slack=service_yaml.slack,
            runbook=service_yaml.runbook,
            jira_component=service_yaml.jira_component,
            application_id=app_id,
            min_sdk=sdk_info.get("min_sdk"),
            target_sdk=sdk_info.get("target_sdk"),
            compile_sdk=sdk_info.get("compile_sdk"),
            android_gradle_plugin=agp_version,
            modules=modules,
            permissions=permissions,
            gradle_plugins=gradle_plugins,
            build_variants=build_variants,
            dependencies=dependencies,
            entry_points=entry_points,
            api_contracts=api_contracts,
            api_calls=api_calls,
            runtime=None,
            ci=ci,
            integration_notes=[
                {"scope": n.scope, "note": n.note} for n in service_yaml.integration_notes
            ]
            if service_yaml.integration_notes
            else [],
            extracted_at=datetime.now(UTC),
            extractor_version=__version__,
            source_repo=source_repo,
        )

    def find_api_contracts(self, repo_path: Path) -> list[ApiContract]:
        """Mobile apps typically don't have API contracts."""
        return []

    def _collect_string_constants(self, root: Path) -> dict[str, str]:
        """Scan Kotlin source files for string const val declarations.

        Handles patterns like:
            const val TICKETING_API = "ticketing"
            const val API_VERSION_V1 = "v1"
            companion object { const val BASE = "/api" }
            object ApiPaths { const val USERS = "/users" }

        Scans all .kt files in the project (excluding build/generated dirs),
        focusing on files named *Constants.kt, *Paths.kt, *Urls.kt, *Config.kt,
        *Api.kt, *Service.kt in any directory, as well as all source files.

        Returns a dict mapping constant name → string value (first-wins).
        """
        excluded_dirs = {"build", "generated", ".gradle", "out"}
        constants: dict[str, str] = {}
        const_pattern = re.compile(
            r'''const\s+val\s+(\w+)\s*=\s*["']([^"']+)["']'''
        )

        for kt_file in root.rglob("*.kt"):
            parts = kt_file.parts
            if any(part in excluded_dirs for part in parts):
                continue
            try:
                content = kt_file.read_text(errors="replace")
            except OSError:
                continue
            for m in const_pattern.finditer(content):
                # first-wins: don't overwrite already-seen constant names
                constants.setdefault(m.group(1), m.group(2))

        return constants

    def _resolve_string_template(self, template: str, constants: dict[str, str]) -> str:
        """Resolve Kotlin string template variables: $FOO and ${FOO}.

        Args:
            template: Path string potentially containing $VARIABLE or ${VARIABLE} refs.
            constants: Mapping of constant name → resolved string value.

        Returns:
            The template with all resolvable variables replaced. Unresolvable
            variables are left as-is (e.g. $UNKNOWN stays $UNKNOWN).
        """
        def replacer(match: re.Match) -> str:
            # Group 1: ${VAR} form, group 2: $VAR form
            var_name = match.group(1) or match.group(2)
            return constants.get(var_name, match.group(0))

        return re.sub(r'\$\{(\w+)\}|\$([A-Za-z_]\w*)', replacer, template)

    def _parse_ktorfit_interfaces(self, root: Path) -> list[ApiCall]:
        """Scan Kotlin source files for Ktorfit/Retrofit-style interface definitions.

        Detects:
        1. interface FooApi { @GET("/v1/foo") suspend fun ... }  — Ktorfit/Retrofit
        2. @GET, @POST, @PUT, @DELETE, @PATCH annotations with path arguments
        3. Groups endpoints by interface name

        Resolves Kotlin string template variables (e.g. $TICKETING_API) in
        annotation paths using const val declarations found anywhere in the project.

        Focuses on files in api/, network/, data/, remote/ directories or
        files matching *Api.kt, *Service.kt, *Endpoint.kt patterns.
        Skips build/ and generated/ directories.
        """
        excluded_dirs = {"build", "generated", ".gradle", "out"}
        network_dir_names = {"api", "network", "data", "remote", "datasource"}
        http_annotation_pattern = re.compile(
            r'@(GET|POST|PUT|DELETE|PATCH|HTTP)\s*\(\s*["\'](/?[^"\']+)["\']\s*\)',
            re.IGNORECASE,
        )
        interface_pattern = re.compile(r'interface\s+(\w+)')

        # Collect string constants once for the whole project
        constants = self._collect_string_constants(root)

        api_calls: list[ApiCall] = []
        seen_calls: set[tuple[str, str]] = set()  # (method, path) deduplication

        for kt_file in root.rglob("*.kt"):
            parts = kt_file.parts
            if any(part in excluded_dirs for part in parts):
                continue

            # Focus on network-related files only
            is_network_dir = any(part.lower() in network_dir_names for part in parts)
            is_api_file = any(
                kt_file.name.endswith(suffix)
                for suffix in ("Api.kt", "Service.kt", "Endpoint.kt", "ApiService.kt")
            )
            if not is_network_dir and not is_api_file:
                continue

            try:
                content = kt_file.read_text(errors="replace")
            except OSError:
                continue

            # Find interface name(s) in this file
            interface_names = interface_pattern.findall(content)
            interface_name = interface_names[0] if interface_names else kt_file.stem

            # Find all HTTP annotations
            for m in http_annotation_pattern.finditer(content):
                method = m.group(1).upper()
                raw_path = m.group(2)
                if not raw_path.startswith("/"):
                    raw_path = "/" + raw_path
                # Resolve Kotlin string template variables
                path = self._resolve_string_template(raw_path, constants)
                if "$" in path:
                    logger.debug(
                        "Unresolved variables in API path",
                        file=str(kt_file),
                        path=path,
                    )
                call_key = (method, path)
                if call_key not in seen_calls:
                    seen_calls.add(call_key)
                    api_calls.append(
                        ApiCall(
                            method=method,
                            path=path,
                            interface_name=interface_name,
                        )
                    )

        return api_calls

    # --- Private parsing methods ---

    def _detect_language(self, root: Path) -> tuple[str, str | None]:
        """Detect primary language by counting .kt vs .java source files.

        Excludes build/ and generated/ directories to avoid counting
        KSP/KAPT-generated Java stubs inflating the Java count.
        """
        excluded_dirs = {"build", "generated", ".gradle"}

        def _count_files(ext: str) -> int:
            count = 0
            for p in root.rglob(f"*{ext}"):
                if not any(part in excluded_dirs for part in p.parts):
                    count += 1
            return count

        kt_count = _count_files(".kt")
        java_count = _count_files(".java")

        if kt_count >= java_count:
            return "kotlin", None
        return "java", None

    def _resolve_buildsrc_constants(self, repo_root: Path) -> dict[str, str]:
        """Scan buildSrc for Kotlin const val integer declarations.

        Handles patterns like:
            const val compileSdk = 36
            const val minSdk = 29

        Returns a dict mapping constant name → string value, e.g.
            {"compileSdk": "36", "minSdk": "29"}
        """
        constants: dict[str, str] = {}
        buildsrc_root = repo_root / "buildSrc"
        if not buildsrc_root.is_dir():
            return constants

        for kt_file in buildsrc_root.rglob("*.kt"):
            try:
                content = kt_file.read_text()
            except OSError:
                continue
            for m in re.finditer(r"const\s+val\s+(\w+)\s*=\s*(\d+)", content):
                # Don't overwrite — first match wins (alphabetical file order)
                constants.setdefault(m.group(1), m.group(2))

        return constants

    def _parse_gradle_sdk(self, root: Path, repo_root: Path | None = None) -> dict[str, str | None]:
        """Parse SDK versions from build.gradle(.kts) — app module first, then root.

        When a value is a non-numeric reference (e.g. ``minSdk = Android.minSdk``),
        attempts to resolve it from buildSrc ``const val`` declarations.

        Args:
            root: Effective project root (may differ from repo root if extractor_hints
                  specifies project_root).
            repo_root: Actual repo root — used to locate ``buildSrc/``.  Defaults to
                ``root`` when not provided.
        """
        if repo_root is None:
            repo_root = root

        sdk_info: dict[str, str | None] = {
            "min_sdk": None,
            "target_sdk": None,
            "compile_sdk": None,
        }

        # Look for app/build.gradle(.kts) first, then root build.gradle(.kts)
        gradle_files: list[Path] = []
        for name in [
            "app/build.gradle.kts",
            "app/build.gradle",
            "build.gradle.kts",
            "build.gradle",
        ]:
            p = root / name
            if p.exists():
                gradle_files.append(p)

        # Collect raw values — may be numeric literals or identifier references
        raw: dict[str, str | None] = {
            "min_sdk": None,
            "target_sdk": None,
            "compile_sdk": None,
        }

        sdk_patterns: list[tuple[str, str]] = [
            ("min_sdk", r"minSdk\s*[=:]\s*(\S+)"),
            ("target_sdk", r"targetSdk\s*[=:]\s*(\S+)"),
            ("compile_sdk", r"compileSdk\s*[=:]\s*(\S+)"),
        ]

        for gf in gradle_files:
            content = gf.read_text()
            for key, pattern in sdk_patterns:
                if raw[key] is None:
                    m = re.search(pattern, content)
                    if m:
                        raw[key] = m.group(1)

        # Resolve values: numeric literals are used directly; identifiers are
        # looked up in buildSrc constants
        buildsrc = None  # Lazy-load

        for key, raw_val in raw.items():
            if raw_val is None:
                continue
            if re.fullmatch(r"\d+", raw_val):
                sdk_info[key] = raw_val
            else:
                # Non-numeric reference — look up last segment (handles Object.constName)
                const_name = raw_val.split(".")[-1]
                if buildsrc is None:
                    buildsrc = self._resolve_buildsrc_constants(repo_root)
                resolved = buildsrc.get(const_name)
                if resolved:
                    sdk_info[key] = resolved
                else:
                    logger.debug(
                        "Could not resolve SDK constant",
                        key=key,
                        raw_value=raw_val,
                        const_name=const_name,
                    )

        return sdk_info

    def _find_application_id(self, root: Path) -> str | None:
        """Find applicationId from build.gradle(.kts) or AndroidManifest.xml.

        Resolution order:
        1. ``applicationId = "..."`` string literal in app/build.gradle(.kts)
        2. ``namespace = "..."`` string literal in app/build.gradle(.kts) (fallback
           for projects where applicationId is loaded dynamically from a properties
           file at build time)
        3. ``package`` attribute in AndroidManifest.xml
        """
        namespace_fallback: str | None = None

        # Try gradle files first
        for name in ["app/build.gradle.kts", "app/build.gradle"]:
            p = root / name
            if p.exists():
                content = p.read_text()
                m = re.search(r'applicationId\s*[=:]\s*["\']([^"\']+)["\']', content)
                if m:
                    return m.group(1)
                # Capture namespace as a fallback while we have the file open
                if namespace_fallback is None:
                    ns = re.search(r'namespace\s*=\s*["\']([^"\']+)["\']', content)
                    if ns:
                        namespace_fallback = ns.group(1)

        # Use namespace fallback if found
        if namespace_fallback:
            logger.debug(
                "applicationId not found as literal; falling back to namespace",
                namespace=namespace_fallback,
            )
            return namespace_fallback

        # Last resort: AndroidManifest.xml package attribute
        for manifest_path in root.rglob("AndroidManifest.xml"):
            try:
                tree = ET.parse(manifest_path)
                pkg = tree.getroot().get("package")
                if pkg:
                    return pkg
            except ET.ParseError:
                continue

        return None

    def _parse_dependencies(self, root: Path) -> list[Dependency]:
        """Parse dependencies from build.gradle(.kts) and libs.versions.toml."""
        deps: list[Dependency] = []
        seen: set[str] = set()

        # Parse from build.gradle(.kts) files
        for gf in root.rglob("build.gradle*"):
            if gf.is_file():
                content = gf.read_text()
                self._parse_gradle_deps(content, gf.name, deps, seen)

        # Parse from libs.versions.toml if present
        toml_path = root / "gradle" / "libs.versions.toml"
        if toml_path.exists():
            self._parse_version_catalog(toml_path, deps, seen)

        return deps

    def _parse_gradle_deps(
        self, content: str, source: str, deps: list[Dependency], seen: set[str]
    ) -> None:
        """Parse dependency declarations from Gradle, capturing configuration name.

        Matches patterns like:
            implementation("group:artifact:version")
            testImplementation "group:artifact:version"
            ksp(libs.something) — skipped (handled by version catalog)
        """
        pattern = (
            rf"({_ALL_CONFIGS})\s*[\(]?\s*[\"']([^\"']+)[\"']"
        )
        for m in re.finditer(pattern, content):
            config_name = m.group(1)
            dep_str = m.group(2)
            parts = dep_str.split(":")
            if len(parts) >= 2:
                name = f"{parts[0]}:{parts[1]}"
                if name not in seen:
                    seen.add(name)
                    category = _DEP_CONFIG_CATEGORY.get(config_name)
                    deps.append(
                        Dependency(
                            name=name,
                            version=parts[2] if len(parts) > 2 else None,
                            source=source,
                            direct=True,
                            category=category,
                        )
                    )

    def _parse_version_catalog(
        self, toml_path: Path, deps: list[Dependency], seen: set[str]
    ) -> None:
        """Parse libs.versions.toml version catalog."""
        try:
            import tomli
        except ImportError:
            logger.warning("tomli not installed, skipping version catalog parsing")
            return

        with open(toml_path, "rb") as f:
            catalog = tomli.load(f)

        versions = catalog.get("versions", {})
        libraries = catalog.get("libraries", {})

        for _lib_alias, lib_def in libraries.items():
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

                version = None
                ver_ref = lib_def.get("version")
                if isinstance(ver_ref, str):
                    version = ver_ref
                elif isinstance(ver_ref, dict):
                    ref = ver_ref.get("ref")
                    if ref and ref in versions:
                        version = versions[ref]

                if dep_name not in seen:
                    seen.add(dep_name)
                    deps.append(
                        Dependency(
                            name=dep_name,
                            version=version,
                            source="libs.versions.toml",
                            direct=True,
                        )
                    )

    def _parse_version_catalog_metadata(self, root: Path) -> dict:
        """Extract kotlin/agp versions and plugin IDs from libs.versions.toml.

        Returns a dict with keys:
            - ``kotlin_version``: Kotlin compiler version string or None
            - ``agp_version``: Android Gradle Plugin version string or None
            - ``plugins``: list of plugin ID strings from [plugins] section
        """
        result: dict = {"kotlin_version": None, "agp_version": None, "plugins": []}

        toml_path = root / "gradle" / "libs.versions.toml"
        if not toml_path.exists():
            return result

        try:
            import tomli
        except ImportError:
            logger.warning("tomli not installed, skipping version catalog metadata parsing")
            return result

        try:
            with open(toml_path, "rb") as f:
                catalog = tomli.load(f)
        except Exception as e:
            logger.warning("Failed to parse libs.versions.toml", error=str(e))
            return result

        versions = catalog.get("versions", {})
        result["kotlin_version"] = versions.get("kotlin")
        result["agp_version"] = versions.get("agp")

        # Extract plugin IDs from [plugins] section
        plugins_section = catalog.get("plugins", {})
        plugin_ids: list[str] = []
        for _alias, plugin_def in plugins_section.items():
            if isinstance(plugin_def, dict):
                pid = plugin_def.get("id")
                if pid and pid not in plugin_ids:
                    plugin_ids.append(pid)
            elif isinstance(plugin_def, str):
                # Simple form: "plugin.id:version"
                pid = plugin_def.split(":")[0]
                if pid and pid not in plugin_ids:
                    plugin_ids.append(pid)

        result["plugins"] = plugin_ids
        return result

    def _parse_modules(self, root: Path) -> list[ModuleInfo]:
        """Parse module names from settings.gradle(.kts) and enrich with type + deps.

        For each module found in settings.gradle, this method:
        1. Resolves the on-disk directory path (handles case differences like
           `:common` → ``Common/``, and nested modules like `:a:b` → ``a/b/``).
        2. Opens the module's build.gradle(.kts) to detect the applied plugin.
        3. Parses ``project(":...")`` declarations for inter-module dependencies.
        4. Resolves ``Modules.xxx`` constant references from buildSrc if needed.

        Returns a list of ``ModuleInfo`` objects.
        """
        module_names: list[str] = []

        for settings_name in ["settings.gradle.kts", "settings.gradle"]:
            p = root / settings_name
            if p.exists():
                content = p.read_text()
                # Match: include(":app", ":core", ":feature-login")
                for m in re.finditer(r"include\s*\(([^)]+)\)", content):
                    includes = m.group(1)
                    for mod in re.findall(r"""["']:([^"']+)["']""", includes):
                        module_names.append(mod)
                # Also match: include ":app"  (without parentheses, Groovy style)
                for m in re.finditer(r"""include\s+["']:([^"']+)["']""", content):
                    mod = m.group(1)
                    if mod not in module_names:
                        module_names.append(mod)
                break  # Use first found

        if not module_names:
            return []

        # Load Modules.kt string constants for dep resolution
        modules_kt_constants = self._resolve_modules_kt_constants(root)

        result: list[ModuleInfo] = []
        for mod_name in module_names:
            build_file = self._find_module_build_file(root, mod_name)
            if build_file is None:
                result.append(ModuleInfo(name=mod_name, type="unknown", dependencies=[]))
                continue

            try:
                build_content = build_file.read_text(encoding="utf-8", errors="replace")
            except OSError:
                result.append(ModuleInfo(name=mod_name, type="unknown", dependencies=[]))
                continue

            mod_type = self._detect_module_type(build_content)
            inter_module_deps = self._parse_inter_module_deps(
                build_content, modules_kt_constants
            )

            result.append(
                ModuleInfo(name=mod_name, type=mod_type, dependencies=inter_module_deps)
            )

        return result

    def _find_module_build_file(self, root: Path, module_path: str) -> Path | None:
        """Resolve a Gradle module path to its build.gradle(.kts) on disk.

        Handles:
        - ``module_path = "app"`` → ``root/app/build.gradle.kts``
        - ``module_path = "common"`` → ``root/Common/build.gradle.kts`` (case-insensitive)
        - ``module_path = "adobeclippers:developerSettings"``
          → ``root/adobeclippers/developerSettings/``
        """
        # Convert colon-separated path to directory path (e.g. "a:b" → "a/b")
        dir_path = module_path.replace(":", "/")

        # Try exact match first
        for gradle_name in ["build.gradle.kts", "build.gradle"]:
            candidate = root / dir_path / gradle_name
            if candidate.exists():
                return candidate

        # Case-insensitive fallback — glob for any directory matching
        # the last segment of the path (for top-level modules only)
        parts = dir_path.split("/")
        if len(parts) == 1:
            # Single-level module — try case-insensitive lookup
            target_dir_name = parts[0].lower()
            for child in root.iterdir():
                if child.is_dir() and child.name.lower() == target_dir_name:
                    for gradle_name in ["build.gradle.kts", "build.gradle"]:
                        candidate = child / gradle_name
                        if candidate.exists():
                            return candidate

        return None

    def _detect_module_type(self, build_content: str) -> str:
        """Detect the module type from the plugins block.

        KMP takes precedence over library when both plugins are applied.
        """
        # Check for KMP first (highest precedence)
        for kmp_id in _KMP_PLUGIN_IDS:
            if kmp_id in build_content:
                return "kmp"
        for app_id in _APP_PLUGIN_IDS:
            if app_id in build_content:
                return "application"
        for lib_id in _LIB_PLUGIN_IDS:
            if lib_id in build_content:
                return "library"
        return "unknown"

    def _resolve_modules_kt_constants(self, repo_root: Path) -> dict[str, str]:
        """Scan buildSrc for Modules.kt string const val declarations.

        Handles patterns like:
            const val common = ":common"
            const val library = ":multi-platform-library"

        Returns a dict mapping constant name → module path string, e.g.
            {"common": ":common", "library": ":multi-platform-library"}
        """
        constants: dict[str, str] = {}
        buildsrc_root = repo_root / "buildSrc"
        if not buildsrc_root.is_dir():
            return constants

        for kt_file in buildsrc_root.rglob("*.kt"):
            try:
                content = kt_file.read_text()
            except OSError:
                continue
            # Match: const val common = ":common"  or  const val library = ":multi-platform-library"
            for m in re.finditer(r'''const\s+val\s+(\w+)\s*=\s*["\']([^"\']+)["\']''', content):
                constants.setdefault(m.group(1), m.group(2))

        return constants

    def _parse_inter_module_deps(
        self, build_content: str, modules_kt_constants: dict[str, str]
    ) -> list[str]:
        """Parse inter-module dependencies from a build.gradle(.kts) file.

        Handles:
        - ``implementation(project(":common"))`` → ``"common"``
        - ``api(project(Modules.common))`` → resolves via modules_kt_constants → ``"common"``
        """
        deps: list[str] = []
        seen: set[str] = set()

        # Pattern 1: project(":module-name") or project(':module-name')
        for m in re.finditer(r'project\s*\(\s*["\']:([^"\']+)["\']', build_content):
            mod_path = m.group(1)
            if mod_path not in seen:
                seen.add(mod_path)
                deps.append(mod_path)

        # Pattern 2: project(Modules.xxx) — resolve via buildSrc constants
        for m in re.finditer(r'project\s*\(\s*Modules\.(\w+)\s*\)', build_content):
            const_name = m.group(1)
            resolved = modules_kt_constants.get(const_name)
            if resolved:
                # Remove leading colon: ":common" → "common"
                mod_path = resolved.lstrip(":")
                if mod_path not in seen:
                    seen.add(mod_path)
                    deps.append(mod_path)

        return deps

    def _parse_permissions(self, root: Path) -> list[str]:
        """Parse permissions from AndroidManifest.xml."""
        permissions: list[str] = []
        android_ns = "http://schemas.android.com/apk/res/android"

        for manifest_path in root.rglob("AndroidManifest.xml"):
            try:
                tree = ET.parse(manifest_path)
                for perm_elem in tree.findall(".//uses-permission"):
                    perm_name = perm_elem.get(f"{{{android_ns}}}name")
                    if perm_name and perm_name not in permissions:
                        permissions.append(perm_name)
            except ET.ParseError:
                logger.warning("Failed to parse AndroidManifest.xml", path=str(manifest_path))
                continue

        return permissions

    def _parse_entry_activities(self, root: Path) -> list[EntryPoint]:
        """Parse entry activities (MAIN intent) from AndroidManifest.xml.

        Deduplicates entries across all manifest files (build variants, flavors,
        and merged manifests all reference the same activity names).
        """
        entry_points: list[EntryPoint] = []
        seen_refs: set[str] = set()
        android_ns = "http://schemas.android.com/apk/res/android"

        for manifest_path in root.rglob("AndroidManifest.xml"):
            try:
                tree = ET.parse(manifest_path)
                for activity in tree.findall(".//activity"):
                    for intent_filter in activity.findall("intent-filter"):
                        for action in intent_filter.findall("action"):
                            action_name = action.get(f"{{{android_ns}}}name")
                            if action_name == "android.intent.action.MAIN":
                                activity_name = activity.get(f"{{{android_ns}}}name", "unknown")
                                if activity_name not in seen_refs:
                                    seen_refs.add(activity_name)
                                    entry_points.append(
                                        EntryPoint(
                                            kind="main-activity",
                                            ref=activity_name,
                                        )
                                    )
            except ET.ParseError:
                continue

        return entry_points

    def _parse_build_variants(self, root: Path) -> list[str]:
        """Extract product flavor names from app/build.gradle.kts or properties/ dir.

        Resolution order:
        1. Parse explicit ``productFlavors { create("name") { ... } }`` blocks
           in app/build.gradle.kts.
        2. Fallback: detect ``properties/`` dir and derive flavor names from
           3-part filenames: ``<club>.<env>.properties`` → ``<club><Env>``
           (e.g. ``clippers.qa.properties`` → ``clippersQa``).
        """
        # Try explicit productFlavors block first
        for gradle_name in ["app/build.gradle.kts", "app/build.gradle"]:
            p = root / gradle_name
            if not p.exists():
                continue
            content = p.read_text()
            # Match create("flavorName") inside productFlavors block
            # Skip template expressions (containing $) — those are dynamically computed
            raw_flavors = re.findall(r'create\s*\(\s*["\']([^"\']+)["\']', content)
            flavors = [f for f in raw_flavors if "$" not in f]
            if flavors:
                return flavors

        # Fallback: derive from properties/ directory filenames
        properties_dir = root / "properties"
        if properties_dir.is_dir():
            flavors: list[str] = []
            for prop_file in sorted(properties_dir.iterdir()):
                if not prop_file.is_file():
                    continue
                parts = prop_file.name.split(".")
                # 3-part filename: <club>.<env>.properties
                if len(parts) == 3 and parts[2] == "properties":
                    club = parts[0]
                    env = parts[1]
                    flavor_name = f"{club}{env[0].upper()}{env[1:]}"
                    if flavor_name not in flavors:
                        flavors.append(flavor_name)
            if flavors:
                return flavors

        return []

    def _detect_ci(self, root: Path) -> str | None:
        """Detect CI system from file presence."""
        if (root / ".github" / "workflows").is_dir():
            return "github-actions"
        if (root / "azure-pipelines.yml").exists():
            return "azure-pipelines"
        if (root / ".gitlab-ci.yml").exists():
            return "gitlab-ci"
        return None
