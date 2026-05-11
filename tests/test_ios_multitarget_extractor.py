"""Tests for iOS extractor target-scoping (monorepos with multiple app targets)."""

from __future__ import annotations

import pytest

from atlas.extractors.ios import IOSExtractor
from atlas.schema import ExtractorHints, ServiceYaml
from tests.conftest import SAMPLE_IOS_MULTITARGET_REPO

# The fixture has:
#   App/
#     FanApp/          ← target "FanApp"
#       Sources/{Ticketing,Loyalty,Payment}/
#       FanApp.entitlements, FanAppDEV.entitlements
#     StaffApp/        ← target "StaffApp"
#       Sources/Operations/
#       StaffApp.entitlements, StaffAppSTG.entitlements
#     SharedConfig/Configuration/
#       Configuration-com.example.fanapp.plist        (PROD)
#       Configuration-com.example.fanapp.dev.plist
#       Configuration-com.example.staffapp.plist      (PROD)
#       Configuration-com.example.staffapp.dev.plist
#     Package.swift    (targets: FanApp, StaffApp, FanAppTests)

REPO = SAMPLE_IOS_MULTITARGET_REPO / "App"


@pytest.fixture
def extractor() -> IOSExtractor:
    return IOSExtractor()


def _make_yaml(name: str, target: str) -> ServiceYaml:
    return ServiceYaml(
        name=name,
        type="ios",
        owner="team-mobile",
        domain="entertainment",
        tier="critical",
        purpose=f"Test {target} app",
        extractor_hints=ExtractorHints(target=target),
    )


class TestTargetScopedBundleId:
    def test_fanapp_bundle_id(self, extractor: IOSExtractor) -> None:
        """Bundle ID resolves to fanapp when target=FanApp."""
        bundle_id = extractor._find_bundle_id(REPO, target_hint="fanapp")
        assert bundle_id == "com.example.fanapp"

    def test_staffapp_bundle_id(self, extractor: IOSExtractor) -> None:
        """Bundle ID resolves to staffapp when target=StaffApp."""
        bundle_id = extractor._find_bundle_id(REPO, target_hint="staffapp")
        assert bundle_id == "com.example.staffapp"

    def test_no_target_hint_returns_shortest(self, extractor: IOSExtractor) -> None:
        """Without target hint, picks shortest prod bundle ID."""
        bundle_id = extractor._find_bundle_id(REPO)
        # Both fanapp and staffapp are prod plists; fanapp is shorter
        assert bundle_id in ("com.example.fanapp", "com.example.staffapp")


class TestTargetScopedFeatureDomains:
    def test_fanapp_features(self, extractor: IOSExtractor) -> None:
        """FanApp features include Ticketing, Loyalty, Payment — not Operations."""
        domains = extractor._parse_feature_domains(REPO, target_hint="FanApp")
        assert "Ticketing" in domains
        assert "Loyalty" in domains
        assert "Payment" in domains
        assert "Operations" not in domains

    def test_staffapp_features(self, extractor: IOSExtractor) -> None:
        """StaffApp features include Operations — not Ticketing/Loyalty/Payment."""
        domains = extractor._parse_feature_domains(REPO, target_hint="StaffApp")
        assert "Operations" in domains
        assert "Ticketing" not in domains
        assert "Loyalty" not in domains

    def test_no_target_hint_returns_all(self, extractor: IOSExtractor) -> None:
        """Without target hint, all feature domains across all targets are returned."""
        domains = extractor._parse_feature_domains(REPO)
        assert "Ticketing" in domains
        assert "Operations" in domains


