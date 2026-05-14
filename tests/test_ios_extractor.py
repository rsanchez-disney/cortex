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

    def test_relative_url_endpoint_protocol_detected(self, tmp_path: Path) -> None:
        """EndPointProtocol pattern with var relativeURL is detected."""
        ep_dir = tmp_path / "MyApp" / "Sources" / "Endpoint"
        ep_dir.mkdir(parents=True)
        (ep_dir / "SuiteEndPoint.swift").write_text(
            "enum SuiteEndpoint {\n"
            "    case getSuite\n"
            "    case updateAccess\n"
            "}\n"
            "\n"
            "extension SuiteEndpoint: EndPointProtocol {\n"
            "    var relativeURL: String {\n"
            "        switch self {\n"
            "        case .getSuite:\n"
            '            return "/suite/packages/exists"\n'
            "        case .updateAccess:\n"
            '            return "/suite/admins/access"\n'
            "        }\n"
            "    }\n"
            "    var method: String {\n"
            "        switch self {\n"
            "        case .getSuite:\n"
            "            return URLRequestMethod.get.rawValue\n"
            "        case .updateAccess:\n"
            "            return URLRequestMethod.put.rawValue\n"
            "        }\n"
            "    }\n"
            "}\n"
        )
        extractor = IOSExtractor()
        calls = extractor._parse_api_calls(tmp_path)
        paths = {c.path: c.method for c in calls}
        assert "/suite/packages/exists" in paths
        assert paths["/suite/packages/exists"] == "GET"
        assert "/suite/admins/access" in paths
        assert paths["/suite/admins/access"] == "PUT"

    def test_url_request_method_rawvalue_detected(self, tmp_path: Path) -> None:
        """URLRequestMethod.delete.rawValue method pattern is detected."""
        ep_dir = tmp_path / "MyApp" / "Data" / "Endpoint"
        ep_dir.mkdir(parents=True)
        (ep_dir / "AdminEndPoint.swift").write_text(
            "enum AdminEndpoint {\n"
            "    case deleteAdmin\n"
            "    case addAdmin\n"
            "}\n"
            "\n"
            "extension AdminEndpoint: EndPointProtocol {\n"
            "    var relativeURL: String {\n"
            "        switch self {\n"
            "        case .deleteAdmin:\n"
            '            return "/admins/remove"\n'
            "        case .addAdmin:\n"
            '            return "/admins/add"\n'
            "        }\n"
            "    }\n"
            "    var method: String {\n"
            "        switch self {\n"
            "        case .deleteAdmin:\n"
            "            return URLRequestMethod.delete.rawValue\n"
            "        case .addAdmin:\n"
            "            return URLRequestMethod.post.rawValue\n"
            "        }\n"
            "    }\n"
            "}\n"
        )
        extractor = IOSExtractor()
        calls = extractor._parse_api_calls(tmp_path)
        paths = {c.path: c.method for c in calls}
        assert paths.get("/admins/remove") == "DELETE"
        assert paths.get("/admins/add") == "POST"

    def test_per_case_method_cross_reference(self, tmp_path: Path) -> None:
        """Per-case methods are cross-referenced with var relativeURL paths."""
        ep_dir = tmp_path / "MyApp" / "Sources" / "Endpoint"
        ep_dir.mkdir(parents=True)
        (ep_dir / "MixedEndPoint.swift").write_text(
            "enum MixedEndpoint {\n"
            "    case getItems\n"
            "    case createItem\n"
            "    case deleteItem\n"
            "}\n"
            "\n"
            "extension MixedEndpoint: EndPointProtocol {\n"
            "    var relativeURL: String {\n"
            "        switch self {\n"
            "        case .getItems:\n"
            '            return "/items/list"\n'
            "        case .createItem:\n"
            '            return "/items/create"\n'
            "        case .deleteItem:\n"
            '            return "/items/delete"\n'
            "        }\n"
            "    }\n"
            "    var method: String {\n"
            "        switch self {\n"
            "        case .getItems:\n"
            "            return URLRequestMethod.get.rawValue\n"
            "        case .createItem:\n"
            "            return URLRequestMethod.post.rawValue\n"
            "        case .deleteItem:\n"
            "            return URLRequestMethod.delete.rawValue\n"
            "        }\n"
            "    }\n"
            "}\n"
        )
        extractor = IOSExtractor()
        calls = extractor._parse_api_calls(tmp_path)
        call_map = {c.path: c.method for c in calls}
        assert call_map["/items/list"] == "GET"
        assert call_map["/items/create"] == "POST"
        assert call_map["/items/delete"] == "DELETE"

    def test_multi_case_method_mapping(self, tmp_path: Path) -> None:
        """Multi-case lines like 'case .a, .b:' map all case names to the same value."""
        ep_dir = tmp_path / "MyApp" / "Sources" / "Endpoint"
        ep_dir.mkdir(parents=True)
        (ep_dir / "BatchEndPoint.swift").write_text(
            "enum BatchEndpoint {\n"
            "    case getList\n"
            "    case getDetail\n"
            "    case createNew\n"
            "}\n"
            "\n"
            "extension BatchEndpoint: EndPointProtocol {\n"
            "    var relativeURL: String {\n"
            "        switch self {\n"
            "        case .getList:\n"
            '            return "/batch/list"\n'
            "        case .getDetail:\n"
            '            return "/batch/detail"\n'
            "        case .createNew:\n"
            '            return "/batch/new"\n'
            "        }\n"
            "    }\n"
            "    var method: String {\n"
            "        switch self {\n"
            "        case .getList, .getDetail:\n"
            "            return URLRequestMethod.get.rawValue\n"
            "        case .createNew:\n"
            "            return URLRequestMethod.post.rawValue\n"
            "        }\n"
            "    }\n"
            "}\n"
        )
        extractor = IOSExtractor()
        calls = extractor._parse_api_calls(tmp_path)
        call_map = {c.path: c.method for c in calls}
        assert call_map["/batch/list"] == "GET"
        assert call_map["/batch/detail"] == "GET"
        assert call_map["/batch/new"] == "POST"

    def test_endpoint_suffix_case_insensitive(self, tmp_path: Path) -> None:
        """Both Endpoint.swift and EndPoint.swift suffixes are detected as API files."""
        src_dir = tmp_path / "MyApp" / "Sources"
        src_dir.mkdir(parents=True)
        (src_dir / "UserEndPoint.swift").write_text(
            "enum UserEndpoint {\n"
            "    var relativeURL: String {\n"
            '        return "/users/profile"\n'
            "    }\n"
            "}\n"
        )
        extractor = IOSExtractor()
        calls = extractor._parse_api_calls(tmp_path)
        paths = [c.path for c in calls]
        assert "/users/profile" in paths

    def test_fixture_has_endpoint_protocol_calls(self) -> None:
        """The sample iOS fixture SuiteEndPoint.swift → api_calls with correct methods."""
        extractor = IOSExtractor()
        calls = extractor._parse_api_calls(SAMPLE_IOS_REPO)
        call_map = {c.path: c.method for c in calls}
        assert "/suite-admin-management/admins/self/packages/exists" in call_map
        assert call_map["/suite-admin-management/admins/self/packages/exists"] == "GET"
        # The interpolation paths should use {param}
        assert "/suite-admin-management/admins/{param}/access" in call_map
        assert call_map["/suite-admin-management/admins/{param}/access"] == "PUT"


