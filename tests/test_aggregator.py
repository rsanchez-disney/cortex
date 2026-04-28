"""Tests for the aggregator."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from atlas.aggregator import aggregate
from atlas.schema import ExtractionError
from atlas.storage import LocalStorageBackend


@pytest.fixture
def storage_with_manifests(tmp_path: Path) -> LocalStorageBackend:
    """Create a storage backend with sample manifests."""
    storage = LocalStorageBackend(root=tmp_path)

    # Write Android manifest
    storage.write_json(
        "services/sample-android/manifest.json",
        {
            "name": "sample-android",
            "type": "android",
            "owner": "team-mobile",
            "domain": "mobile",
            "tier": "standard",
            "status": "active",
            "purpose": "Sample Android app",
            "keywords": ["android", "banking"],
            "language": "kotlin",
            "dependencies": [
                {
                    "name": "retrofit",
                    "version": "2.9.0",
                    "source": "build.gradle.kts",
                    "direct": True,
                },
            ],
            "entry_points": [{"kind": "main-activity", "ref": ".MainActivity"}],
            "api_contracts": [],
            "ci": "github-actions",
            "integration_notes": [],
            "extracted_at": "2026-04-23T03:00:00Z",
            "extractor_version": "1.0.0",
        },
    )

    # Write iOS manifest
    storage.write_json(
        "services/sample-ios/manifest.json",
        {
            "name": "sample-ios",
            "type": "ios",
            "owner": "team-mobile",
            "domain": "mobile",
            "tier": "standard",
            "status": "active",
            "purpose": "Sample iOS app",
            "keywords": ["ios", "banking"],
            "language": "swift",
            "language_version": "5.9",
            "dependencies": [
                {
                    "name": "Alamofire",
                    "version": "5.8.0",
                    "source": "Package.swift",
                    "direct": True,
                },
            ],
            "entry_points": [{"kind": "target", "ref": "MyApp"}],
            "api_contracts": [],
            "ci": "github-actions",
            "integration_notes": [],
            "extracted_at": "2026-04-23T03:00:00Z",
            "extractor_version": "1.0.0",
        },
    )

    return storage


@pytest.fixture
def storage_with_error(tmp_path: Path) -> LocalStorageBackend:
    """Create a storage backend with one manifest and one error."""
    storage = LocalStorageBackend(root=tmp_path)

    storage.write_json(
        "services/good-app/manifest.json",
        {
            "name": "good-app",
            "type": "android",
            "owner": "team-a",
            "domain": "payments",
            "tier": "critical",
            "status": "active",
            "purpose": "A good app",
            "keywords": [],
            "dependencies": [],
            "entry_points": [],
            "api_contracts": [],
            "ci": "azure-pipelines",
            "integration_notes": [],
            "extracted_at": "2026-04-23T03:00:00Z",
            "extractor_version": "1.0.0",
        },
    )

    storage.write_json(
        "services/bad-app/extraction-error.json",
        {
            "repo": "bad-app",
            "timestamp": "2026-04-23T03:00:00Z",
            "error": "service.yaml validation failed: 'domain' is required",
            "phase": "validation",
        },
    )

    return storage


class TestAggregate:
    """Tests for the aggregate function."""

    def test_aggregates_multiple_manifests(
        self, storage_with_manifests: LocalStorageBackend
    ) -> None:
        """Graph includes both Android and iOS services."""
        graph = aggregate(storage_with_manifests)

        assert graph.metadata.service_count == 2
        assert len(graph.services) == 2
        assert len(graph.failed_extractions) == 0

        names = [s.name for s in graph.services]
        assert "sample-android" in names
        assert "sample-ios" in names

    def test_graph_structure(self, storage_with_manifests: LocalStorageBackend) -> None:
        """Graph has expected structure fields."""
        graph = aggregate(storage_with_manifests)

        assert graph.metadata.version == "1.0.0"
        assert graph.metadata.timestamp is not None
        assert graph.metadata.service_count == len(graph.services)

    def test_handles_valid_manifest_and_error(
        self, storage_with_error: LocalStorageBackend
    ) -> None:
        """Graph includes valid service and lists failed one."""
        graph = aggregate(storage_with_error)

        assert len(graph.services) == 1
        assert graph.services[0].name == "good-app"

        assert len(graph.failed_extractions) == 1
        assert graph.failed_extractions[0].repo == "bad-app"
        assert "domain" in graph.failed_extractions[0].error

    def test_empty_storage(self, tmp_path: Path) -> None:
        """Empty storage produces empty graph."""
        storage = LocalStorageBackend(root=tmp_path)
        graph = aggregate(storage)

        assert len(graph.services) == 0
        assert len(graph.failed_extractions) == 0
        assert graph.metadata.service_count == 0

    def test_graph_entry_has_dependencies(
        self, storage_with_manifests: LocalStorageBackend
    ) -> None:
        """Graph entries include dependency names."""
        graph = aggregate(storage_with_manifests)

        android = next(s for s in graph.services if s.name == "sample-android")
        assert "retrofit" in android.dependencies

        ios = next(s for s in graph.services if s.name == "sample-ios")
        assert "Alamofire" in ios.dependencies

    def test_graph_entry_has_keywords(self, storage_with_manifests: LocalStorageBackend) -> None:
        """Graph entries include keywords."""
        graph = aggregate(storage_with_manifests)

        android = next(s for s in graph.services if s.name == "sample-android")
        assert "android" in android.keywords
        assert "banking" in android.keywords
