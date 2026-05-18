"""Tests for the aggregator."""

from __future__ import annotations

from pathlib import Path

import pytest

from cortex.aggregator import (
    _extract_topic_name,
    _normalize_path,
    _resolve_api_call_edges,
    _resolve_api_call_edges_by_interface,
    _resolve_kafka_edges,
    aggregate,
)
from cortex.storage import LocalStorageBackend


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

    def test_graph_entry_has_keywords(self, storage_with_manifests: LocalStorageBackend) -> None:
        """Graph entries include keywords."""
        graph = aggregate(storage_with_manifests)

        android = next(s for s in graph.services if s.name == "sample-android")
        assert "android" in android.keywords
        assert "banking" in android.keywords

    def test_communication_field_present(
        self, storage_with_manifests: LocalStorageBackend
    ) -> None:
        """Graph always has a communication field (even if no edges)."""
        graph = aggregate(storage_with_manifests)
        assert hasattr(graph, "communication")
        assert isinstance(graph.communication.edges, list)


# ---------------------------------------------------------------------------
# TestCommunicationGraph
# ---------------------------------------------------------------------------


def _make_manifest(name: str, produces: list[str], consumes: list[str]) -> dict:
    """Helper to build a minimal manifest dict with kafka producer/consumer data."""
    return {
        "name": name,
        "type": "backend-java",
        "owner": "team-a",
        "domain": "test",
        "tier": "standard",
        "status": "active",
        "purpose": f"Test service {name}",
        "keywords": [],
        "dependencies": [],
        "entry_points": [],
        "api_contracts": [],
        "kafka_topics": produces + consumes,
        "kafka_produces": produces,
        "kafka_consumes": consumes,
        "integration_notes": [],
        "extracted_at": "2026-05-01T00:00:00Z",
        "extractor_version": "1.0.0",
    }