# ---------------------------------------------------------------------------
# TestSharedComponentScanning
# ---------------------------------------------------------------------------


class TestSharedComponentScanning:
    """Tests for _find_shared_component_dirs() and shared directory scanning."""

    def test_common_components_dir_found(self, tmp_path: Path) -> None:
        """Directories containing 'Common' are returned as shared dirs."""
        (tmp_path / "LaClippers" / "Sources").mkdir(parents=True)
        (tmp_path / "LaLiga" / "CommonComponents" / "Data").mkdir(parents=True)
        extractor = IOSExtractor()
        shared = extractor._find_shared_component_dirs(tmp_path, "LaClippers")
        shared_names = [d.name for d in shared]
        assert "CommonComponents" in shared_names

    def test_target_dir_excluded(self, tmp_path: Path) -> None:
        """The target directory itself is not included in shared dirs."""
        (tmp_path / "LaClippers" / "Sources").mkdir(parents=True)
        (tmp_path / "CommonShared").mkdir(parents=True)
        extractor = IOSExtractor()
        shared = extractor._find_shared_component_dirs(tmp_path, "LaClippers")
        shared_names = [d.name for d in shared]
        assert "LaClippers" not in shared_names

    def test_shared_endpoints_included_in_extract(self, tmp_path: Path) -> None:
        """When target_hint is set, shared component endpoints are included."""
        # Target directory
        fan_dir = tmp_path / "LaClippers" / "Sources" / "Endpoint"
        fan_dir.mkdir(parents=True)
        (fan_dir / "FanEndpoint.swift").write_text(
            "enum FanEndpoint {\n"
            "    var path: String {\n"
            '        return "/fan/loyalty"\n'
            "    }\n"
            "}\n"
        )
        # Shared CommonComponents
        shared_dir = tmp_path / "LaLiga" / "CommonComponents" / "Data" / "Endpoint"
        shared_dir.mkdir(parents=True)
        (shared_dir / "PaymentEndpoint.swift").write_text(
            "enum PaymentEndpoint {\n"
            "    var path: String {\n"
            '        return "/payments/process"\n'
            "    }\n"
            "}\n"
        )
        # Create minimal project files
        (tmp_path / "Package.swift").write_text(
            "// swift-tools-version: 5.9\n"
            "import PackageDescription\n"
            "let package = Package(name: \"Test\")\n"
        )
        service_yaml = ServiceYaml(
            name="test-fan-app",
            type="ios",
            owner="team",
            domain="mobile",
            tier="standard",
            purpose="Test fan app",
            extractor_hints={"target": "LaClippers"},
        )
        extractor = IOSExtractor()
        manifest = extractor.extract(tmp_path, service_yaml)
        paths = [c.path for c in manifest.api_calls]
        assert "/fan/loyalty" in paths
        assert "/payments/process" in paths


