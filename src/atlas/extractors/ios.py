"""iOS extractor — parses iOS project files to produce a ServiceManifest.

Extracts:
- Language (Swift/Objective-C) from source file counts
- Swift version from Package.swift or xcodeproj
- Bundle identifier from Configuration-{bundleId}.{env}.plist filenames or xcodeproj
- Deployment target from xcodeproj or Package.swift
- Dependencies from Package.swift, Podfile, Cartfile, xcodeproj SPM refs, or vendored xcframeworks
- Local Swift packages as modules with inter-package dependency graph
- Targets from xcodeproj (via pbxproj package)
- Feature domains from Sources/ subdirectory names
- Build configurations (DEV/STG/TEST/Release) as build_variants
- Entitlements capabilities (non-sensitive keys) from *.entitlements
- Base URLs from per-environment configuration plists as integration_notes
- CI system from .github/workflows/, azure-pipelines.yml, or .gitlab-ci.yml
"""

from __future__ import annotations

import plistlib
import re
from datetime import UTC, datetime
from pathlib import Path

import structlog

from atlas import __version__
from atlas.extractors.base import Extractor
from atlas.schema import (
    ApiCall,
    ApiContract,
    Dependency,
    EntryPoint,
    ModuleInfo,
    ServiceManifest,
    ServiceYaml,
)

logger = structlog.get_logger()