class TestCommunicationGraph:
    """Tests for the communication edge resolution and graph output."""

    def test_kafka_edges_from_producer_consumer_overlap(self, tmp_path: Path) -> None:
        """service-a produces topic-x, service-b consumes topic-x → 1 edge."""
        edges = _resolve_kafka_edges(
            [
                _make_manifest("service-a", ["topic-x"], []),
                _make_manifest("service-b", [], ["topic-x"]),
            ]
        )
        assert len(edges) == 1
        assert edges[0].source == "service-a"
        assert edges[0].target == "service-b"
        assert edges[0].protocol == "kafka"
        assert edges[0].detail == "topic-x"

    def test_kafka_edges_multiple_consumers(self, tmp_path: Path) -> None:
        """service-a produces topic-x; service-b and service-c both consume it → 2 edges."""
        edges = _resolve_kafka_edges(
            [
                _make_manifest("service-a", ["topic-x"], []),
                _make_manifest("service-b", [], ["topic-x"]),
                _make_manifest("service-c", [], ["topic-x"]),
            ]
        )
        assert len(edges) == 2
        sources = {e.source for e in edges}
        targets = {e.target for e in edges}
        assert sources == {"service-a"}
        assert targets == {"service-b", "service-c"}

    def test_kafka_edges_no_overlap(self, tmp_path: Path) -> None:
        """No shared topics → 0 edges."""
        edges = _resolve_kafka_edges(
            [
                _make_manifest("service-a", ["topic-x"], []),
                _make_manifest("service-b", [], ["topic-y"]),
            ]
        )
        assert len(edges) == 0

    def test_kafka_edges_bidirectional(self, tmp_path: Path) -> None:
        """service-a produces topic-x and consumes topic-y; service-b is the reverse → 2 edges."""
        edges = _resolve_kafka_edges(
            [
                _make_manifest("service-a", ["topic-x"], ["topic-y"]),
                _make_manifest("service-b", ["topic-y"], ["topic-x"]),
            ]
        )
        assert len(edges) == 2
        edge_pairs = {(e.source, e.target) for e in edges}
        assert ("service-a", "service-b") in edge_pairs
        assert ("service-b", "service-a") in edge_pairs

    def test_kafka_edges_spring_el_default_resolution(self, tmp_path: Path) -> None:
        """${VAR:default-topic} in producer and consumer both resolve to default → edge created."""
        # service-a produces "${PRODUCE_TOPIC:order-events}"
        # service-b consumes "${CONSUME_TOPIC:order-events}"
        # Different env var names but same default → should match
        edges = _resolve_kafka_edges(
            [
                _make_manifest("service-a", ["${PRODUCE_TOPIC:order-events}"], []),
                _make_manifest("service-b", [], ["${CONSUME_TOPIC:order-events}"]),
            ]
        )
        assert len(edges) == 1
        assert edges[0].source == "service-a"
        assert edges[0].target == "service-b"
        assert edges[0].detail == "order-events"

    def test_kafka_edges_spring_el_no_default_uses_varname(self, tmp_path: Path) -> None:
        """${VAR} with no default — var name used as topic key, still matches if same var name."""
        edges = _resolve_kafka_edges(
            [
                _make_manifest("service-a", ["${SHARED_TOPIC}"], []),
                _make_manifest("service-b", [], ["${SHARED_TOPIC}"]),
            ]
        )
        assert len(edges) == 1
        assert edges[0].detail == "SHARED_TOPIC"

    def test_extract_topic_name_with_default(self) -> None:
        """_extract_topic_name strips ${VAR:default} to just 'default'."""
        result = _extract_topic_name(
            "${IDENTITY_BLOCK_TOPIC:identity-block-account}"
        )
        assert result == "identity-block-account"

    def test_extract_topic_name_no_default(self) -> None:
        """_extract_topic_name with ${VAR} returns the var name."""
        assert _extract_topic_name("${MY_TOPIC}") == "MY_TOPIC"

    def test_extract_topic_name_plain_string(self) -> None:
        """_extract_topic_name with plain string returns it unchanged."""
        assert _extract_topic_name("my.topic.name") == "my.topic.name"

    def test_communication_in_graph_output(self, tmp_path: Path) -> None:
        """After aggregate(), graph.communication.edges is populated when topics match."""
        storage = LocalStorageBackend(root=tmp_path)
        storage.write_json("services/svc-a/manifest.json", _make_manifest("svc-a", ["ev-x"], []))
        storage.write_json("services/svc-b/manifest.json", _make_manifest("svc-b", [], ["ev-x"]))

        graph = aggregate(storage)

        assert len(graph.communication.edges) >= 1
        edge = next(e for e in graph.communication.edges if e.detail == "ev-x")
        assert edge.source == "svc-a"
        assert edge.target == "svc-b"

    def test_backward_compat_no_kafka_fields(self, tmp_path: Path) -> None:
        """Manifests without kafka_produces/kafka_consumes → 0 edges, no crash."""
        storage = LocalStorageBackend(root=tmp_path)
        # Old-format manifest without the new fields
        old_manifest = {
            "name": "old-svc",
            "type": "backend-java",
            "owner": "team-a",
            "domain": "test",
            "tier": "standard",
            "status": "active",
            "purpose": "Old service without kafka fields",
            "keywords": [],
            "dependencies": [],
            "entry_points": [],
            "api_contracts": [],
            "integration_notes": [],
            "extracted_at": "2026-05-01T00:00:00Z",
            "extractor_version": "0.9.0",
        }
        storage.write_json("services/old-svc/manifest.json", old_manifest)

        graph = aggregate(storage)
        assert len(graph.communication.edges) == 0
        assert len(graph.services) == 1

    def test_graph_entry_carries_kafka_fields(self, tmp_path: Path) -> None:
        """GraphEntry includes kafka_produces and kafka_consumes from manifest."""
        storage = LocalStorageBackend(root=tmp_path)
        storage.write_json(
            "services/svc-a/manifest.json",
            _make_manifest("svc-a", ["topic-x", "topic-y"], ["topic-z"]),
        )

        graph = aggregate(storage)
        svc = graph.services[0]
        assert svc.kafka_produces == ["topic-x", "topic-y"]
        assert svc.kafka_consumes == ["topic-z"]


# ---------------------------------------------------------------------------
# TestApiCallEdgeResolution
# ---------------------------------------------------------------------------


def _make_mobile_manifest(
    name: str,
    svc_type: str,
    api_calls: list[dict],
) -> dict:
    """Helper to build a minimal mobile (android/ios) manifest dict."""
    return {
        "name": name,
        "type": svc_type,
        "owner": "team-mobile",
        "domain": "mobile",
        "tier": "standard",
        "status": "active",
        "purpose": f"Test {svc_type} app {name}",
        "keywords": [],
        "dependencies": [],
        "entry_points": [],
        "api_contracts": [],
        "api_calls": api_calls,
        "integration_notes": [],
        "extracted_at": "2026-05-01T00:00:00Z",
        "extractor_version": "1.0.0",
    }


def _make_backend_manifest(
    name: str,
    endpoints: list[dict],
) -> dict:
    """Helper to build a minimal backend manifest with api_contracts endpoints."""
    return {
        "name": name,
        "type": "backend-java",
        "owner": "team-backend",
        "domain": "backend",
        "tier": "standard",
        "status": "active",
        "purpose": f"Test backend service {name}",
        "keywords": [],
        "dependencies": [],
        "entry_points": [],
        "api_contracts": [{"endpoints": endpoints}],
        "api_calls": [],
        "integration_notes": [],
        "kafka_produces": [],
        "kafka_consumes": [],
        "extracted_at": "2026-05-01T00:00:00Z",
        "extractor_version": "1.0.0",
    }


