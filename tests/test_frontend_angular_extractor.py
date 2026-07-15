"""Comprehensive unit tests for FrontendAngularExtractor."""

from __future__ import annotations

from pathlib import Path

import pytest

from cortex.extractors.frontend_angular import FrontendAngularExtractor
from cortex.schema import ServiceYaml

FIXTURES_DIR = Path(__file__).parent / "fixtures"
SAMPLE_ANGULAR_REPO = FIXTURES_DIR / "sample-frontend-angular-repo"


@pytest.fixture
def extractor() -> FrontendAngularExtractor:
    """Return a fresh FrontendAngularExtractor instance."""
    return FrontendAngularExtractor()


@pytest.fixture
def service_yaml() -> ServiceYaml:
    """Return a minimal ServiceYaml for frontend-angular."""
    return ServiceYaml(
        name="test-angular-app",
        type="frontend-angular",
        owner="frontend-team",
        domain="ui",
        tier="standard",
        purpose="Test Angular app for extractor unit tests",
    )


class TestFrontendAngularExtractor:
    """Tests for FrontendAngularExtractor."""

    def test_extract_detects_angular_version(
        self, extractor: FrontendAngularExtractor, service_yaml: ServiceYaml
    ) -> None:
        """Angular version is detected from @angular/core in package.json."""
        manifest = extractor.extract(SAMPLE_ANGULAR_REPO, service_yaml)
        # @angular/core is ^17.0.0, so framework should include Angular 17
        assert manifest.framework is not None
        assert "Angular" in manifest.framework
        assert "17" in manifest.framework

    def test_extract_finds_route_modules(
        self, extractor: FrontendAngularExtractor, service_yaml: ServiceYaml
    ) -> None:
        """Lazy-loaded route modules are discovered from app.routes.ts."""
        manifest = extractor.extract(SAMPLE_ANGULAR_REPO, service_yaml)
        # The fixture has loadChildren for 'payments' and 'reports'
        module_names = [m.name for m in manifest.modules]
        assert "payments" in module_names

    def test_extract_detects_http_client_calls(
        self, extractor: FrontendAngularExtractor, service_yaml: ServiceYaml
    ) -> None:
        """HttpClient calls in *.service.ts are detected as outbound calls."""
        manifest = extractor.extract(SAMPLE_ANGULAR_REPO, service_yaml)
        assert len(manifest.outbound_calls) > 0
        # payments.service.ts makes calls to /api/v1/payments
        all_paths = [oc.target_url for oc in manifest.outbound_calls]
        assert any("/api/v1/payments" in p for p in all_paths)

    def test_extract_builds_module_info(
        self, extractor: FrontendAngularExtractor, service_yaml: ServiceYaml
    ) -> None:
        """Modules are built from route structure with correct type."""
        manifest = extractor.extract(SAMPLE_ANGULAR_REPO, service_yaml)
        # Should have at least one lazy-module and one application module
        module_types = [m.type for m in manifest.modules]
        assert "lazy-module" in module_types
        # The 'dashboard' and 'config' routes use components, so 'app' module should exist
        module_names = [m.name for m in manifest.modules]
        assert "app" in module_names

    def test_extract_with_no_routes(
        self, extractor: FrontendAngularExtractor, tmp_path: Path, service_yaml: ServiceYaml
    ) -> None:
        """Extractor handles a project with no route definitions gracefully."""
        # Create minimal package.json with Angular
        pkg = tmp_path / "package.json"
        pkg.write_text(
            '{"dependencies": {"@angular/core": "^17.0.0"}, "devDependencies": {"typescript": "~5.2.0"}}'
        )
        angular_json = tmp_path / "angular.json"
        angular_json.write_text('{"projects": {}}')

        manifest = extractor.extract(tmp_path, service_yaml)
        assert manifest.modules == []
        assert manifest.outbound_calls == []

    def test_extract_parses_dependencies(
        self, extractor: FrontendAngularExtractor, service_yaml: ServiceYaml
    ) -> None:
        """Dependencies from package.json are correctly parsed."""
        manifest = extractor.extract(SAMPLE_ANGULAR_REPO, service_yaml)
        dep_names = [d.name for d in manifest.dependencies]
        assert "@angular/core" in dep_names
        assert "@angular/router" in dep_names
        assert "rxjs" in dep_names
        # Dev deps
        assert "typescript" in dep_names