# ---------------------------------------------------------------------------
# TestDatabaseTypeDetection
# ---------------------------------------------------------------------------


class TestDatabaseTypeDetection:
    """Tests for _detect_database_type()."""

    def test_realm_from_dependency(self, tmp_path: Path) -> None:
        """Realm dependency → database_type = 'realm'."""
        from atlas.schema import Dependency
        deps = [Dependency(name="realm-swift", version="10.45.0", source="Package.swift")]
        extractor = IOSExtractor()
        assert extractor._detect_database_type(deps, tmp_path) == "realm"

    def test_realm_from_import(self, tmp_path: Path) -> None:
        """import RealmSwift in source → database_type = 'realm'."""
        src = tmp_path / "Sources"
        src.mkdir()
        (src / "DB.swift").write_text("import RealmSwift\nclass DB {}\n")
        extractor = IOSExtractor()
        assert extractor._detect_database_type([], tmp_path) == "realm"

    def test_coredata_from_xcdatamodeld(self, tmp_path: Path) -> None:
        """.xcdatamodeld bundle → database_type = 'coredata'."""
        (tmp_path / "Model.xcdatamodeld").mkdir()
        extractor = IOSExtractor()
        assert extractor._detect_database_type([], tmp_path) == "coredata"

    def test_grdb_from_dependency(self, tmp_path: Path) -> None:
        """GRDB.swift dependency → database_type = 'sqlite'."""
        from atlas.schema import Dependency
        deps = [Dependency(name="GRDB.swift", version="6.0.0", source="Package.swift")]
        extractor = IOSExtractor()
        assert extractor._detect_database_type(deps, tmp_path) == "sqlite"

    def test_swiftdata_from_import(self, tmp_path: Path) -> None:
        """import SwiftData → database_type = 'swiftdata'."""
        src = tmp_path / "Sources"
        src.mkdir()
        (src / "Store.swift").write_text("import SwiftData\n@Model class Item {}\n")
        extractor = IOSExtractor()
        assert extractor._detect_database_type([], tmp_path) == "swiftdata"

    def test_no_database(self, tmp_path: Path) -> None:
        """No database signals → database_type = None."""
        src = tmp_path / "Sources"
        src.mkdir()
        (src / "App.swift").write_text("import UIKit\nclass App {}\n")
        extractor = IOSExtractor()
        assert extractor._detect_database_type([], tmp_path) is None

    def test_fixture_detects_realm(self) -> None:
        """The sample iOS fixture has RealmSwift → database_type = 'realm'."""
        extractor = IOSExtractor()
        deps = extractor._parse_dependencies(SAMPLE_IOS_REPO)
        assert extractor._detect_database_type(deps, SAMPLE_IOS_REPO) == "realm"