class IOSExtractor(Extractor):
    """Extractor for iOS (Swift/Objective-C) repositories."""

    type = "ios"

    def extract(self, repo_path: Path, service_yaml: ServiceYaml) -> ServiceManifest:
        """Extract metadata from an iOS repo."""
        # Respect extractor_hints.project_root
        effective_root = repo_path
        if service_yaml.extractor_hints and service_yaml.extractor_hints.project_root:
            effective_root = repo_path / service_yaml.extractor_hints.project_root

        # Resolve target hint — used to scope extraction in monorepos where a single
        # .xcodeproj contains multiple app targets (e.g. FanApp + StaffApp).
        target_hint: str | None = (
            service_yaml.extractor_hints.target
            if service_yaml.extractor_hints
            else None
        )

        language = self._detect_language(effective_root)
        swift_version = self._detect_swift_version(effective_root)
        bundle_id = self._find_bundle_id(effective_root, target_hint=target_hint)
        deployment_target = self._find_deployment_target(effective_root)
        dependencies = self._parse_dependencies(effective_root)
        # Categorize dependencies
        for dep in dependencies:
            if dep.category is None:
                dep.category = self._categorize_dependency(dep)
        modules = self._parse_local_packages(effective_root)
        targets = self._parse_targets(effective_root, target_hint=target_hint)
        build_variants = self._parse_build_configurations(effective_root, target_hint=target_hint)
        entitlements = self._parse_entitlements(effective_root, target_hint=target_hint)
        integration_notes = self._extract_env_urls(effective_root, target_hint=target_hint)
        ci = self._detect_ci(repo_path)
        api_contracts = self.find_api_contracts(effective_root)
        database_type = self._detect_database_type(dependencies, effective_root)
        framework = self._detect_framework(effective_root)
        # Scope api_calls to the target directory when a target hint is set,
        # so monorepo apps (fan-app-ios vs staff-app-ios) don't share call lists.
        # Also scan shared component directories (e.g. CommonComponents/).
        api_calls: list[ApiCall] = []
        if target_hint:
            target_dir = self._resolve_target_dir(effective_root, target_hint)
            if target_dir and target_dir.is_dir():
                api_calls = self._parse_api_calls(target_dir)
            # Also scan shared/common directories alongside the target directory
            for shared_dir in self._find_shared_component_dirs(effective_root, target_hint):
                api_calls.extend(self._parse_api_calls(shared_dir))
        else:
            api_calls = self._parse_api_calls(effective_root)

        # Build entry points: xcodeproj targets + feature domains from Sources/
        entry_points: list[EntryPoint] = []
        for target in targets:
            entry_points.append(EntryPoint(kind="target", ref=target))
        for domain in self._parse_feature_domains(effective_root, target_hint=target_hint):
            entry_points.append(EntryPoint(kind="feature", ref=domain))

        # Merge config-level integration notes with extracted env URL notes
        config_notes = (
            [{"scope": n.scope, "note": n.note} for n in service_yaml.integration_notes]
            if service_yaml.integration_notes
            else []
        )
        all_notes = config_notes + integration_notes

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
            language_version=swift_version,
            slack=service_yaml.slack,
            runbook=service_yaml.runbook,
            jira_component=service_yaml.jira_component,
            application_id=bundle_id,
            min_sdk=deployment_target,
            framework=framework,
            database_type=database_type,
            permissions=entitlements,
            modules=modules,
            build_variants=build_variants,
            dependencies=dependencies,
            entry_points=entry_points,
            api_contracts=api_contracts,
            api_calls=api_calls,
            runtime=None,
            ci=ci,
            integration_notes=all_notes,
            extracted_at=datetime.now(UTC),
            extractor_version=__version__,
            source_repo=source_repo,
        )

    def find_api_contracts(self, repo_path: Path) -> list[ApiContract]:
        """Mobile apps typically don't have API contracts."""
        return []

    def _parse_api_calls(self, root: Path) -> list[ApiCall]:
        """Scan Swift source files for networking patterns.

        Detects:
        1. Moya TargetType implementations: var path: String { "/v1/foo" }
           + var method: Moya.Method { .get }
        2. Alamofire router enums: enum Router { case getAccount } with path/method
        3. EndPointProtocol pattern: var relativeURL: String { switch self { ... } }
           + var method: String { ... URLRequestMethod.get.rawValue ... }
        4. URL path string literals matching /v\\d+/.* in networking files
        5. Simple string constants in files under Network/, API/, Services/ directories

        For files with switch-based ``var relativeURL`` and ``var method`` properties,
        the extractor cross-references per-case paths with per-case methods by matching
        case names to produce ``ApiCall(method=X, path=Y)``.

        Focuses on files in Network/, API/, Services/, DataSource/, Remote/, Endpoint/
        directories.  Skips .build/, DerivedData/, Pods/ directories.
        """
        excluded_dirs = {".build", "DerivedData", "Pods", "Carthage", ".git"}
        network_dir_names = {"network", "api", "services", "datasource", "remote",
                             "endpoint", "endpoints"}

        # Pattern: var path: String { return "/..." } or var path: String { "/..." }
        # Also matches var relativeURL: String { ... }
        path_prop_pattern = re.compile(
            r'var\s+(?:path|relativeURL)\s*:\s*String\s*\{[^}]*?(?:return\s+)?["\'](/[^"\']+)["\']',
            re.DOTALL,
        )
        # Pattern: case .getAccount: return "/accounts" (any path starting with /)
        # Uses /[a-z] to avoid matching filesystem paths like "/Users/..."
        case_return_pattern = re.compile(
            r'return\s+["\'](/[a-z][^"\']*)["\']',
        )
        # Pattern for detecting method from Moya-style or plain String method property
        # Handles: var method: Moya.Method { .get }
        #      and var method: String { ... "GET" ... }
        #      and var method: String { ... URLRequestMethod.get.rawValue ... }
        method_prop_pattern = re.compile(
            r'var\s+method\s*:\s*(?:Moya\.)?(?:Method|String)\s*\{[^}]*?'
            r'(?:\.\s*(get|post|put|delete|patch)|["\']([Gg][Ee][Tt]|[Pp][Oo][Ss][Tt]'
            r'|[Pp][Uu][Tt]|[Dd][Ee][Ll][Ee][Tt][Ee]|[Pp][Aa][Tt][Cc][Hh])["\']'
            r'|URLRequestMethod\.\s*(get|post|put|patch|delete)\s*\.rawValue)',
            re.IGNORECASE | re.DOTALL,
        )

        # --- Switch-case cross-reference patterns ---
        # Extract the body of ``var relativeURL: String { <body> }`` (or var path)
        _url_body_re = re.compile(
            r'var\s+(?:path|relativeURL)\s*:\s*String\s*\{(.*?)\n\s*\}',
            re.DOTALL,
        )
        # Extract the body of ``var method: String { <body> }``
        _method_body_re = re.compile(
            r'var\s+method\s*:\s*(?:Moya\.)?(?:Method|String)\s*\{(.*?)\n\s*\}',
            re.DOTALL,
        )
        # Match a case line like ``case .getSomething:`` or ``case .a, .b:``
        _case_name_re = re.compile(
            r'case\s+((?:\.\w+(?:\s*,\s*)?)+)\s*:',
        )
        # Match return value in a case arm: ``return "/path"``
        _case_return_path_re = re.compile(
            r'return\s+["\'](/[^"\']+)["\']',
        )
        # Match return value in a case arm: ``return URLRequestMethod.get.rawValue``
        # or ``return "GET"`` or ``return .get``
        _case_return_method_re = re.compile(
            r'return\s+(?:URLRequestMethod\.\s*(\w+)\s*\.rawValue'
            r'|["\']([Gg][Ee][Tt]|[Pp][Oo][Ss][Tt]|[Pp][Uu][Tt]|[Dd][Ee][Ll][Ee][Tt][Ee]|[Pp][Aa][Tt][Cc][Hh])["\']'
            r'|\.(\w+))',
            re.IGNORECASE,
        )

        # Compiled filter for paths that look like placeholder/variable names rather than
        # real API routes. Rejects paths like /pathToTheInformation or /someCamelCaseThing
        # (single segment, camelCase, no hyphens/underscores — strongly suggests a variable).
        _noise_path_re = re.compile(
            r'^/[a-z][a-zA-Z0-9]*$'   # /camelCase with no separators or sub-segments
        )

        def _is_noise_path(p: str) -> bool:
            """Return True if the path looks like a placeholder, not a real API route."""
            # Single camelCase segment (e.g. /pathToTheInformation)
            if _noise_path_re.match(p):
                return True
            # Contains suspicious placeholder words
            lower = p.lower()
            if any(w in lower for w in ("placeholder", "example", "yourpath", "pathto")):
                return True
            return False

        def _parse_switch_cases(
            body: str,
            value_re: re.Pattern[str],
            *,
            is_method: bool = False,
        ) -> dict[str, str]:
            """Parse a switch body into a mapping of case name → value.

            For multi-case lines like ``case .a, .b:`` all names get the same value.
            ``default:`` arms are skipped.
            """
            mapping: dict[str, str] = {}
            current_cases: list[str] = []
            for line in body.splitlines():
                stripped = line.strip()
                case_m = _case_name_re.match(stripped)
                if case_m:
                    # Extract all dot-prefixed names: ".foo, .bar" → ["foo", "bar"]
                    raw = case_m.group(1)
                    current_cases = [
                        n.strip().lstrip(".")
                        for n in raw.split(",")
                        if n.strip().startswith(".")
                    ]
                    # The return might be on the same line after the colon
                    after_colon = stripped[case_m.end():]
                    val_m = value_re.search(after_colon)
                    if val_m:
                        val = next((g for g in val_m.groups() if g is not None), None)
                        if val:
                            if is_method:
                                val = val.upper()
                            for name in current_cases:
                                mapping[name] = val
                        current_cases = []
                    continue
                if current_cases:
                    val_m = value_re.search(stripped)
                    if val_m:
                        val = next((g for g in val_m.groups() if g is not None), None)
                        if val:
                            if is_method:
                                val = val.upper()
                            for name in current_cases:
                                mapping[name] = val
                        current_cases = []
            return mapping

        api_calls: list[ApiCall] = []
        seen_paths: set[str] = set()

        for swift_file in root.rglob("*.swift"):
            parts = swift_file.parts
            if any(part in excluded_dirs for part in parts):
                continue

            # Focus on network-related files
            is_network_dir = any(part.lower() in network_dir_names for part in parts)
            is_api_file = any(
                swift_file.name.endswith(suffix)
                for suffix in ("Router.swift", "API.swift", "Api.swift", "Target.swift",
                               "NetworkLayer.swift", "APIRouter.swift", "Endpoint.swift",
                               "EndPoint.swift", "Service.swift", "Client.swift")
            )
            # Also include any file that seems to be a router/API definition
            try:
                content = swift_file.read_text(errors="replace")
            except OSError:
                continue

            # Relaxed gate: require "var path" or "var relativeURL" and any "/"
            has_url_prop = "var path" in content or "var relativeURL" in content
            has_path_var = has_url_prop and "/" in content
            if not (is_network_dir or is_api_file or has_path_var):
                continue

            interface_name = swift_file.stem

            # --- Try switch-case cross-reference for per-case method+path ---
            url_body_m = _url_body_re.search(content)
            method_body_m = _method_body_re.search(content)

            if url_body_m and method_body_m:
                path_map = _parse_switch_cases(
                    url_body_m.group(1), _case_return_path_re
                )
                method_map = _parse_switch_cases(
                    method_body_m.group(1), _case_return_method_re, is_method=True
                )
                if path_map:
                    # Cross-reference: join on case name
                    for case_name, raw_path in path_map.items():
                        path = re.sub(r'\\\([^)]*\)', '{param}', raw_path)
                        if path not in seen_paths and not _is_noise_path(path):
                            seen_paths.add(path)
                            method = method_map.get(case_name)
                            api_calls.append(
                                ApiCall(
                                    method=method,
                                    path=path,
                                    interface_name=interface_name,
                                )
                            )
                    # Skip the legacy path extraction for this file — already handled
                    continue

            # --- Fallback: legacy single-method detection ---
            method: str | None = None
            method_m = method_prop_pattern.search(content)
            if method_m:
                # Group 1: .get/.post style; group 2: "GET" string; group 3: URLRequestMethod
                raw_method = method_m.group(1) or method_m.group(2) or method_m.group(3)
                if raw_method:
                    method = raw_method.upper()

            # Find paths from var path / var relativeURL { ... } pattern
            for m in path_prop_pattern.finditer(content):
                path = m.group(1)
                # Treat Swift string interpolation \(...) like a path parameter {param}
                path = re.sub(r'\\\([^)]*\)', '{param}', path)
                if path not in seen_paths and not _is_noise_path(path):
                    seen_paths.add(path)
                    api_calls.append(
                        ApiCall(
                            method=method,
                            path=path,
                            interface_name=interface_name,
                        )
                    )

            # Find paths from case return statements (router pattern)
            for m in case_return_pattern.finditer(content):
                path = m.group(1)
                # Treat Swift string interpolation \(...) like a path parameter {param}
                path = re.sub(r'\\\([^)]*\)', '{param}', path)
                if path not in seen_paths and not _is_noise_path(path):
                    api_calls.append(
                        ApiCall(
                            method=None,  # method context not easily inferable per-case
                            path=path,
                            interface_name=interface_name,
                        )
                    )

        return api_calls

    # --- Database, framework, and dependency categorization ---

    # Well-known iOS dependency categories
    _DEPENDENCY_CATEGORIES: dict[str, str] = {
        # Networking
        "alamofire": "networking",
        "moya": "networking",
        "urlsession": "networking",
        "grpc-swift": "networking",
        "starscream": "networking",
        "socket.io-client-swift": "networking",
        # Database
        "realm": "database",
        "realmswift": "database",
        "realm-swift": "database",
        "grdb": "database",
        "grdb.swift": "database",
        "sqlite.swift": "database",
        "corestore": "database",
        "swiftdata": "database",
        # Analytics / Observability
        "firebase": "analytics",
        "firebase/analytics": "analytics",
        "firebaseanalytics": "analytics",
        "firebase/crashlytics": "analytics",
        "dd-sdk-ios": "analytics",
        "datadogcore": "analytics",
        "datadogrumswift": "analytics",
        "datadogrum": "analytics",
        "datadoglogs": "analytics",
        "datadogtrace": "analytics",
        "datadogcrashreporting": "analytics",
        "aepsdk-core-ios": "analytics",
        "aepsdk-analytics-ios": "analytics",
        "aepsdk-places-ios": "analytics",
        "amplitude-ios": "analytics",
        "mixpanel-swift": "analytics",
        # UI
        "lottie": "ui",
        "lottie-ios": "ui",
        "sdwebimage": "ui",
        "sdwebimageswiftui": "ui",
        "kingfisher": "ui",
        "snapkit": "ui",
        "swiftui-introspect": "ui",
        "nuke": "ui",
        "hero": "ui",
        "icarousel": "ui",
        # Auth
        "microsoft-authentication-library-for-objc": "auth",
        "msal": "auth",
        "jwtdecode.swift": "auth",
        "jwtdecode": "auth",
        "appauth-ios": "auth",
        # Maps
        "mapsindoors-googlemaps-ios": "maps",
        "googlemaps": "maps",
        "mapbox-maps-ios": "maps",
        # Media
        "nbavideokit-ios": "media",
        "avkit": "media",
        # Security
        "kount-ios-swift-package": "security",
        "cryptoswift": "security",
        # Testing
        "quick": "testing",
        "nimble": "testing",
        "ohhttpstubs": "testing",
        "snapshotting": "testing",
        "swift-snapshot-testing": "testing",
        # Lint / tooling
        "swiftlint": "tooling",
        "swiftformat": "tooling",
    }

    # Database libraries that map to database_type
    _DATABASE_LIBS: dict[str, str] = {
        "realm": "realm",
        "realmswift": "realm",
        "realm-swift": "realm",
        "grdb": "sqlite",
        "grdb.swift": "sqlite",
        "sqlite.swift": "sqlite",
        "corestore": "coredata",
    }

    def _detect_database_type(
        self, dependencies: list[Dependency], root: Path
    ) -> str | None:
        """Detect the primary local database from dependencies and import patterns.

        Priority:
        1. Known database libraries in the dependency list
        2. ``import RealmSwift``, ``import CoreData``, ``import SwiftData`` in source files
        3. ``.xcdatamodeld`` bundle presence → CoreData
        """
        # 1. Check dependency names
        for dep in dependencies:
            db_type = self._DATABASE_LIBS.get(dep.name.lower())
            if db_type:
                return db_type

        # 2. Check source imports
        excluded = {".build", "DerivedData", "Pods", "Carthage", ".git"}
        import_db_map = {
            "import RealmSwift": "realm",
            "import Realm": "realm",
            "import CoreData": "coredata",
            "import SwiftData": "swiftdata",
            "import GRDB": "sqlite",
        }
        for swift_file in root.rglob("*.swift"):
            if any(p in excluded for p in swift_file.parts):
                continue
            try:
                content = swift_file.read_text(errors="replace")
            except OSError:
                continue
            for import_str, db_type in import_db_map.items():
                if import_str in content:
                    return db_type

        # 3. Check for .xcdatamodeld (CoreData model file)
        for _ in root.rglob("*.xcdatamodeld"):
            return "coredata"

        return None

    def _detect_framework(self, root: Path) -> str | None:
        """Detect the primary UI framework by counting import statements.

        Returns ``"swiftui"`` if SwiftUI imports dominate, ``"uikit"`` if UIKit
        dominates, or ``"swiftui+uikit"`` if both are present in significant
        numbers (neither below 20% of total).
        """
        excluded = {".build", "DerivedData", "Pods", "Carthage", ".git",
                    "Tests", "UITests"}
        swiftui_count = 0
        uikit_count = 0

        for swift_file in root.rglob("*.swift"):
            if any(p in excluded for p in swift_file.parts):
                continue
            try:
                content = swift_file.read_text(errors="replace")
            except OSError:
                continue
            if "import SwiftUI" in content:
                swiftui_count += 1
            if "import UIKit" in content:
                uikit_count += 1

        total = swiftui_count + uikit_count
        if total == 0:
            return None

        # Both significant (neither below 20% of total)
        if swiftui_count > 0 and uikit_count > 0:
            min_ratio = min(swiftui_count, uikit_count) / total
            if min_ratio >= 0.2:
                return "swiftui+uikit"

        if swiftui_count >= uikit_count:
            return "swiftui"
        return "uikit"

    def _categorize_dependency(self, dep: Dependency) -> str | None:
        """Return the category for a dependency based on well-known library names."""
        return self._DEPENDENCY_CATEGORIES.get(dep.name.lower())

    # --- Private parsing methods ---

    def _detect_language(self, root: Path) -> str:
        """Detect primary language by counting .swift vs .m/.h files."""
        swift_count = len(list(root.rglob("*.swift")))
        objc_count = len(list(root.rglob("*.m"))) + len(list(root.rglob("*.h")))

        if swift_count >= objc_count:
            return "swift"
        return "objective-c"

    def _detect_swift_version(self, root: Path) -> str | None:
        """Detect Swift version from multiple sources (highest priority first):

        1. .xcode-version file — specifies the Xcode version, which maps to Swift
        2. Package.swift swiftLanguageVersions([.v5]) — explicit Swift language version
        3. xcodeproj SWIFT_VERSION build setting via pbxproj
        4. Package.swift swift-tools-version comment (least reliable — often older)
        """
        # 1. .xcode-version file: "16.2" → Xcode 16.x uses Swift 6.x, 15.x → Swift 5.9/5.10
        xcode_ver_file = root / ".xcode-version"
        if xcode_ver_file.exists():
            try:
                raw = xcode_ver_file.read_text().strip()
                # Parse major.minor from strings like "16.2" or "16.2.1"
                xm = re.match(r"(\d+)\.(\d+)", raw)
                if xm:
                    xcode_major = int(xm.group(1))
                    xcode_minor = int(xm.group(2))
                    # Xcode 16+ ships Swift 6.x; Xcode 15 ships Swift 5.9/5.10
                    if xcode_major >= 16:
                        return f"6.{xcode_minor}"
                    elif xcode_major == 15:
                        return "5.10" if xcode_minor >= 3 else "5.9"
                    elif xcode_major == 14:
                        return "5.7" if xcode_minor == 0 else "5.8"
            except OSError:
                pass

        # 2. Package.swift swiftLanguageVersions([.v5]) or .v6
        package_swift = root / "Package.swift"
        if package_swift.exists():
            try:
                content = package_swift.read_text()
                # swiftLanguageVersions: [.v5, .v6] or [.version("5.9")]
                slv_m = re.search(
                    r'swiftLanguageVersions\s*:\s*\[([^\]]+)\]', content
                )
                if slv_m:
                    inner = slv_m.group(1)
                    # Look for highest .vN or .version("N.x")
                    versions = re.findall(r'\.v(\d+)', inner)
                    ver_strings = re.findall(r'\.version\s*\(\s*["\'](\d+[\d.]*)["\']', inner)
                    all_vers = versions + ver_strings
                    if all_vers:
                        return sorted(all_vers, reverse=True)[0]
            except OSError:
                pass

        # 3. xcodeproj SWIFT_VERSION build setting via pbxproj
        swift_ver = self._swift_version_from_xcodeproj(root)
        if swift_ver:
            return swift_ver

        # 4. Package.swift swift-tools-version (fallback — often older than actual Swift ver)
        if package_swift.exists():
            try:
                content = package_swift.read_text()
                m = re.search(r"swift-tools-version:\s*([\d.]+)", content)
                if m:
                    return m.group(1)
            except OSError:
                pass

        return None

    def _swift_version_from_xcodeproj(self, root: Path) -> str | None:
        """Try to get Swift version from xcodeproj build settings."""
        try:
            from pbxproj import XcodeProject
        except ImportError:
            logger.debug("pbxproj not installed, skipping xcodeproj Swift version detection")
            return None

        for xcodeproj_dir in root.glob("*.xcodeproj"):
            pbxproj_path = xcodeproj_dir / "project.pbxproj"
            if pbxproj_path.exists():
                try:
                    project = XcodeProject.load(str(pbxproj_path))
                    for obj in project.objects.get_objects_in_section("XCBuildConfiguration"):
                        build_settings = getattr(obj, "buildSettings", None)
                        if build_settings and "SWIFT_VERSION" in build_settings:
                            return build_settings["SWIFT_VERSION"]
                except Exception:
                    logger.debug(
                        "Failed to parse xcodeproj for Swift version", path=str(pbxproj_path)
                    )
                    continue
        return None

    def _find_bundle_id(self, root: Path, target_hint: str | None = None) -> str | None:
        """Find bundle identifier from Configuration-{bundleId}.{env}.plist filenames,
        Info.plist, or xcodeproj build settings.

        When target_hint is provided, only Configuration plists whose bundle ID segment
        contains the target_hint name (case-insensitive) are considered, allowing
        monorepos with multiple app targets to resolve the correct bundle ID.
        """
        # Pattern: Configuration-{bundleId}.{env}.plist — most reliable for multi-env apps.
        # Collect all candidates and prefer the one whose PROD plist (no env suffix) exists,
        # falling back to the lexicographically first candidate.
        env_labels = {"dev", "stg", "qa", "uat", "test", "debug", "release", "prod"}
        candidates: dict[str, bool] = {}  # bundle_id -> has_prod_plist

        # When target_hint is set, derive keywords to match against bundle ID segments.
        # e.g. "LACStaff" → ["lacstaff", "staff"] so it matches "com.laclippers.staffapp"
        # via the "staff" keyword.
        bundle_keywords: list[str] | None = (
            self._bundle_id_keywords(target_hint) if target_hint else None
        )

        for plist_path in root.rglob("Configuration-*.plist"):
            stem = plist_path.stem  # e.g. "Configuration-com.laclippers.fanapp.dev"
            inner = stem[len("Configuration-"):]  # e.g. "com.laclippers.fanapp.dev"
            parts = inner.split(".")
            if len(parts) < 2 or " " in inner:
                continue
            if parts[-1].lower() in env_labels:
                candidate = ".".join(parts[:-1])
                has_prod = False
            else:
                # No env suffix → this IS the prod plist, inner is the bare bundle ID
                candidate = inner
                has_prod = True
            if "." in candidate:
                # When target_hint is set, filter to plists whose bundle ID contains
                # at least one of the target keywords (e.g. "staff" in "com.laclippers.staffapp")
                if bundle_keywords and not any(kw in candidate.lower() for kw in bundle_keywords):
                    continue
                existing = candidates.get(candidate, False)
                candidates[candidate] = existing or has_prod

        if candidates:
            # Prefer bundle IDs that have a prod plist; among those, pick shortest (main app)
            prod_ids = [bid for bid, has_prod in candidates.items() if has_prod]
            if prod_ids:
                return min(prod_ids, key=len)
            return min(candidates.keys(), key=len)

        # Try Info.plist files
        for plist_path in root.rglob("Info.plist"):
            try:
                with open(plist_path, "rb") as f:
                    plist = plistlib.load(f)
                bundle_id = plist.get("CFBundleIdentifier")
                if bundle_id and not bundle_id.startswith("$("):
                    return bundle_id
            except Exception:
                continue

        # Try xcodeproj build settings
        try:
            from pbxproj import XcodeProject
        except ImportError:
            return None

        for xcodeproj_dir in root.glob("*.xcodeproj"):
            pbxproj_path = xcodeproj_dir / "project.pbxproj"
            if pbxproj_path.exists():
                try:
                    project = XcodeProject.load(str(pbxproj_path))
                    for obj in project.objects.get_objects_in_section("XCBuildConfiguration"):
                        build_settings = getattr(obj, "buildSettings", None)
                        if build_settings and "PRODUCT_BUNDLE_IDENTIFIER" in build_settings:
                            bid = build_settings["PRODUCT_BUNDLE_IDENTIFIER"]
                            if not bid.startswith("$("):
                                return bid
                except Exception:
                    continue

        return None

    def _find_deployment_target(self, root: Path) -> str | None:
        """Find deployment target from Package.swift or xcodeproj."""
        # Try Package.swift platforms
        package_swift = root / "Package.swift"
        if package_swift.exists():
            content = package_swift.read_text()
            # Match .iOS(.v16) or .iOS("16.0") or .macOS(.v13)
            m = re.search(r"\.iOS\(\s*\.v(\d+)\s*\)", content)
            if m:
                return f"iOS {m.group(1)}.0"
            m = re.search(r'\.iOS\(\s*["\'](\d+\.\d+)["\']', content)
            if m:
                return f"iOS {m.group(1)}"

        # Try xcodeproj
        try:
            from pbxproj import XcodeProject
        except ImportError:
            return None

        for xcodeproj_dir in root.glob("*.xcodeproj"):
            pbxproj_path = xcodeproj_dir / "project.pbxproj"
            if pbxproj_path.exists():
                try:
                    project = XcodeProject.load(str(pbxproj_path))
                    for obj in project.objects.get_objects_in_section("XCBuildConfiguration"):
                        build_settings = getattr(obj, "buildSettings", None)
                        if build_settings and "IPHONEOS_DEPLOYMENT_TARGET" in build_settings:
                            return f"iOS {build_settings['IPHONEOS_DEPLOYMENT_TARGET']}"
                except Exception:
                    continue

        return None

    def _parse_dependencies(self, root: Path) -> list[Dependency]:
        """Parse dependencies from Package.swift, Podfile, Cartfile, xcodeproj SPM refs,
        and vendored xcframeworks."""
        deps: list[Dependency] = []
        seen: set[str] = set()

        # Parse Package.swift (root-level only — local packages handled by _parse_local_packages)
        self._parse_spm_deps(root, deps, seen)

        # Parse Podfile
        self._parse_podfile_deps(root, deps, seen)

        # Parse Cartfile
        self._parse_cartfile_deps(root, deps, seen)

        # Parse SPM deps from xcodeproj (XCRemoteSwiftPackageReference)
        self._parse_xcodeproj_spm_deps(root, deps, seen)

        # Parse vendored xcframeworks
        self._parse_vendored_frameworks(root, deps, seen)

        return deps

    def _parse_spm_deps(self, root: Path, deps: list[Dependency], seen: set[str]) -> None:
        """Parse Swift Package Manager dependencies from Package.swift."""
        package_swift = root / "Package.swift"
        if not package_swift.exists():
            return

        content = package_swift.read_text()
        # Match .package(url: "https://github.com/...", from: "1.0.0")
        # or .package(url: "...", .upToNextMajor(from: "1.0"))
        for m in re.finditer(
            r'\.package\(\s*url:\s*["\']([^"\']+)["\'].*?(?:from:\s*["\']([^"\']*)["\'])?',
            content,
            re.DOTALL,
        ):
            url = m.group(1)
            version = m.group(2)
            # Extract name from URL
            name = url.rstrip("/").split("/")[-1]
            if name.endswith(".git"):
                name = name[:-4]

            if name not in seen:
                seen.add(name)
                deps.append(
                    Dependency(
                        name=name,
                        version=version,
                        source="Package.swift",
                        direct=True,
                    )
                )

    def _parse_podfile_deps(self, root: Path, deps: list[Dependency], seen: set[str]) -> None:
        """Parse CocoaPods dependencies from Podfile."""
        podfile = root / "Podfile"
        if not podfile.exists():
            return

        content = podfile.read_text()
        # Match: pod 'PodName', '~> 1.0'
        for m in re.finditer(r"pod\s+['\"]([^'\"]+)['\"](?:\s*,\s*['\"]([^'\"]*)['\"])?", content):
            name = m.group(1)
            version = m.group(2)

            if name not in seen:
                seen.add(name)
                deps.append(
                    Dependency(
                        name=name,
                        version=version,
                        source="Podfile",
                        direct=True,
                    )
                )

    def _parse_cartfile_deps(self, root: Path, deps: list[Dependency], seen: set[str]) -> None:
        """Parse Carthage dependencies from Cartfile."""
        cartfile = root / "Cartfile"
        if not cartfile.exists():
            return

        content = cartfile.read_text()
        # Match: github "owner/repo" ~> 1.0
        for m in re.finditer(
            r'(?:github|git|binary)\s+["\']([^"\']+)["\'](?:\s+[~>=<]*\s*["\']?([^"\'\s]+)["\']?)?',
            content,
        ):
            repo_ref = m.group(1)
            version = m.group(2)
            name = repo_ref.split("/")[-1] if "/" in repo_ref else repo_ref

            if name not in seen:
                seen.add(name)
                deps.append(
                    Dependency(
                        name=name,
                        version=version,
                        source="Cartfile",
                        direct=True,
                    )
                )

    def _parse_xcodeproj_spm_deps(
        self, root: Path, deps: list[Dependency], seen: set[str]
    ) -> None:
        """Parse SPM remote dependencies from xcodeproj XCRemoteSwiftPackageReference entries."""
        try:
            from pbxproj import XcodeProject
        except ImportError:
            return

        for xcodeproj_dir in root.glob("*.xcodeproj"):
            pbxproj_path = xcodeproj_dir / "project.pbxproj"
            if not pbxproj_path.exists():
                continue
            try:
                project = XcodeProject.load(str(pbxproj_path))
                for obj in project.objects.get_objects_in_section(
                    "XCRemoteSwiftPackageReference"
                ):
                    url = getattr(obj, "repositoryURL", None)
                    if not url:
                        continue
                    # Strip embedded credentials from URL before storing
                    url = re.sub(r"(https?://)([^@]+@)", r"\1", url)
                    # Extract version from requirement dict
                    requirement = getattr(obj, "requirement", None)
                    version = None
                    if requirement:
                        version = getattr(requirement, "version", None) or getattr(
                            requirement, "minimumVersion", None
                        )
                    # Derive name from URL path
                    name = url.rstrip("/").split("/")[-1]
                    if name.endswith(".git"):
                        name = name[:-4]
                    if name not in seen:
                        seen.add(name)
                        deps.append(
                            Dependency(
                                name=name,
                                version=str(version) if version else None,
                                source="xcodeproj",
                                direct=True,
                            )
                        )
            except Exception:
                logger.debug(
                    "Failed to parse xcodeproj SPM deps", path=str(pbxproj_path)
                )
                continue

    def _parse_vendored_frameworks(
        self, root: Path, deps: list[Dependency], seen: set[str]
    ) -> None:
        """Parse vendored binary xcframeworks as dependencies."""
        for xcfw in root.rglob("*.xcframework"):
            # Skip SPM build cache and Xcode DerivedData
            parts = xcfw.parts
            if any(p in ("DerivedData", ".build", "checkouts") for p in parts):
                continue
            name = xcfw.stem  # e.g. "Realm" from "Realm.xcframework"
            if name not in seen:
                seen.add(name)
                deps.append(
                    Dependency(
                        name=name,
                        version=None,
                        source="vendored-xcframework",
                        direct=True,
                    )
                )

    def _parse_local_packages(self, root: Path) -> list[ModuleInfo]:
        """Parse local Swift packages as modules with inter-package dependency graph.

        Scans for Package.swift files in subdirectories, extracts product names,
        and resolves relative-path .package(path:) references to build the graph.
        """
        modules: list[ModuleInfo] = []
        # Map from resolved absolute path → module name for dependency resolution
        path_to_name: dict[Path, str] = {}
        raw: list[tuple[Path, str, list[Path]]] = []  # (package_dir, name, dep_paths)

        for pkg_swift in root.rglob("Package.swift"):
            pkg_dir = pkg_swift.parent
            # Skip root-level Package.swift (not a local module)
            if pkg_dir == root:
                continue
            # Skip demo/test/example packages
            parts = pkg_dir.parts
            if any(p.lower() in ("demos", "demo", "example", "examples") for p in parts):
                continue
            try:
                content = pkg_swift.read_text()
            except Exception:
                continue

            # Extract package name from first product declaration
            name_match = re.search(
                r'\.library\(\s*name:\s*["\']([^"\']+)["\']', content
            ) or re.search(
                r'\.executable\(\s*name:\s*["\']([^"\']+)["\']', content
            )
            if not name_match:
                # Fall back to directory name
                name = pkg_dir.name
            else:
                name = name_match.group(1)

            path_to_name[pkg_dir.resolve()] = name

            # Collect relative .package(path: "...") references
            dep_paths: list[Path] = []
            for m in re.finditer(r'\.package\(\s*path:\s*["\']([^"\']+)["\']', content):
                dep_paths.append((pkg_dir / m.group(1)).resolve())

            raw.append((pkg_dir, name, dep_paths))

        # Second pass: resolve path references to module names
        for pkg_dir, name, dep_paths in raw:
            dep_names = []
            for dep_path in dep_paths:
                dep_name = path_to_name.get(dep_path)
                if dep_name:
                    dep_names.append(dep_name)
                else:
                    # Fallback: use directory name
                    dep_names.append(dep_path.name)

            modules.append(ModuleInfo(name=name, type="library", dependencies=dep_names))

        return modules

    def _parse_targets(self, root: Path, target_hint: str | None = None) -> list[str]:
        """Parse targets from xcodeproj via pbxproj, or fall back to Package.swift.

        When target_hint is provided, only the matching target (case-insensitive) is
        returned, allowing monorepos to expose a single app's targets.
        """
        targets: list[str] = []

        # Try pbxproj
        try:
            from pbxproj import XcodeProject
        except ImportError:
            logger.debug("pbxproj not installed, trying Package.swift for targets")
            return self._targets_from_package_swift(root)

        for xcodeproj_dir in root.glob("*.xcodeproj"):
            pbxproj_path = xcodeproj_dir / "project.pbxproj"
            if pbxproj_path.exists():
                try:
                    project = XcodeProject.load(str(pbxproj_path))
                    for obj in project.objects.get_objects_in_section("PBXNativeTarget"):
                        target_name = getattr(obj, "name", None)
                        if target_name and target_name not in targets:
                            targets.append(target_name)
                except Exception:
                    logger.debug("Failed to parse xcodeproj for targets", path=str(pbxproj_path))
                    continue

        if not targets:
            targets = self._targets_from_package_swift(root)

        if target_hint:
            target_lower = target_hint.lower()
            targets = [t for t in targets if target_lower in t.lower()]

        return targets

    def _targets_from_package_swift(self, root: Path) -> list[str]:
        """Extract target names from Package.swift."""
        package_swift = root / "Package.swift"
        if not package_swift.exists():
            return []

        content = package_swift.read_text()
        targets: list[str] = []
        # Match .target(name: "MyTarget") or .executableTarget(name: "MyApp")
        for m in re.finditer(
            r'\.(?:target|executableTarget|testTarget)\(\s*name:\s*["\']([^"\']+)["\']', content
        ):
            name = m.group(1)
            if name not in targets:
                targets.append(name)
        return targets

    def _parse_build_configurations(
        self, root: Path, target_hint: str | None = None
    ) -> list[str]:
        """Extract build configuration names from xcodeproj (e.g. DEV, STG, TEST, Release).

        When target_hint is set, derives build variants from the entitlements filenames
        for the matching target directory instead of the full xcodeproj list, since a
        shared xcodeproj contains configs for all targets.
        """
        # If a target is scoped, infer variants from *.entitlements filenames in the
        # target's source directory (e.g. LaClippersDEV.entitlements → DEV).
        if target_hint:
            target_dir = self._resolve_target_dir(root, target_hint)
            if target_dir and target_dir.is_dir():
                variants: list[str] = []
                target_lower = target_hint.lower()
                for ent_path in target_dir.rglob("*.entitlements"):
                    stem = ent_path.stem  # e.g. "LaClippersDEV"
                    # Strip the target prefix (case-insensitive) to get the variant suffix
                    stem_lower = stem.lower()
                    if stem_lower.startswith(target_lower):
                        suffix = stem[len(target_hint):]  # e.g. "DEV", "STG"
                    else:
                        suffix = stem
                    if suffix and suffix not in variants:
                        variants.append(suffix)
                if variants:
                    return sorted(variants)

        try:
            from pbxproj import XcodeProject
        except ImportError:
            return []

        configs: list[str] = []
        for xcodeproj_dir in root.glob("*.xcodeproj"):
            pbxproj_path = xcodeproj_dir / "project.pbxproj"
            if not pbxproj_path.exists():
                continue
            try:
                project = XcodeProject.load(str(pbxproj_path))
                for obj in project.objects.get_objects_in_section("XCBuildConfiguration"):
                    name = getattr(obj, "name", None)
                    if name and name not in configs:
                        configs.append(name)
            except Exception:
                logger.debug(
                    "Failed to parse xcodeproj for build configs", path=str(pbxproj_path)
                )
                continue
        return sorted(set(configs))

    def _bundle_id_keywords(self, target_hint: str) -> list[str]:
        """Derive bundle-ID match keywords from a target directory name.

        Bundle IDs often use an abbreviated or suffix-only form of the target name.
        For example:
          - "LaClippers" → ["laclippers", "clippers", "fanapp"]  (no, just camel-split)
          - "LACStaff"   → ["lacstaff", "staff"]
          - "FanApp"     → ["fanapp", "fan"]

        Strategy: return the full lowercased name plus each camelCase component
        longer than 3 characters so that any one of them is sufficient to identify
        the correct bundle ID segment.
        """
        name = target_hint
        keywords = [name.lower()]
        # Split on camelCase boundaries
        parts = re.findall(r"[A-Z][a-z0-9]*|[a-z0-9]+", name)
        for part in parts:
            kw = part.lower()
            if len(kw) > 3 and kw not in keywords:
                keywords.append(kw)
        return keywords

    def _find_shared_component_dirs(
        self, root: Path, target_hint: str
    ) -> list[Path]:
        """Find shared/common component directories that should be scanned alongside
        a target-scoped directory.

        Returns directories containing "Common" or "Shared" in their name,
        excluding other target directories and build artifacts.
        """
        shared_keywords = {"common", "shared"}
        excluded = {".build", "deriveddata", "pods", "carthage", ".git"}
        target_lower = target_hint.lower()

        shared_dirs: list[Path] = []
        for child in root.iterdir():
            if not child.is_dir():
                continue
            child_lower = child.name.lower()
            # Skip excluded dirs
            if child_lower in excluded:
                continue
            # Skip the target dir itself (already scanned)
            if child_lower == target_lower or child_lower.startswith(target_lower):
                continue
            # Include if name contains a shared keyword
            if any(kw in child_lower for kw in shared_keywords):
                shared_dirs.append(child)
                continue
            # Also search one level deeper for CommonComponents-style dirs
            try:
                for grandchild in child.iterdir():
                    if grandchild.is_dir() and any(
                        kw in grandchild.name.lower() for kw in shared_keywords
                    ):
                        shared_dirs.append(grandchild)
            except OSError:
                continue

        return shared_dirs

    def _resolve_target_dir(self, root: Path, target_hint: str) -> Path | None:
        """Resolve the source directory for a given target name.

        Looks for a direct child of root whose name matches target_hint
        (case-insensitive exact match, then case-insensitive prefix match).
        Returns the resolved Path or None if not found.
        """
        target_lower = target_hint.lower()
        exact: Path | None = None
        prefix: Path | None = None
        for child in root.iterdir():
            if not child.is_dir():
                continue
            child_lower = child.name.lower()
            if child_lower == target_lower:
                exact = child
                break
            if prefix is None and child_lower.startswith(target_lower):
                prefix = child
        return exact or prefix

    def _parse_feature_domains(self, root: Path, target_hint: str | None = None) -> list[str]:
        """Infer feature domains from Sources/ subdirectory names under any target directory.

        Looks for Sources/ directories inside known target-like directories (e.g. LaClippers/,
        LACStaff/) and returns their direct subdirectory names as feature domains.
        Names with spaces are normalised (spaces replaced with underscores).
        Generic names that don't represent real features are excluded.

        When target_hint is provided, only the matching target directory is scanned.
        """
        generic = {"Common", "Component", "Data", "UIComponent", "Sources"}
        domains: list[str] = []
        seen: set[str] = set()

        # Determine which directories to walk
        if target_hint:
            target_dir = self._resolve_target_dir(root, target_hint)
            candidates = [target_dir] if target_dir and target_dir.is_dir() else []
        else:
            # Walk direct children of root that look like target dirs
            candidates = [child for child in root.iterdir() if child.is_dir()]

        for child in candidates:
            sources_dir = child / "Sources"
            if not sources_dir.is_dir():
                continue
            for feature_dir in sources_dir.iterdir():
                if not feature_dir.is_dir():
                    continue
                # Normalise spaces away; use the no-space variant as canonical key
                raw = feature_dir.name
                normalised = raw.replace(" ", "").replace("_", "")
                if normalised in generic or normalised in seen:
                    continue
                seen.add(normalised)
                # Prefer the name without spaces or underscores if it differs
                name = raw.replace(" ", "")
                domains.append(name)

        return sorted(domains)

    def _parse_entitlements(self, root: Path, target_hint: str | None = None) -> list[str]:
        """Parse entitlement keys from *.entitlements plist files (non-sensitive only).

        When target_hint is provided, only entitlements files inside the matching
        target directory are parsed (e.g. LaClippers/ for target="LaClippers").
        """
        entitlement_keys: list[str] = []
        sensitive_keys = {
            "com.apple.developer.associated-domains",
            "keychain-access-groups",
        }

        search_root = root
        if target_hint:
            target_dir = self._resolve_target_dir(root, target_hint)
            if target_dir and target_dir.is_dir():
                search_root = target_dir

        for ent_path in search_root.rglob("*.entitlements"):
            try:
                with open(ent_path, "rb") as f:
                    plist = plistlib.load(f)
                for key in plist:
                    if key not in sensitive_keys and key not in entitlement_keys:
                        entitlement_keys.append(key)
            except Exception:
                logger.debug("Failed to parse entitlements", path=str(ent_path))
                continue

        return entitlement_keys

    def _extract_env_urls(self, root: Path, target_hint: str | None = None) -> list[dict]:
        """Extract BASE_URL values from per-environment configuration plists.

        Returns integration_notes with scope='env:{env}' and note='BASE_URL: {url}'.
        Capped at 10 notes to respect the schema limit.

        When target_hint is provided, only Configuration plists whose filename contains
        the target_hint name (case-insensitive) are considered.
        """
        notes: list[dict] = []
        seen_urls: set[str] = set()

        bundle_keywords: list[str] | None = (
            self._bundle_id_keywords(target_hint) if target_hint else None
        )
        env_labels = {"dev", "stg", "qa", "uat", "test", "debug", "release", "prod"}
        for plist_path in sorted(root.rglob("Configuration-*.plist")):
            # Derive env label from filename: Configuration-com.bundle.id.env.plist
            stem = plist_path.stem  # e.g. "Configuration-com.laclippers.fanapp.dev"
            inner = stem[len("Configuration-"):]

            # When target_hint is set, skip plists that don't match this target
            if bundle_keywords and not any(kw in inner.lower() for kw in bundle_keywords):
                continue

            last = inner.split(".")[-1].lower()
            env_label = last.upper() if last in env_labels else "PROD"

            try:
                with open(plist_path, "rb") as f:
                    data = plistlib.load(f)
            except Exception:
                continue

            base_url = data.get("BASE_URL")
            if base_url and isinstance(base_url, str) and base_url not in seen_urls:
                seen_urls.add(base_url)
                notes.append({"scope": f"env:{env_label}", "note": f"BASE_URL: {base_url}"})
                if len(notes) >= 10:
                    break

        return notes

    def _detect_ci(self, root: Path) -> str | None:
        """Detect CI system from file presence."""
        if (root / ".github" / "workflows").is_dir():
            return "github-actions"
        if (root / "azure-pipelines.yml").exists():
            return "azure-pipelines"
        if (root / ".gitlab-ci.yml").exists():
            return "gitlab-ci"
        return None
