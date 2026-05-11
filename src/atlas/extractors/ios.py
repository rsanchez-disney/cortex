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
from datetime import datetime, timezone
from pathlib import Path

import structlog

from atlas import __version__
from atlas.extractors.base import Extractor
from atlas.schema import (
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
        modules = self._parse_local_packages(effective_root)
        targets = self._parse_targets(effective_root, target_hint=target_hint)
        build_variants = self._parse_build_configurations(effective_root, target_hint=target_hint)
        entitlements = self._parse_entitlements(effective_root, target_hint=target_hint)
        integration_notes = self._extract_env_urls(effective_root, target_hint=target_hint)
        ci = self._detect_ci(repo_path)
        api_contracts = self.find_api_contracts(effective_root)

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
            modules=modules,
            build_variants=build_variants,
            dependencies=dependencies,
            entry_points=entry_points,
            api_contracts=api_contracts,
            runtime=None,
            ci=ci,
            integration_notes=all_notes,
            extracted_at=datetime.now(timezone.utc),
            extractor_version=__version__,
            source_repo=source_repo,
        )

    def find_api_contracts(self, repo_path: Path) -> list[ApiContract]:
        """Mobile apps typically don't have API contracts."""
        return []

    # --- Private parsing methods ---

    def _detect_language(self, root: Path) -> str:
        """Detect primary language by counting .swift vs .m/.h files."""
        swift_count = len(list(root.rglob("*.swift")))
        objc_count = len(list(root.rglob("*.m"))) + len(list(root.rglob("*.h")))

        if swift_count >= objc_count:
            return "swift"
        return "objective-c"

    def _detect_swift_version(self, root: Path) -> str | None:
        """Detect Swift version from Package.swift swift-tools-version comment."""
        package_swift = root / "Package.swift"
        if package_swift.exists():
            content = package_swift.read_text()
            m = re.search(r"swift-tools-version:\s*([\d.]+)", content)
            if m:
                return m.group(1)

        # Try xcodeproj settings via pbxproj
        swift_ver = self._swift_version_from_xcodeproj(root)
        if swift_ver:
            return swift_ver

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
        _GENERIC = {"Common", "Component", "Data", "UIComponent", "Sources"}
        domains: list[str] = []
        seen: set[str] = set()

        # Determine which directories to walk
        if target_hint:
            target_dir = self._resolve_target_dir(root, target_hint)
            candidates = [target_dir] if target_dir and target_dir.is_dir() else []
        else:
            # Walk direct children of root that look like target directories (have a Sources/ subdir)
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
                if normalised in _GENERIC or normalised in seen:
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
