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
