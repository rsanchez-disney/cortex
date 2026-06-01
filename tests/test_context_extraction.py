"""Tests for AI context extraction (R2.1, R2.2, R2.3, R2.4).

Validates that the base extractor methods correctly extract AGENTS.md / CLAUDE.md,
domain.md, and context-pack files from repos, and that the backend-java extractor
integrates them into the manifest via _enrich_with_context.

R2.4 tests validate the enriched_purpose generation from agent_context content.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cortex.extractors.backend_java import BackendJavaExtractor
from cortex.schema import ServiceYaml

FIXTURES = Path(__file__).parent / "fixtures"
BACKEND_JAVA_FIXTURE = FIXTURES / "sample-backend-java-repo"


@pytest.fixture()
def service_yaml() -> ServiceYaml:
    """Minimal ServiceYaml for the backend-java fixture."""
    return ServiceYaml(
        name="demo-service",
        type="backend-java",
        owner="team-backend",
        domain="orders",
        tier="standard",
        purpose="Demo Spring Boot service for testing",
    )


@pytest.fixture()
def extractor() -> BackendJavaExtractor:
    return BackendJavaExtractor()


# --- R2.1: agent_context ---


class TestAgentContext:
    """Tests for AGENTS.md / CLAUDE.md extraction."""

    def test_agent_context_extracted(
        self, extractor: BackendJavaExtractor, service_yaml: ServiceYaml
    ) -> None:
        """AGENTS.md content is captured in manifest.agent_context."""
        manifest = extractor.extract(BACKEND_JAVA_FIXTURE, service_yaml)
        assert manifest.agent_context is not None
        assert "Sample Demo Service" in manifest.agent_context
        assert "Spring Boot microservice" in manifest.agent_context

    def test_agent_context_falls_back_to_claude_md(
        self, extractor: BackendJavaExtractor, tmp_path: Path
    ) -> None:
        """When AGENTS.md is absent but CLAUDE.md exists, it is used instead."""
        claude_content = "# Claude instructions\nUse clean architecture."
        (tmp_path / "CLAUDE.md").write_text(claude_content, encoding="utf-8")

        result = extractor._extract_agent_context(tmp_path)
        assert result == claude_content

    def test_agent_context_prefers_agents_md(
        self, extractor: BackendJavaExtractor, tmp_path: Path
    ) -> None:
        """When both AGENTS.md and CLAUDE.md exist, AGENTS.md wins."""
        (tmp_path / "AGENTS.md").write_text("agents content", encoding="utf-8")
        (tmp_path / "CLAUDE.md").write_text("claude content", encoding="utf-8")

        result = extractor._extract_agent_context(tmp_path)
        assert result == "agents content"

    def test_agent_context_none_when_missing(
        self, extractor: BackendJavaExtractor, tmp_path: Path
    ) -> None:
        """Returns None when neither AGENTS.md nor CLAUDE.md exists."""
        result = extractor._extract_agent_context(tmp_path)
        assert result is None


# --- R2.2: domain_context ---


class TestDomainContext:
    """Tests for domain.md extraction from context-pack directories."""

    def test_domain_context_extracted(
        self, extractor: BackendJavaExtractor, service_yaml: ServiceYaml
    ) -> None:
        """domain.md content is captured in manifest.domain_context."""
        manifest = extractor.extract(BACKEND_JAVA_FIXTURE, service_yaml)
        assert manifest.domain_context is not None
        assert "Order Management" in manifest.domain_context
        assert "Business Rules" in manifest.domain_context

    def test_domain_context_from_ai_dir(
        self, extractor: BackendJavaExtractor, tmp_path: Path
    ) -> None:
        """Finds domain.md in .ai/context-pack/ directory."""
        pack_dir = tmp_path / ".ai" / "context-pack"
        pack_dir.mkdir(parents=True)
        (pack_dir / "domain.md").write_text("domain from .ai", encoding="utf-8")

        result = extractor._extract_domain_context(tmp_path)
        assert result == "domain from .ai"

    def test_domain_context_from_ai_no_dot_dir(
        self, extractor: BackendJavaExtractor, tmp_path: Path
    ) -> None:
        """Finds domain.md in ai/context-pack/ directory (no dot)."""
        pack_dir = tmp_path / "ai" / "context-pack"
        pack_dir.mkdir(parents=True)
        (pack_dir / "domain.md").write_text("domain from ai", encoding="utf-8")

        result = extractor._extract_domain_context(tmp_path)
        assert result == "domain from ai"

    def test_domain_context_from_ia_dir(
        self, extractor: BackendJavaExtractor, tmp_path: Path
    ) -> None:
        """Finds domain.md in ia-context-pack/ directory."""
        pack_dir = tmp_path / "ia-context-pack"
        pack_dir.mkdir(parents=True)
        (pack_dir / "domain.md").write_text("domain from ia", encoding="utf-8")

        result = extractor._extract_domain_context(tmp_path)
        assert result == "domain from ia"

    def test_domain_context_from_dot_ia_dir(
        self, extractor: BackendJavaExtractor, tmp_path: Path
    ) -> None:
        """Finds domain.md in .ia/context-pack/ directory (typo variant)."""
        pack_dir = tmp_path / ".ia" / "context-pack"
        pack_dir.mkdir(parents=True)
        (pack_dir / "domain.md").write_text("domain from .ia", encoding="utf-8")

        result = extractor._extract_domain_context(tmp_path)
        assert result == "domain from .ia"

    def test_domain_context_none_when_missing(
        self, extractor: BackendJavaExtractor, tmp_path: Path
    ) -> None:
        """Returns None when no context-pack directory exists."""
        result = extractor._extract_domain_context(tmp_path)
        assert result is None

    def test_domain_context_none_when_no_domain_md(
        self, extractor: BackendJavaExtractor, tmp_path: Path
    ) -> None:
        """Returns None when context-pack dir exists but has no domain.md."""
        pack_dir = tmp_path / ".ai" / "context-pack"
        pack_dir.mkdir(parents=True)
        (pack_dir / "other.md").write_text("not domain", encoding="utf-8")

        result = extractor._extract_domain_context(tmp_path)
        assert result is None


# --- R2.3: context_pack ---


class TestContextPack:
    """Tests for context-pack directory indexing."""

    def test_context_pack_extracted(
        self, extractor: BackendJavaExtractor, service_yaml: ServiceYaml
    ) -> None:
        """All .md files in context-pack are indexed by stem."""
        manifest = extractor.extract(BACKEND_JAVA_FIXTURE, service_yaml)
        assert manifest.context_pack is not None
        assert "domain" in manifest.context_pack
        assert "tech-stack" in manifest.context_pack
        assert "Order Management" in manifest.context_pack["domain"]
        assert "Spring Boot 3.x" in manifest.context_pack["tech-stack"]

    def test_context_pack_keys_are_stems(
        self, extractor: BackendJavaExtractor, tmp_path: Path
    ) -> None:
        """Keys are filename stems (without .md extension)."""
        pack_dir = tmp_path / ".ai" / "context-pack"
        pack_dir.mkdir(parents=True)
        (pack_dir / "architecture.md").write_text("arch content", encoding="utf-8")
        (pack_dir / "api-guide.md").write_text("api content", encoding="utf-8")

        result = extractor._extract_context_pack(tmp_path)
        assert result is not None
        assert set(result.keys()) == {"architecture", "api-guide"}
        assert result["architecture"] == "arch content"
        assert result["api-guide"] == "api content"

    def test_context_pack_from_dot_ia_typo_variant(
        self, extractor: BackendJavaExtractor, tmp_path: Path
    ) -> None:
        """Verify .ia/context-pack/ (typo variant) is detected."""
        ia_dir = tmp_path / ".ia" / "context-pack"
        ia_dir.mkdir(parents=True)
        (ia_dir / "domain.md").write_text("# Domain\nQueue management", encoding="utf-8")

        result = extractor._extract_context_pack(tmp_path)
        assert result is not None
        assert "domain" in result
        assert "Queue management" in result["domain"]

    def test_context_pack_none_when_missing(
        self, extractor: BackendJavaExtractor, tmp_path: Path
    ) -> None:
        """Returns None when no context-pack directory exists."""
        result = extractor._extract_context_pack(tmp_path)
        assert result is None

    def test_context_pack_none_when_empty(
        self, extractor: BackendJavaExtractor, tmp_path: Path
    ) -> None:
        """Returns None when context-pack directory exists but has no .md files."""
        pack_dir = tmp_path / ".ai" / "context-pack"
        pack_dir.mkdir(parents=True)
        (pack_dir / "readme.txt").write_text("not markdown", encoding="utf-8")

        result = extractor._extract_context_pack(tmp_path)
        assert result is None

    def test_context_pack_ignores_non_md_files(
        self, extractor: BackendJavaExtractor, tmp_path: Path
    ) -> None:
        """Only .md files are included in the context pack."""
        pack_dir = tmp_path / ".ai" / "context-pack"
        pack_dir.mkdir(parents=True)
        (pack_dir / "notes.md").write_text("notes", encoding="utf-8")
        (pack_dir / "data.json").write_text("{}", encoding="utf-8")
        (pack_dir / "script.py").write_text("print(1)", encoding="utf-8")

        result = extractor._extract_context_pack(tmp_path)
        assert result is not None
        assert set(result.keys()) == {"notes"}


# --- R2.4: enriched_purpose ---


class TestEnrichedPurpose:
    """Tests for enriched_purpose generation from agent_context."""

    def test_enriched_purpose_from_what_this_project_is(
        self, extractor: BackendJavaExtractor
    ) -> None:
        """Extracts summary from a 'What This Project Is' section."""
        content = (
            "# My Service\n\n"
            "## What This Project Is\n\n"
            "Central identity service managing fan and staff accounts.\n\n"
            "- **Type**: Spring Boot microservice\n"
            "- **Language**: Java 17\n"
            "- **Database**: PostgreSQL\n"
            "- **Key Integrations**: NBA CIAM, Apple Wallet, Kafka\n\n"
            "## Architecture\n\n"
            "Clean architecture with controllers and services.\n"
        )
        result = extractor._generate_enriched_purpose(content)
        assert result is not None
        assert "Spring Boot microservice" in result
        assert "Java 17" in result
        assert "PostgreSQL" in result
        assert "Central identity service" in result
        assert "Integrates with:" in result
        assert "NBA CIAM" in result

    def test_enriched_purpose_from_project_dna(
        self, extractor: BackendJavaExtractor
    ) -> None:
        """Extracts summary from a 'Project DNA' section."""
        content = (
            "# Sample Demo Service\n\n"
            "## Project DNA\n"
            "- **Type**: Spring Boot microservice\n"
            "- **Language**: Java 17\n"
            "- **Framework**: Spring Boot 3.x\n"
            "- **Database**: PostgreSQL with Flyway migrations\n\n"
            "## Architecture\n"
            "Clean architecture with controllers, services, and data providers.\n"
        )
        result = extractor._generate_enriched_purpose(content)
        assert result is not None
        assert "Spring Boot microservice" in result
        assert "Java 17" in result
        assert "PostgreSQL" in result

    def test_enriched_purpose_from_overview(
        self, extractor: BackendJavaExtractor
    ) -> None:
        """Extracts summary from an 'Overview' section."""
        content = (
            "# Geo Service\n\n"
            "## Overview\n\n"
            "Provides geolocation data for venue operations.\n\n"
            "- **Type**: Spring Boot microservice\n"
            "- **Database**: CosmosDB\n\n"
            "## Setup\n\n"
            "Run `./gradlew bootRun`.\n"
        )
        result = extractor._generate_enriched_purpose(content)
        assert result is not None
        assert "Spring Boot microservice" in result
        assert "CosmosDB" in result
        assert "geolocation" in result

    def test_enriched_purpose_fallback_first_paragraph(
        self, extractor: BackendJavaExtractor
    ) -> None:
        """Falls back to the first non-heading paragraph when no recognised section exists."""
        content = (
            "# Some Service\n\n"
            "This service handles payment processing for all venues.\n\n"
            "## Setup\n\n"
            "Run the build script.\n"
        )
        result = extractor._generate_enriched_purpose(content)
        assert result is not None
        assert "payment processing" in result

    def test_enriched_purpose_none_when_no_context(
        self, extractor: BackendJavaExtractor
    ) -> None:
        """Returns None when agent_context is None."""
        result = extractor._generate_enriched_purpose(None)
        assert result is None

    def test_enriched_purpose_truncated_to_limit(
        self, extractor: BackendJavaExtractor
    ) -> None:
        """Output is limited to ~500 characters."""
        # Build a very long "What This Project Is" section
        long_prose = "This is a very detailed description. " * 50  # ~1850 chars
        content = (
            "# Big Service\n\n"
            "## What This Project Is\n\n"
            f"{long_prose}\n\n"
            "## Other\n\nStuff.\n"
        )
        result = extractor._generate_enriched_purpose(content)
        assert result is not None
        assert len(result) <= 503  # 500 + "..." suffix
        assert result.endswith("...")

    def test_enriched_purpose_integrated_via_extract(
        self, extractor: BackendJavaExtractor, service_yaml: ServiceYaml
    ) -> None:
        """enriched_purpose is populated when extracting the backend-java fixture."""
        manifest = extractor.extract(BACKEND_JAVA_FIXTURE, service_yaml)
        assert manifest.enriched_purpose is not None
        # The fixture AGENTS.md has "Project DNA" with "Spring Boot microservice"
        assert "Spring Boot microservice" in manifest.enriched_purpose

    def test_enriched_purpose_none_when_empty_content(
        self, extractor: BackendJavaExtractor
    ) -> None:
        """Returns None when agent_context is empty or whitespace-only."""
        assert extractor._generate_enriched_purpose("") is None
        assert extractor._generate_enriched_purpose("   \n\n  ") is None

    def test_enriched_purpose_what_this_service_is(
        self, extractor: BackendJavaExtractor
    ) -> None:
        """Recognises 'What This Service Is' heading variant."""
        content = (
            "# Notifications\n\n"
            "## What This Service Is\n\n"
            "Push notification gateway for mobile apps.\n\n"
            "## Config\n\nSee application.yml.\n"
        )
        result = extractor._generate_enriched_purpose(content)
        assert result is not None
        assert "Push notification gateway" in result