# ---------------------------------------------------------------------------
# TestMinSdkFromDeploymentTarget
# ---------------------------------------------------------------------------


class TestMinSdkFromDeploymentTarget:
    """Tests for min_sdk populated from deployment target."""

    def test_min_sdk_in_manifest(self) -> None:
        """Manifest includes min_sdk from deployment target."""
        extractor = IOSExtractor()
        service_yaml = ServiceYaml(
            name="sample-ios",
            type="ios",
            owner="team-mobile",
            domain="mobile",
            tier="standard",
            purpose="Test",
        )
        manifest = extractor.extract(SAMPLE_IOS_REPO, service_yaml)
        assert manifest.min_sdk is not None
        assert "16" in manifest.min_sdk


# ---------------------------------------------------------------------------
# TestPermissionsFromEntitlements
# ---------------------------------------------------------------------------


class TestPermissionsFromEntitlements:
    """Tests for permissions populated from entitlements."""

    def test_permissions_in_manifest(self) -> None:
        """Manifest includes permissions from entitlements parsing."""
        extractor = IOSExtractor()
        service_yaml = ServiceYaml(
            name="sample-ios",
            type="ios",
            owner="team-mobile",
            domain="mobile",
            tier="standard",
            purpose="Test",
        )
        manifest = extractor.extract(SAMPLE_IOS_REPO, service_yaml)
        assert "aps-environment" in manifest.permissions
        assert "com.apple.developer.applesignin" in manifest.permissions


# ---------------------------------------------------------------------------
# TestFrameworkDetection
# ---------------------------------------------------------------------------


class TestFrameworkDetection:
    """Tests for _detect_framework()."""

    def test_swiftui_dominant(self, tmp_path: Path) -> None:
        """More SwiftUI imports than UIKit → 'swiftui'."""
        src = tmp_path / "Sources"
        src.mkdir()
        for i in range(5):
            (src / f"View{i}.swift").write_text("import SwiftUI\nstruct V: View {}\n")
        (src / "App.swift").write_text("import UIKit\nclass App {}\n")
        extractor = IOSExtractor()
        assert extractor._detect_framework(tmp_path) == "swiftui"

    def test_uikit_dominant(self, tmp_path: Path) -> None:
        """More UIKit imports than SwiftUI → 'uikit'."""
        src = tmp_path / "Sources"
        src.mkdir()
        for i in range(5):
            (src / f"VC{i}.swift").write_text("import UIKit\nclass VC {}\n")
        (src / "Widget.swift").write_text("import SwiftUI\nstruct W: View {}\n")
        extractor = IOSExtractor()
        assert extractor._detect_framework(tmp_path) == "uikit"

    def test_mixed_framework(self, tmp_path: Path) -> None:
        """Both SwiftUI and UIKit present significantly → 'swiftui+uikit'."""
        src = tmp_path / "Sources"
        src.mkdir()
        for i in range(3):
            (src / f"View{i}.swift").write_text("import SwiftUI\nstruct V: View {}\n")
        for i in range(3):
            (src / f"VC{i}.swift").write_text("import UIKit\nclass VC {}\n")
        extractor = IOSExtractor()
        assert extractor._detect_framework(tmp_path) == "swiftui+uikit"

    def test_no_framework(self, tmp_path: Path) -> None:
        """No SwiftUI or UIKit imports → None."""
        src = tmp_path / "Sources"
        src.mkdir()
        (src / "Util.swift").write_text("import Foundation\nfunc foo() {}\n")
        extractor = IOSExtractor()
        assert extractor._detect_framework(tmp_path) is None