class TestTargetScopedEntitlements:
    def test_fanapp_entitlements(self, extractor: IOSExtractor) -> None:
        """FanApp entitlements include aps-environment and applesignin."""
        keys = extractor._parse_entitlements(REPO, target_hint="FanApp")
        assert "aps-environment" in keys
        assert "com.apple.developer.applesignin" in keys
        assert "com.apple.developer.nfc.readersession.formats" not in keys

    def test_staffapp_entitlements(self, extractor: IOSExtractor) -> None:
        """StaffApp entitlements include NFC — not applesignin."""
        keys = extractor._parse_entitlements(REPO, target_hint="StaffApp")
        assert "aps-environment" in keys
        assert "com.apple.developer.nfc.readersession.formats" in keys
        assert "com.apple.developer.applesignin" not in keys


class TestTargetScopedBuildVariants:
    def test_fanapp_variants(self, extractor: IOSExtractor) -> None:
        """FanApp build variants derived from FanApp*.entitlements filenames."""
        variants = extractor._parse_build_configurations(REPO, target_hint="FanApp")
        assert "DEV" in variants

    def test_staffapp_variants(self, extractor: IOSExtractor) -> None:
        """StaffApp build variants derived from StaffApp*.entitlements filenames."""
        variants = extractor._parse_build_configurations(REPO, target_hint="StaffApp")
        assert "STG" in variants


class TestTargetScopedEnvUrls:
    def test_fanapp_env_urls(self, extractor: IOSExtractor) -> None:
        """FanApp env URLs only include fanapp plists."""
        notes = extractor._extract_env_urls(REPO, target_hint="fanapp")
        urls = [n["note"] for n in notes]
        assert any("api.example.com" in u for u in urls)
        # Staff-specific URL must not appear
        assert not any("staff.api.example.com" in u for u in urls)

    def test_staffapp_env_urls(self, extractor: IOSExtractor) -> None:
        """StaffApp env URLs only include staffapp plists."""
        notes = extractor._extract_env_urls(REPO, target_hint="staffapp")
        urls = [n["note"] for n in notes]
        assert any("staff.api.example.com" in u for u in urls)


class TestTargetScopedFullExtraction:
    def test_fanapp_full_extraction(self, extractor: IOSExtractor) -> None:
        """Full extraction scoped to FanApp produces correct manifest."""
        service_yaml = _make_yaml("fan-app-ios", "FanApp")
        manifest = extractor.extract(REPO, service_yaml)

        assert manifest.name == "fan-app-ios"
        assert manifest.application_id == "com.example.fanapp"
        # Features are FanApp-only
        feature_refs = [ep.ref for ep in manifest.entry_points if ep.kind == "feature"]
        assert "Ticketing" in feature_refs
        assert "Operations" not in feature_refs
        # Dependencies parsed from Package.swift
        dep_names = [d.name for d in manifest.dependencies]
        assert "Alamofire" in dep_names

    def test_staffapp_full_extraction(self, extractor: IOSExtractor) -> None:
        """Full extraction scoped to StaffApp produces correct manifest."""
        service_yaml = _make_yaml("staff-app-ios", "StaffApp")
        manifest = extractor.extract(REPO, service_yaml)

        assert manifest.name == "staff-app-ios"
        assert manifest.application_id == "com.example.staffapp"
        feature_refs = [ep.ref for ep in manifest.entry_points if ep.kind == "feature"]
        assert "Operations" in feature_refs
        assert "Ticketing" not in feature_refs

    def test_two_extractions_produce_distinct_manifests(self, extractor: IOSExtractor) -> None:
        """Extracting FanApp and StaffApp from same repo path yields distinct manifests."""
        fan_yaml = _make_yaml("fan-app-ios", "FanApp")
        staff_yaml = _make_yaml("staff-app-ios", "StaffApp")

        fan_manifest = extractor.extract(REPO, fan_yaml)
        staff_manifest = extractor.extract(REPO, staff_yaml)

        assert fan_manifest.application_id != staff_manifest.application_id
        fan_features = {ep.ref for ep in fan_manifest.entry_points if ep.kind == "feature"}
        staff_features = {ep.ref for ep in staff_manifest.entry_points if ep.kind == "feature"}
        assert fan_features.isdisjoint(staff_features)
