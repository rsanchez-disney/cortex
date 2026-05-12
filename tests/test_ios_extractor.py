"""Tests for the iOS extractor."""

from __future__ import annotations

from pathlib import Path

import pytest

from atlas.extractors.ios import IOSExtractor
from atlas.schema import ServiceYaml
from tests.conftest import SAMPLE_IOS_REPO


@pytest.fixture
def ios_extractor() -> IOSExtractor:
    return IOSExtractor()


@pytest.fixture
def ios_service_yaml() -> ServiceYaml:
    return ServiceYaml(
        name="sample-ios",
        type="ios",
        owner="team-mobile",
        domain="mobile",
        tier="standard",
        purpose="Sample iOS app for testing the extractor.",
        status="active",
        slack="#team-mobile",
        keywords=["banking", "ios", "mobile"],
        integration_notes=[
            {"scope": "global", "note": "Uses biometric auth for sensitive operations"}
        ],
    )


class TestIOSExtractor:
    """Tests for IOSExtractor.extract()."""

    def test_successful_extraction(
        self, ios_extractor: IOSExtractor, ios_service_yaml: ServiceYaml
    ) -> None:
        """Successful extraction produces correct manifest."""
        manifest = ios_extractor.extract(SAMPLE_IOS_REPO, ios_service_yaml)

        assert manifest.name == "sample-ios"
        assert manifest.type == "ios"
        assert manifest.owner == "team-mobile"
        assert manifest.domain == "mobile"
        assert manifest.tier == "standard"
        assert manifest.extracted_at is not None
        assert manifest.extractor_version == "1.0.0"

    def test_language_detection(self, ios_extractor: IOSExtractor) -> None:
        """Detects Swift as primary language."""
        lang = ios_extractor._detect_language(SAMPLE_IOS_REPO)
        assert lang == "swift"

    def test_swift_version_from_package_swift(self, ios_extractor: IOSExtractor) -> None:
        """Detects Swift tools version from Package.swift."""
        version = ios_extractor._detect_swift_version(SAMPLE_IOS_REPO)
        assert version == "5.9"

    def test_bundle_id_from_info_plist(self, ios_extractor: IOSExtractor) -> None:
        """Extracts bundle identifier from Info.plist."""
        bundle_id = ios_extractor._find_bundle_id(SAMPLE_IOS_REPO)
        assert bundle_id == "com.example.myapp"

    def test_deployment_target(self, ios_extractor: IOSExtractor) -> None:
        """Finds deployment target from Package.swift."""
        target = ios_extractor._find_deployment_target(SAMPLE_IOS_REPO)
        assert target is not None
        assert "16" in target

    def test_spm_dependencies(self, ios_extractor: IOSExtractor) -> None:
        """Parses dependencies from Package.swift."""
        deps = ios_extractor._parse_dependencies(SAMPLE_IOS_REPO)
        dep_names = [d.name for d in deps]
        assert "Alamofire" in dep_names
        assert "Kingfisher" in dep_names

    def test_podfile_dependencies(self, ios_extractor: IOSExtractor) -> None:
        """Parses dependencies from Podfile."""
        deps = ios_extractor._parse_dependencies(SAMPLE_IOS_REPO)
        dep_names = [d.name for d in deps]
        assert "SwiftLint" in dep_names
        assert "Firebase/Analytics" in dep_names

    def test_targets_from_package_swift(self, ios_extractor: IOSExtractor) -> None:
        """Parses targets from Package.swift (when no xcodeproj)."""
        targets = ios_extractor._targets_from_package_swift(SAMPLE_IOS_REPO)
        assert "MyApp" in targets
        assert "MyAppTests" in targets

    def test_entitlements_parsed(self, ios_extractor: IOSExtractor) -> None:
        """Parses entitlement keys from *.entitlements files."""
        entitlements = ios_extractor._parse_entitlements(SAMPLE_IOS_REPO)
        assert "aps-environment" in entitlements
        assert "com.apple.developer.applesignin" in entitlements

    def test_ci_detection(self, ios_extractor: IOSExtractor) -> None:
        """Detects GitHub Actions CI from .github/workflows/."""
        ci = ios_extractor._detect_ci(SAMPLE_IOS_REPO)
        assert ci == "github-actions"

    def test_no_api_contracts(self, ios_extractor: IOSExtractor) -> None:
        """Mobile apps return no API contracts."""
        contracts = ios_extractor.find_api_contracts(SAMPLE_IOS_REPO)
        assert contracts == []

    def test_api_calls_field_present_in_manifest(self, tmp_path: Path) -> None:
        """ios extractor manifest includes api_calls field."""
        (tmp_path / "Sources").mkdir()
        (tmp_path / "Sources" / "main.swift").write_text('print("hello")')
        (tmp_path / "Package.swift").write_text(
            "// swift-tools-version: 5.9\n"
            "import PackageDescription\n"
            "let package = Package(\n"
            '    name: "TestPkg",\n'
            "    targets: [\n"
            '        .executableTarget(name: "TestPkg")\n'
            "    ]\n"
            ")\n"
        )
        service_yaml = ServiceYaml(
            name="test-ios",
            type="ios",
            owner="team",
            domain="mobile",
            tier="standard",
            purpose="Test iOS app",
        )
        extractor = IOSExtractor()
        manifest = extractor.extract(tmp_path, service_yaml)
        assert hasattr(manifest, "api_calls")
        assert isinstance(manifest.api_calls, list)

    def test_missing_xcodeproj_still_works(self, tmp_path: Path) -> None:
        """Missing xcodeproj still works if Package.swift is present."""
        # Create a Swift package only project
        (tmp_path / "Sources").mkdir()
        (tmp_path / "Sources" / "main.swift").write_text('print("hello")')
        (tmp_path / "Package.swift").write_text(
            "// swift-tools-version: 5.9\n"
            "import PackageDescription\n"
            "let package = Package(\n"
            '    name: "TestPkg",\n'
            "    targets: [\n"
            '        .executableTarget(name: "TestPkg")\n'
            "    ]\n"
            ")\n"
        )

        service_yaml = ServiceYaml(
            name="test-pkg",
            type="ios",
            owner="team",
            domain="mobile",
            tier="standard",
            purpose="Test package",
        )

        extractor = IOSExtractor()
        manifest = extractor.extract(tmp_path, service_yaml)
        assert manifest.name == "test-pkg"
        assert manifest.language == "swift"
        assert manifest.language_version == "5.9"