# ---------------------------------------------------------------------------
# TestDependencyCategorization
# ---------------------------------------------------------------------------


class TestDependencyCategorization:
    """Tests for dependency category assignment."""

    def test_networking_category(self) -> None:
        """Alamofire → category = 'networking'."""
        from atlas.schema import Dependency
        dep = Dependency(name="Alamofire", version="5.8.0", source="Package.swift")
        extractor = IOSExtractor()
        assert extractor._categorize_dependency(dep) == "networking"

    def test_database_category(self) -> None:
        """RealmSwift → category = 'database'."""
        from atlas.schema import Dependency
        dep = Dependency(name="RealmSwift", version="10.0", source="Podfile")
        extractor = IOSExtractor()
        assert extractor._categorize_dependency(dep) == "database"

    def test_analytics_category(self) -> None:
        """Firebase/Analytics → category = 'analytics'."""
        from atlas.schema import Dependency
        dep = Dependency(name="Firebase/Analytics", version="10.20.0", source="Podfile")
        extractor = IOSExtractor()
        assert extractor._categorize_dependency(dep) == "analytics"

    def test_ui_category(self) -> None:
        """Kingfisher → category = 'ui'."""
        from atlas.schema import Dependency
        dep = Dependency(name="Kingfisher", version="7.10.0", source="Package.swift")
        extractor = IOSExtractor()
        assert extractor._categorize_dependency(dep) == "ui"

    def test_unknown_dependency_no_category(self) -> None:
        """Unknown dependency → category = None."""
        from atlas.schema import Dependency
        dep = Dependency(name="SomeCustomLib", version="1.0", source="Package.swift")
        extractor = IOSExtractor()
        assert extractor._categorize_dependency(dep) is None

    def test_categories_in_manifest(self) -> None:
        """Dependencies in manifest have categories assigned."""
        extractor = IOSExtractor()
        service_yaml = ServiceYaml(
            name="sample-ios",
            type="ios",
            owner="team-mobile",
            domain="mobile",
            tier="standard",
            purpose="Test",
        )
        manifest = extractor.extract(SAMPLE_IOS_REPO, service_yaml)
        dep_cats = {d.name: d.category for d in manifest.dependencies}
        assert dep_cats.get("Alamofire") == "networking"
        assert dep_cats.get("Kingfisher") == "ui"
        assert dep_cats.get("Firebase/Analytics") == "analytics"
        assert dep_cats.get("SwiftLint") == "tooling"

    def test_auth_category(self) -> None:
        """JWTDecode → category = 'auth'."""
        from atlas.schema import Dependency
        dep = Dependency(name="JWTDecode", version="3.0", source="Package.swift")
        extractor = IOSExtractor()
        assert extractor._categorize_dependency(dep) == "auth"

    def test_testing_category(self) -> None:
        """Quick → category = 'testing'."""
        from atlas.schema import Dependency
        dep = Dependency(name="Quick", version="7.0", source="Package.swift")
        extractor = IOSExtractor()
        assert extractor._categorize_dependency(dep) == "testing"
