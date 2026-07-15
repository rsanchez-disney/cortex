"""Comprehensive unit tests for BackendGoExtractor."""

from __future__ import annotations

from pathlib import Path

import pytest

from cortex.extractors.backend_go import BackendGoExtractor
from cortex.schema import ServiceYaml

FIXTURES_DIR = Path(__file__).parent / "fixtures"
SAMPLE_GO_REPO = FIXTURES_DIR / "sample-backend-go-repo"


@pytest.fixture
def extractor() -> BackendGoExtractor:
    """Return a fresh BackendGoExtractor instance."""
    return BackendGoExtractor()


@pytest.fixture
def service_yaml() -> ServiceYaml:
    """Return a minimal ServiceYaml for backend-go."""
    return ServiceYaml(
        name="events-service",
        type="backend-go",
        owner="platform-team",
        domain="events",
        tier="standard",
        purpose="Event processing service for extractor unit tests",
    )


class TestBackendGoExtractor:
    """Tests for BackendGoExtractor."""

    def test_extract_parses_go_mod(
        self, extractor: BackendGoExtractor, service_yaml: ServiceYaml
    ) -> None:
        """go.mod is parsed for module name and dependencies."""
        manifest = extractor.extract(SAMPLE_GO_REPO, service_yaml)
        dep_names = [d.name for d in manifest.dependencies]
        assert "github.com/go-chi/chi/v5" in dep_names
        assert "github.com/jackc/pgx/v5" in dep_names
        assert "github.com/segmentio/kafka-go" in dep_names

    def test_extract_detects_chi_framework(
        self, extractor: BackendGoExtractor, service_yaml: ServiceYaml
    ) -> None:
        """chi framework is detected from github.com/go-chi/chi dependency."""
        manifest = extractor.extract(SAMPLE_GO_REPO, service_yaml)
        assert manifest.framework == "chi"

    def test_extract_detects_gin_framework(
        self, extractor: BackendGoExtractor, tmp_path: Path, service_yaml: ServiceYaml
    ) -> None:
        """gin framework is detected when github.com/gin-gonic/gin is in go.mod."""
        go_mod = tmp_path / "go.mod"
        go_mod.write_text(
            "module github.com/example/gin-service\n\n"
            "go 1.21\n\n"
            "require (\n"
            "\tgithub.com/gin-gonic/gin v1.9.1\n"
            ")\n"
        )
        # Create a minimal .go file with a gin route
        main_go = tmp_path / "main.go"
        main_go.write_text(
            "package main\n\n"
            'import "github.com/gin-gonic/gin"\n\n'
            "func main() {\n"
            "\tr := gin.Default()\n"
            '\tr.GET("/healthz", healthCheck)\n'
            "}\n"
        )

        manifest = extractor.extract(tmp_path, service_yaml)
        assert manifest.framework == "gin"

    def test_extract_detects_database_type(
        self, extractor: BackendGoExtractor, service_yaml: ServiceYaml
    ) -> None:
        """PostgreSQL is detected from pgx dependency in go.mod."""
        manifest = extractor.extract(SAMPLE_GO_REPO, service_yaml)
        assert manifest.database_type == "postgresql"

    def test_extract_detects_cache_type(
        self, extractor: BackendGoExtractor, service_yaml: ServiceYaml
    ) -> None:
        """Redis cache is detected from go-redis dependency in go.mod."""
        manifest = extractor.extract(SAMPLE_GO_REPO, service_yaml)
        assert manifest.cache_type == "redis"

    def test_extract_with_missing_go_mod(
        self, extractor: BackendGoExtractor, tmp_path: Path, service_yaml: ServiceYaml
    ) -> None:
        """Extractor does not crash when go.mod is missing; returns graceful defaults."""
        manifest = extractor.extract(tmp_path, service_yaml)
        assert manifest.name == "events-service"
        assert manifest.framework is None
        assert manifest.dependencies == []
        assert manifest.database_type is None
        assert manifest.cache_type is None
        assert manifest.language == "Go"

    def test_extract_sets_language_version(
        self, extractor: BackendGoExtractor, service_yaml: ServiceYaml
    ) -> None:
        """Language is set to Go and version is extracted from go.mod."""
        manifest = extractor.extract(SAMPLE_GO_REPO, service_yaml)
        assert manifest.language == "Go"
        assert manifest.language_version == "1.22"