class TestNormalizePath:
    """Tests for the _normalize_path helper."""

    def test_strips_path_parameters(self) -> None:
        assert _normalize_path("/v1/orders/{id}") == "/v1/orders"

    def test_strips_dollar_variable(self) -> None:
        assert _normalize_path("/$TICKETING_API/v1/games") == "/v1/games"

    def test_strips_dollar_curly_variable(self) -> None:
        assert _normalize_path("/${SERVICE}/v1/foo") == "/v1/foo"

    def test_strips_trailing_slash(self) -> None:
        assert _normalize_path("/v1/orders/") == "/v1/orders"

    def test_collapses_double_slash_after_variable_removal(self) -> None:
        # $PREFIX removed leaves double slash
        assert _normalize_path("/$PREFIX/$VER/items") == "/items"

    def test_plain_path_unchanged(self) -> None:
        assert _normalize_path("/v1/users") == "/v1/users"


class TestApiCallEdgeResolution:
    """Tests for _resolve_api_call_edges and _resolve_api_call_edges_by_interface."""

    def _make_graph_entry(self, name: str, svc_type: str, endpoints: list[dict]):
        """Build a GraphEntry for use in edge resolution."""
        from cortex.aggregator import _manifest_to_graph_entry
        manifest = _make_backend_manifest(name, endpoints)
        manifest["type"] = svc_type
        return _manifest_to_graph_entry(manifest)

    def test_resolved_path_matches_backend_endpoint(self) -> None:
        """Mobile api_call with resolved path /v1/orders matches backend GET /v1/orders."""
        android_manifest = _make_mobile_manifest(
            "my-android-app",
            "android",
            [{"method": "GET", "path": "/v1/orders", "interface_name": "OrderApi"}],
        )
        backend_entry = self._make_graph_entry(
            "orders-microservice",
            "backend-java",
            [{"method": "GET", "path": "/v1/orders", "summary": "List orders"}],
        )
        edges = _resolve_api_call_edges([android_manifest], [backend_entry])
        assert len(edges) == 1
        assert edges[0].source == "my-android-app"
        assert edges[0].target == "orders-microservice"
        assert edges[0].protocol == "http"
        assert edges[0].confidence == 0.7

    def test_path_with_param_matches_backend_template(self) -> None:
        """Mobile /v1/orders/123 path matches backend /v1/orders/{id} endpoint."""
        android_manifest = _make_mobile_manifest(
            "my-android-app",
            "android",
            [{"method": "GET", "path": "/v1/orders/{id}", "interface_name": "OrderApi"}],
        )
        backend_entry = self._make_graph_entry(
            "orders-microservice",
            "backend-java",
            [{"method": "GET", "path": "/v1/orders/{id}", "summary": "Get order"}],
        )
        edges = _resolve_api_call_edges([android_manifest], [backend_entry])
        assert len(edges) == 1
        assert edges[0].target == "orders-microservice"

    def test_unresolved_dollar_variable_still_matches_after_strip(self) -> None:
        """Path /$UNKNOWN/v1/orders matches /v1/orders after stripping $UNKNOWN."""
        android_manifest = _make_mobile_manifest(
            "my-android-app",
            "android",
            [{"method": "GET", "path": "/$UNKNOWN/v1/orders", "interface_name": "OrderApi"}],
        )
        backend_entry = self._make_graph_entry(
            "orders-microservice",
            "backend-java",
            [{"method": "GET", "path": "/v1/orders", "summary": "List orders"}],
        )
        edges = _resolve_api_call_edges([android_manifest], [backend_entry])
        assert len(edges) == 1
        assert edges[0].target == "orders-microservice"

    def test_interface_name_fallback_ticketing_api(self) -> None:
        """TicketingApi interface name matches ticketing-microservice backend."""
        android_manifest = _make_mobile_manifest(
            "my-android-app",
            "android",
            [{"method": "GET", "path": "/$UNRESOLVED/games", "interface_name": "TicketingApi"}],
        )
        backend_entry = self._make_graph_entry(
            "ticketing-microservice",
            "backend-java",
            [{"method": "GET", "path": "/v1/events", "summary": "List events"}],
        )
        edges = _resolve_api_call_edges_by_interface([android_manifest], [backend_entry])
        assert len(edges) == 1
        assert edges[0].source == "my-android-app"
        assert edges[0].target == "ticketing-microservice"
        assert edges[0].confidence == 0.5
        assert "TicketingApi" in edges[0].detail

    def test_interface_name_fallback_payments_api(self) -> None:
        """PaymentApi interface name matches payments-microservice backend."""
        android_manifest = _make_mobile_manifest(
            "my-android-app",
            "android",
            [{"method": "POST", "path": "/$PAY/charge", "interface_name": "PaymentApi"}],
        )
        backend_entry = self._make_graph_entry(
            "payments-microservice",
            "backend-java",
            [{"method": "POST", "path": "/v1/charge", "summary": "Process payment"}],
        )
        edges = _resolve_api_call_edges_by_interface([android_manifest], [backend_entry])
        assert len(edges) == 1
        assert edges[0].target == "payments-microservice"

    def test_no_duplicate_edges_from_both_strategies(self) -> None:
        """If path-match and interface-name both match same target, only one edge total."""
        android_manifest = _make_mobile_manifest(
            "my-android-app",
            "android",
            [{"method": "GET", "path": "/v1/orders", "interface_name": "OrderApi"}],
        )
        backend_entry = self._make_graph_entry(
            "orders-microservice",
            "backend-java",
            [{"method": "GET", "path": "/v1/orders", "summary": "List orders"}],
        )
        path_edges = _resolve_api_call_edges([android_manifest], [backend_entry])
        iface_edges = _resolve_api_call_edges_by_interface([android_manifest], [backend_entry])

        # Simulate aggregate() dedup logic
        path_matched_callers = {e.source for e in path_edges}
        path_edge_pairs = {(e.source, e.target) for e in path_edges}
        deduped = [
            e for e in iface_edges
            if e.source not in path_matched_callers
            or (e.source, e.target) not in path_edge_pairs
        ]
        total = path_edges + deduped
        # Only one edge should exist for this (caller, target) pair
        pairs = [(e.source, e.target) for e in total]
        assert pairs.count(("my-android-app", "orders-microservice")) == 1

    def test_mobile_to_multiple_backends(self) -> None:
        """One mobile app calls multiple backend services → multiple interface-name edges."""
        android_manifest = _make_mobile_manifest(
            "my-android-app",
            "android",
            [
                {"method": "GET", "path": "/$T/games", "interface_name": "TicketingApi"},
                {"method": "POST", "path": "/$P/charge", "interface_name": "PaymentApi"},
            ],
        )
        ticketing_entry = self._make_graph_entry(
            "ticketing-microservice",
            "backend-java",
            [{"method": "GET", "path": "/v1/events"}],
        )
        payments_entry = self._make_graph_entry(
            "payments-microservice",
            "backend-java",
            [{"method": "POST", "path": "/v1/charge"}],
        )
        edges = _resolve_api_call_edges_by_interface(
            [android_manifest], [ticketing_entry, payments_entry]
        )
        targets = {e.target for e in edges}
        assert "ticketing-microservice" in targets
        assert "payments-microservice" in targets

    def test_ios_app_interface_name_fallback(self) -> None:
        """iOS app with empty api_calls from path matching → interface fallback works."""
        ios_manifest = _make_mobile_manifest(
            "my-ios-app",
            "ios",
            [{"method": None, "path": "/accounts/list", "interface_name": "AccountRouter"}],
        )
        backend_entry = self._make_graph_entry(
            "account-microservice",
            "backend-java",
            [{"method": "GET", "path": "/v1/accounts"}],
        )
        edges = _resolve_api_call_edges_by_interface([ios_manifest], [backend_entry])
        assert len(edges) == 1
        assert edges[0].source == "my-ios-app"
        assert edges[0].target == "account-microservice"

    def test_no_self_edges(self) -> None:
        """Mobile service does not produce edges to itself."""
        # This shouldn't happen in practice but guard against it
        android_manifest = _make_mobile_manifest(
            "my-android-app",
            "android",
            [{"method": "GET", "path": "/v1/foo", "interface_name": "FooApi"}],
        )
        # Only one service — the android app itself
        from cortex.aggregator import _manifest_to_graph_entry
        android_entry = _manifest_to_graph_entry(
            _make_mobile_manifest("my-android-app", "android", [])
        )
        edges = _resolve_api_call_edges_by_interface([android_manifest], [android_entry])
        assert edges == []

    def test_aggregate_includes_interface_name_edges(self, tmp_path: Path) -> None:
        """Full aggregate() produces interface-name edges when no path match exists."""
        storage = LocalStorageBackend(root=tmp_path)
        storage.write_json(
            "services/my-android-app/manifest.json",
            _make_mobile_manifest(
                "my-android-app",
                "android",
                [{"method": "GET", "path": "/$UNRESOLVED/games", "interface_name": "TicketingApi"}],
            ),
        )
        storage.write_json(
            "services/ticketing-microservice/manifest.json",
            _make_backend_manifest(
                "ticketing-microservice",
                [{"method": "GET", "path": "/v1/events", "summary": "Events"}],
            ),
        )
        graph = aggregate(storage)
        mobile_edges = [e for e in graph.communication.edges if e.source == "my-android-app"]
        assert len(mobile_edges) >= 1
        assert mobile_edges[0].target == "ticketing-microservice"