# ---------------------------------------------------------------------------
# TestApiCallExtraction
# ---------------------------------------------------------------------------


class TestApiCallExtraction:
    """Tests for _parse_api_calls() (Swift networking pattern detection)."""

    def test_url_path_literals_detected(self, tmp_path: Path) -> None:
        """Swift file in Network/ dir with return '/v1/...' → api_calls populated."""
        net_dir = tmp_path / "MyApp" / "Sources" / "Network"
        net_dir.mkdir(parents=True)
        (net_dir / "APIRouter.swift").write_text(
            "enum APIRouter {\n"
            "    case getAccounts\n"
            "    var path: String {\n"
            "        switch self {\n"
            '        case .getAccounts: return "/v1/accounts"\n'
            "        }\n"
            "    }\n"
            "}\n"
        )
        extractor = IOSExtractor()
        calls = extractor._parse_api_calls(tmp_path)
        paths = [c.path for c in calls]
        assert "/v1/accounts" in paths

    def test_fixture_has_api_calls(self) -> None:
        """The sample iOS fixture APIRouter.swift → api_calls populated."""
        extractor = IOSExtractor()
        calls = extractor._parse_api_calls(SAMPLE_IOS_REPO)
        paths = [c.path for c in calls]
        assert "/v1/accounts" in paths

    def test_no_api_calls_in_empty_repo(self, tmp_path: Path) -> None:
        """Empty repo → api_calls is empty list."""
        extractor = IOSExtractor()
        calls = extractor._parse_api_calls(tmp_path)
        assert calls == []

    def test_build_dirs_excluded(self, tmp_path: Path) -> None:
        """Files under .build/ are not scanned."""
        build_net = tmp_path / ".build" / "checkouts" / "SomeLib" / "Network"
        build_net.mkdir(parents=True)
        (build_net / "SomeAPI.swift").write_text(
            "enum API {\n"
            "    var path: String {\n"
            '        return "/v1/should-not-appear"\n'
            "    }\n"
            "}\n"
        )
        extractor = IOSExtractor()
        calls = extractor._parse_api_calls(tmp_path)
        paths = [c.path for c in calls]
        assert "/v1/should-not-appear" not in paths

    def test_non_versioned_paths_detected(self, tmp_path: Path) -> None:
        """Non-versioned API paths (e.g. /accounts/list) are detected after relaxation."""
        net_dir = tmp_path / "MyApp" / "Sources" / "Network"
        net_dir.mkdir(parents=True)
        (net_dir / "AccountRouter.swift").write_text(
            "enum AccountRouter {\n"
            "    case listAccounts\n"
            "    var path: String {\n"
            "        switch self {\n"
            '        case .listAccounts: return "/accounts/list"\n'
            "        }\n"
            "    }\n"
            "}\n"
        )
        extractor = IOSExtractor()
        calls = extractor._parse_api_calls(tmp_path)
        paths = [c.path for c in calls]
        assert "/accounts/list" in paths

    def test_endpoint_swift_suffix_included(self, tmp_path: Path) -> None:
        """Files ending in Endpoint.swift are included as API files."""
        net_dir = tmp_path / "MyApp" / "Sources"
        net_dir.mkdir(parents=True)
        (net_dir / "UserEndpoint.swift").write_text(
            "enum UserEndpoint {\n"
            "    var path: String {\n"
            '        return "/users/profile"\n'
            "    }\n"
            "}\n"
        )
        extractor = IOSExtractor()
        calls = extractor._parse_api_calls(tmp_path)
        paths = [c.path for c in calls]
        assert "/users/profile" in paths

    def test_swift_interpolation_treated_as_param(self, tmp_path: Path) -> None:
        """Swift \\(variable) interpolation is replaced with {param} placeholder."""
        net_dir = tmp_path / "MyApp" / "Sources" / "Network"
        net_dir.mkdir(parents=True)
        (net_dir / "ItemRouter.swift").write_text(
            "enum ItemRouter {\n"
            "    case getItem(id: String)\n"
            "    var path: String {\n"
            "        switch self {\n"
            '        case .getItem(let id): return "/items/\\(id)"\n'
            "        }\n"
            "    }\n"
            "}\n"
        )
        extractor = IOSExtractor()
        calls = extractor._parse_api_calls(tmp_path)
        paths = [c.path for c in calls]
        assert "/items/{param}" in paths

    def test_plain_string_method_detected(self, tmp_path: Path) -> None:
        """var method: String { return \"GET\" } is detected (not just Moya.Method)."""
        net_dir = tmp_path / "MyApp" / "Sources" / "Network"
        net_dir.mkdir(parents=True)
        (net_dir / "APIRouter.swift").write_text(
            "enum APIRouter {\n"
            "    case getOrders\n"
            "    var path: String {\n"
            '        return "/v1/orders"\n'
            "    }\n"
            "    var method: String {\n"
            '        return "GET"\n'
            "    }\n"
            "}\n"
        )
        extractor = IOSExtractor()
        calls = extractor._parse_api_calls(tmp_path)
        assert len(calls) >= 1
        get_calls = [c for c in calls if c.method == "GET"]
        assert len(get_calls) >= 1

    def test_noise_path_filtered(self, tmp_path: Path) -> None:
        """/pathToTheInformation style camelCase paths are filtered as noise."""
        net_dir = tmp_path / "App" / "Sources" / "Network"
        net_dir.mkdir(parents=True)
        (net_dir / "NavRouter.swift").write_text(
            "enum NavRouter {\n"
            "    var path: String {\n"
            '        return "/pathToTheInformation"\n'
            "    }\n"
            "}\n"
        )
        extractor = IOSExtractor()
        calls = extractor._parse_api_calls(tmp_path)
        paths = [c.path for c in calls]
        assert "/pathToTheInformation" not in paths

    def test_real_api_path_not_filtered(self, tmp_path: Path) -> None:
        """Real API paths like /v1/accounts are not filtered as noise."""
        net_dir = tmp_path / "App" / "Sources" / "Network"
        net_dir.mkdir(parents=True)
        (net_dir / "APIRouter.swift").write_text(
            "enum APIRouter {\n"
            "    var path: String {\n"
            '        return "/v1/accounts"\n'
            "    }\n"
            "}\n"
        )
        extractor = IOSExtractor()
        calls = extractor._parse_api_calls(tmp_path)
        paths = [c.path for c in calls]
        assert "/v1/accounts" in paths

    def test_target_scoping_isolates_api_calls(self, tmp_path: Path) -> None:
        """When target_hint scopes to a subdirectory, only that dir's files are scanned."""
        # FanApp target directory with its own API
        fan_dir = tmp_path / "LaClippers" / "Sources" / "Network"
        fan_dir.mkdir(parents=True)
        (fan_dir / "FanRouter.swift").write_text(
            "enum FanRouter {\n"
            "    var path: String {\n"
            '        return "/fan/loyalty"\n'
            "    }\n"
            "}\n"
        )
        # StaffApp target directory with its own API
        staff_dir = tmp_path / "LACStaff" / "Sources" / "Network"
        staff_dir.mkdir(parents=True)
        (staff_dir / "StaffRouter.swift").write_text(
            "enum StaffRouter {\n"
            "    var path: String {\n"
            '        return "/staff/access"\n'
            "    }\n"
            "}\n"
        )
        extractor = IOSExtractor()
        # Scoped to LaClippers — should only see fan paths
        fan_calls = extractor._parse_api_calls(tmp_path / "LaClippers")
        fan_paths = [c.path for c in fan_calls]
        assert "/fan/loyalty" in fan_paths
        assert "/staff/access" not in fan_paths

        # Scoped to LACStaff — should only see staff paths
        staff_calls = extractor._parse_api_calls(tmp_path / "LACStaff")
        staff_paths = [c.path for c in staff_calls]
        assert "/staff/access" in staff_paths
        assert "/fan/loyalty" not in staff_paths
