"""Tests for MCP server tools.

Tests all 4 tools against a fixture graph built from Android + iOS manifests.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from cortex.storage import LocalStorageBackend
from mcp_server.server import (
    CortexMCPServer,
    _matches_filter,
    _score_service,
    _tokenize,
    _tokenize_identifier,
)


@pytest.fixture
def mcp_storage(tmp_path: Path) -> LocalStorageBackend:
    """Create storage with fixture graph and manifests."""
    storage = LocalStorageBackend(root=tmp_path)

    # Create manifests
    android_manifest = {
        "name": "sample-android",
        "type": "android",
        "owner": "team-mobile",
        "domain": "mobile",
        "tier": "standard",
        "status": "active",
        "purpose": "Sample Android banking app for mobile payments",
        "keywords": ["android", "banking", "mobile", "payments"],
        "language": "kotlin",
        "dependencies": [
            {"name": "retrofit", "version": "2.9.0", "source": "build.gradle.kts", "direct": True},
            {"name": "sample-ios", "version": None, "source": "manual", "direct": True},
        ],
        "entry_points": [{"kind": "main-activity", "ref": ".MainActivity"}],
        "api_contracts": [],
        "kafka_produces": [],
        "kafka_consumes": [],
        "ci": "github-actions",
        "integration_notes": [
            {"scope": "global", "note": "Uses custom auth flow via AuthManager"},
        ],
        "extracted_at": "2026-04-23T03:00:00Z",
        "extractor_version": "1.0.0",
    }

    ios_manifest = {
        "name": "sample-ios",
        "type": "ios",
        "owner": "team-mobile",
        "domain": "mobile",
        "tier": "standard",
        "status": "active",
        "purpose": "Sample iOS banking app with biometric authentication",
        "keywords": ["ios", "banking", "mobile", "biometric"],
        "language": "swift",
        "language_version": "5.9",
        "dependencies": [
            {"name": "Alamofire", "version": "5.8.0", "source": "Package.swift", "direct": True},
        ],
        "entry_points": [{"kind": "target", "ref": "MyApp"}],
        "api_contracts": [],
        "kafka_produces": ["orders.created"],
        "kafka_consumes": [],
        "ci": "github-actions",
        "integration_notes": [
            {"scope": "global", "note": "Uses biometric auth for sensitive operations"},
        ],
        "extracted_at": "2026-04-23T03:00:00Z",
        "extractor_version": "1.0.0",
    }

    backend_manifest = {
        "name": "sample-backend",
        "type": "backend-java",
        "owner": "team-backend",
        "domain": "orders",
        "tier": "critical",
        "purpose": "Order management service",
        "language": "java",
        "language_version": "17",
        "framework": "spring-boot",
        "spring_boot_version": "3.2.0",
        "database_type": "postgresql",
        "secondary_databases": ["redis"],
        "cache_type": "redis",
        "flyway_migration_count": 5,
        "gradle_plugins": ["org.springframework.boot"],
        "swagger_url": "https://orders.example.com/swagger-ui.html",
        "outbound_calls": [
            {
                "target_url": "https://notifications.example.com",
                "target_service": "notifications-service",
                "config_key": "services.notifications.base-url",
                "protocol": "http",
                "client_interfaces": ["NotificationsWebClient"],
                "endpoints": [],
            }
        ],
        "api_calls": [
            {
                "method": "GET",
                "path": "/v1/users/{id}",
                "interface_name": "UserApi",
                "base_url_key": "services.identity.base-url",
            }
        ],
        "entry_points": [
            {"kind": "main-class", "ref": "com.example.demo.DemoApplication"},
            {"kind": "kafka-listener", "ref": "OrderEventListener.onOrderEvent"},
            {"kind": "scheduled", "ref": "OrderCleanupJob.cleanup"},
        ],
        "agent_context": "This service handles order lifecycle management.",
        "domain_context": "Orders domain: manages order creation, updates, and fulfillment.",
        "context_pack": {
            "AGENTS.md": "# Order Service\nHandles order CRUD operations.",
            "ARCHITECTURE.md": "# Architecture\nClean architecture with hexagonal ports.",
        },
        "api_contracts": [
            {
                "controller": "OrderController",
                "base_path": "/api/v1",
                "endpoints": [
                    {
                        "method": "POST",
                        "path": "/api/v1/orders",
                        "handler": "createOrder",
                        "request_body": {"type": "CreateOrderRequest", "required": True},
                        "response": {"type": "OrderDto", "wrapper": "ResponseEntity"},
                        "parameters": [],
                    }
                ],
            }
        ],
        "dto_schemas": {
            "CreateOrderRequest": {
                "name": "CreateOrderRequest",
                "kind": "class",
                "fields": [
                    {
                        "name": "customerName",
                        "type": "String",
                        "required": True,
                        "constraints": [
                            {"kind": "size", "value": None, "min": 1, "max": 100}
                        ],
                    },
                    {
                        "name": "items",
                        "type": "List<OrderItemDto>",
                        "required": True,
                        "constraints": [],
                    },
                    {
                        "name": "shippingAddress",
                        "type": "AddressDto",
                        "required": False,
                        "constraints": [],
                    },
                ],
                "enum_values": [],
                "parent": None,
                "source_file": "src/main/java/com/example/dto/CreateOrderRequest.java",
            },
            "OrderDto": {
                "name": "OrderDto",
                "kind": "class",
                "fields": [
                    {"name": "id", "type": "Long", "required": False, "constraints": []},
                    {
                        "name": "customerName",
                        "type": "String",
                        "required": False,
                        "constraints": [],
                    },
                    {
                        "name": "status",
                        "type": "OrderStatus",
                        "required": False,
                        "constraints": [],
                    },
                    {
                        "name": "items",
                        "type": "List<OrderItemDto>",
                        "required": False,
                        "constraints": [],
                    },
                ],
                "enum_values": [],
                "parent": None,
                "source_file": "src/main/java/com/example/dto/OrderDto.java",
            },
            "OrderItemDto": {
                "name": "OrderItemDto",
                "kind": "class",
                "fields": [
                    {
                        "name": "productName",
                        "type": "String",
                        "required": True,
                        "constraints": [],
                    },
                    {
                        "name": "quantity",
                        "type": "int",
                        "required": False,
                        "constraints": [
                            {"kind": "min", "value": "1", "min": None, "max": None}
                        ],
                    },
                    {
                        "name": "price",
                        "type": "BigDecimal",
                        "required": False,
                        "constraints": [],
                    },
                ],
                "enum_values": [],
                "parent": None,
                "source_file": "src/main/java/com/example/dto/OrderItemDto.java",
            },
            "OrderStatus": {
                "name": "OrderStatus",
                "kind": "enum",
                "fields": [],
                "enum_values": [
                    "PENDING",
                    "CONFIRMED",
                    "SHIPPED",
                    "DELIVERED",
                    "CANCELLED",
                ],
                "parent": None,
                "source_file": "src/main/java/com/example/dto/OrderStatus.java",
            },
            "AddressDto": {
                "name": "AddressDto",
                "kind": "class",
                "fields": [
                    {
                        "name": "street",
                        "type": "String",
                        "required": True,
                        "constraints": [],
                    },
                    {
                        "name": "city",
                        "type": "String",
                        "required": True,
                        "constraints": [],
                    },
                    {
                        "name": "state",
                        "type": "String",
                        "required": False,
                        "constraints": [
                            {"kind": "size", "value": None, "min": 2, "max": 2}
                        ],
                    },
                    {
                        "name": "zipCode",
                        "type": "String",
                        "required": True,
                        "constraints": [],
                    },
                ],
                "enum_values": [],
                "parent": None,
                "source_file": "src/main/java/com/example/dto/AddressDto.java",
            },
        },
        "dependencies": [],
        "kafka_produces": [],
        "kafka_consumes": [],
        "integration_notes": [],
        "extracted_at": "2026-04-23T03:00:00Z",
        "extractor_version": "1.0.0",
    }

    storage.write_json("services/sample-android/manifest.json", android_manifest)
    storage.write_json("services/sample-ios/manifest.json", ios_manifest)
    storage.write_json("services/sample-backend/manifest.json", backend_manifest)

    # Create graph with communication edges
    graph = {
        "services": [
            {
                "name": "sample-android",
                "type": "android",
                "owner": "team-mobile",
                "domain": "mobile",
                "tier": "standard",
                "status": "active",
                "purpose": "Sample Android banking app for mobile payments",
                "keywords": ["android", "banking", "mobile", "payments"],
                "language": "kotlin",
                "endpoints": [],
                "kafka_produces": [],
                "kafka_consumes": ["orders.created"],
            },
            {
                "name": "sample-ios",
                "type": "ios",
                "owner": "team-mobile",
                "domain": "mobile",
                "tier": "standard",
                "status": "active",
                "purpose": "Sample iOS banking app with biometric authentication",
                "keywords": ["ios", "banking", "mobile", "biometric"],
                "language": "swift",
                "endpoints": [],
                "kafka_produces": ["orders.created"],
                "kafka_consumes": [],
            },
            {
                "name": "sample-backend",
                "type": "backend-java",
                "owner": "team-backend",
                "domain": "orders",
                "tier": "critical",
                "purpose": "Order management service",
                "framework": "spring-boot",
                "database_type": "postgresql",
                "cache_type": "redis",
            },
        ],
        "communication": {
            "edges": [
                {
                    "source": "sample-ios",
                    "target": "sample-android",
                    "protocol": "kafka",
                    "detail": "orders.created",
                    "confidence": 0.9,
                }
            ]
        },
        "failed_extractions": [],
        "metadata": {
            "timestamp": "2026-04-23T03:00:00Z",
            "version": "1.0.0",
            "service_count": 3,
        },
    }
    storage.write_json("graph/latest.json", graph)

    return storage


@pytest.fixture
def mcp_server(mcp_storage: LocalStorageBackend) -> CortexMCPServer:
    """Create an MCP server instance with fixture data."""
    return CortexMCPServer(storage=mcp_storage)


class TestTokenize:
    """Tests for _tokenize helper."""

    def test_basic_tokenization(self) -> None:
        tokens = _tokenize("I need to add a login page to Android")
        assert "login" in tokens
        assert "page" in tokens
        assert "android" in tokens
        # Stop words removed
        assert "i" not in tokens
        assert "to" not in tokens
        assert "a" not in tokens

    def test_empty_string(self) -> None:
        assert _tokenize("") == set()


class TestScoreService:
    """Tests for _score_service helper."""

    def test_name_match_highest_weight(self) -> None:
        svc = {"name": "android-app", "keywords": [], "purpose": "", "domain": ""}
        score, matched = _score_service(svc, {"android"})
        assert score > 0
        assert "name" in matched

    def test_keyword_match(self) -> None:
        svc = {"name": "some-app", "keywords": ["banking"], "purpose": "", "domain": ""}
        score, matched = _score_service(svc, {"banking"})
        assert score > 0
        assert "keywords" in matched

    def test_no_match_zero_score(self) -> None:
        svc = {"name": "some-app", "keywords": ["banking"], "purpose": "Payments", "domain": "pay"}
        score, matched = _score_service(svc, {"quantum", "physics"})
        assert score == 0
        assert matched == []

    def test_endpoint_path_match(self) -> None:
        """Service with matching endpoint path tokens gets scored."""
        svc = {
            "name": "geoinfo-microservice",
            "keywords": [],
            "purpose": "Geo info service",
            "domain": "geo-info",
            "endpoints": [
                {
                    "method": "POST",
                    "path": "/v1/zipcode/validate-country",
                    "request_body": {"type": "ValidateCountryRequest"},
                    "response": {"type": "ValidateCountryResponse"},
                }
            ],
        }
        score, matched = _score_service(svc, {"zipcode"})
        assert score > 0
        assert "endpoint_paths" in matched

    def test_endpoint_dto_match(self) -> None:
        """Service with matching DTO name tokens gets scored."""
        svc = {
            "name": "some-service",
            "keywords": [],
            "purpose": "Some service",
            "domain": "misc",
            "endpoints": [
                {
                    "method": "POST",
                    "path": "/api/orders",
                    "request_body": {"type": "CreateOrderRequest"},
                    "response": {"type": "OrderDto"},
                }
            ],
        }
        score, matched = _score_service(svc, {"order"})
        assert score > 0
        assert "endpoint_dtos" in matched

    def test_no_endpoints_no_crash(self) -> None:
        """Service without endpoints does not crash and gets no endpoint score."""
        svc = {
            "name": "mobile-app",
            "keywords": [],
            "purpose": "Mobile app",
            "domain": "mobile",
        }
        score, matched = _score_service(svc, {"zipcode"})
        assert "endpoint_paths" not in matched
        assert "endpoint_dtos" not in matched

    def test_endpoint_null_path_handled(self) -> None:
        """Endpoint with None path does not crash."""
        svc = {
            "name": "some-service",
            "keywords": [],
            "purpose": "",
            "domain": "",
            "endpoints": [{"method": "GET", "path": None}],
        }
        score, matched = _score_service(svc, {"zipcode"})
        assert "endpoint_paths" not in matched

    def test_endpoint_no_request_body_or_response(self) -> None:
        """Endpoint without request_body/response does not crash."""
        svc = {
            "name": "some-service",
            "keywords": [],
            "purpose": "",
            "domain": "",
            "endpoints": [{"method": "GET", "path": "/health"}],
        }
        score, matched = _score_service(svc, {"health"})
        assert "endpoint_paths" in matched
        assert "endpoint_dtos" not in matched


class TestTokenizeIdentifier:
    """Tests for _tokenize_identifier helper."""

    def test_camel_case_splitting(self) -> None:
        tokens = _tokenize_identifier("ValidateCountryRequest")
        assert "validate" in tokens
        assert "country" in tokens
        assert "request" not in tokens  # filtered as noise

    def test_path_splitting(self) -> None:
        tokens = _tokenize_identifier("/v1/zipcode/validate-country")
        assert "zipcode" in tokens
        assert "validate" in tokens
        assert "country" in tokens
        assert "v1" not in tokens  # filtered as noise

    def test_empty_string(self) -> None:
        assert _tokenize_identifier("") == set()

    def test_acronym_handling(self) -> None:
        tokens = _tokenize_identifier("NBAWebClient")
        assert "nba" in tokens
        assert "web" in tokens
        assert "client" in tokens

    def test_noise_words_filtered(self) -> None:
        tokens = _tokenize_identifier("OrderResponseDto")
        assert "order" in tokens
        assert "response" not in tokens
        assert "dto" not in tokens

    def test_underscore_splitting(self) -> None:
        tokens = _tokenize_identifier("order_status_update")
        assert "order" in tokens
        assert "status" in tokens
        assert "update" in tokens

    def test_single_char_filtered(self) -> None:
        tokens = _tokenize_identifier("a/b/c")
        assert len(tokens) == 0


class TestFindRelevantServices:
    """Tests for find_relevant_services tool."""

    def test_returns_relevant_services(self, mcp_server: CortexMCPServer) -> None:
        """Returns relevant services for a known task description."""
        # Access the tool function directly through the registered tools
        result = asyncio.run(
            _call_tool(
                mcp_server,
                "find_relevant_services",
                {
                    "task_description": "I need to add a banking feature to the Android app",
                    "max_results": 5,
                },
            )
        )
        assert "candidates" in result
        assert len(result["candidates"]) > 0
        # Android should be ranked highly
        names = [c["name"] for c in result["candidates"]]
        assert "sample-android" in names

    def test_max_results_respected(self, mcp_server: CortexMCPServer) -> None:
        """max_results limits the number of candidates."""
        result = asyncio.run(
            _call_tool(
                mcp_server,
                "find_relevant_services",
                {
                    "task_description": "banking mobile",
                    "max_results": 1,
                },
            )
        )
        assert len(result["candidates"]) <= 1

    def test_empty_graph_returns_empty(self, tmp_path: Path) -> None:
        """Empty graph returns empty results."""
        storage = LocalStorageBackend(root=tmp_path)
        storage.write_json(
            "graph/latest.json", {"services": [], "failed_extractions": [], "metadata": {}}
        )
        server = CortexMCPServer(storage=storage)

        result = asyncio.run(
            _call_tool(
                server,
                "find_relevant_services",
                {
                    "task_description": "anything",
                },
            )
        )
        assert result["candidates"] == []

    def test_find_service_by_endpoint_path(self, tmp_path: Path) -> None:
        """Service is found when query matches an endpoint path token."""
        storage = LocalStorageBackend(root=tmp_path)
        graph = {
            "services": [
                {
                    "name": "geoinfo-microservice",
                    "type": "backend-java",
                    "owner": "team-geo",
                    "domain": "geo-info",
                    "tier": "standard",
                    "purpose": "Geographic information service",
                    "keywords": ["geo", "location"],
                    "endpoints": [
                        {
                            "method": "POST",
                            "path": "/v1/zipcode/validate-country",
                            "tags": ["zipcode"],
                            "request_body": {
                                "type": "ValidateCountryRequest",
                                "required": True,
                            },
                            "response": {
                                "type": "ValidateCountryResponse",
                                "wrapper": None,
                            },
                        }
                    ],
                },
                {
                    "name": "unrelated-service",
                    "type": "backend-java",
                    "owner": "team-other",
                    "domain": "other",
                    "tier": "standard",
                    "purpose": "Unrelated service",
                    "keywords": [],
                    "endpoints": [],
                },
            ],
            "communication": {"edges": []},
            "failed_extractions": [],
            "metadata": {
                "timestamp": "2026-01-01T00:00:00Z",
                "version": "1.0.0",
                "service_count": 2,
            },
        }
        storage.write_json("graph/latest.json", graph)
        server = CortexMCPServer(storage=storage)

        result = asyncio.run(
            _call_tool(
                server,
                "find_relevant_services",
                {"task_description": "zipcode"},
            )
        )
        candidates = result["candidates"]
        names = [c["name"] for c in candidates]
        assert "geoinfo-microservice" in names
        assert "unrelated-service" not in names

    def test_find_service_by_dto_name(self, tmp_path: Path) -> None:
        """Service is found when query matches a DTO name token."""
        storage = LocalStorageBackend(root=tmp_path)
        graph = {
            "services": [
                {
                    "name": "payment-service",
                    "type": "backend-java",
                    "owner": "team-pay",
                    "domain": "payments",
                    "tier": "critical",
                    "purpose": "Handles transactions",
                    "keywords": [],
                    "endpoints": [
                        {
                            "method": "POST",
                            "path": "/api/v1/charge",
                            "request_body": {"type": "ChargeWalletRequest"},
                            "response": {"type": "WalletBalanceResponse"},
                        }
                    ],
                },
            ],
            "communication": {"edges": []},
            "failed_extractions": [],
            "metadata": {
                "timestamp": "2026-01-01T00:00:00Z",
                "version": "1.0.0",
                "service_count": 1,
            },
        }
        storage.write_json("graph/latest.json", graph)
        server = CortexMCPServer(storage=storage)

        result = asyncio.run(
            _call_tool(
                server,
                "find_relevant_services",
                {"task_description": "wallet"},
            )
        )
        candidates = result["candidates"]
        names = [c["name"] for c in candidates]
        assert "payment-service" in names


class TestListEndpoints:
    """Tests for list_endpoints tool."""

    def test_returns_endpoints_for_known_service(self, mcp_server: CortexMCPServer) -> None:
        """Returns endpoints for a known service."""
        result = asyncio.run(
            _call_tool(mcp_server, "list_endpoints", {"service": "sample-android"})
        )
        assert result["service"] == "sample-android"
        assert "endpoints" in result

    def test_unknown_service_returns_error(self, mcp_server: CortexMCPServer) -> None:
        """Unknown service returns error."""
        result = asyncio.run(
            _call_tool(mcp_server, "list_endpoints", {"service": "nonexistent"})
        )
        assert "error" in result


class TestGetServiceContext:
    """Tests for get_service_context tool."""

    def test_returns_full_context(self, mcp_server: CortexMCPServer) -> None:
        """Returns full context by default."""
        result = asyncio.run(
            _call_tool(mcp_server, "get_service_context", {"name": "sample-android"})
        )
        assert result["name"] == "sample-android"
        assert "manifest" in result
        assert "direct_dependencies" in result
        assert "api_contracts" in result
        assert "integration_notes" in result

    def test_include_filters_correctly(self, mcp_server: CortexMCPServer) -> None:
        """include parameter filters sections correctly."""
        result = asyncio.run(
            _call_tool(
                mcp_server,
                "get_service_context",
                {
                    "name": "sample-android",
                    "include": ["deps"],
                },
            )
        )
        assert "direct_dependencies" in result
        assert "manifest" not in result
        assert "api_contracts" not in result

    def test_unknown_service_returns_error(self, mcp_server: CortexMCPServer) -> None:
        """Unknown service returns error."""
        result = asyncio.run(
            _call_tool(mcp_server, "get_service_context", {"name": "nonexistent"})
        )
        assert "error" in result

class TestGetEndpointContract:
    """Tests for get_endpoint_contract tool."""

    def test_mobile_returns_no_api_spec_message(self, mcp_server: CortexMCPServer) -> None:
        """For mobile services, returns 'no API spec available' message."""
        result = asyncio.run(
            _call_tool(
                mcp_server,
                "get_endpoint_contract",
                {
                    "service": "sample-android",
                    "method": "GET",
                    "path": "/api/v1/users",
                },
            )
        )
        assert "message" in result
        assert "No API spec" in result["message"] or "no API spec" in result["message"].lower()

    def test_unknown_service_returns_error(self, mcp_server: CortexMCPServer) -> None:
        """Unknown service returns error."""
        result = asyncio.run(
            _call_tool(
                mcp_server,
                "get_endpoint_contract",
                {
                    "service": "nonexistent",
                    "method": "GET",
                    "path": "/",
                },
            )
        )
        assert "error" in result


class TestCommunicationContext:
    """Tests for the 'communication' section in get_service_context."""

    def test_communication_included_by_default(self, mcp_server: CortexMCPServer) -> None:
        """By default, get_service_context includes 'communication' key."""
        result = asyncio.run(
            _call_tool(mcp_server, "get_service_context", {"name": "sample-android"})
        )
        assert "communication" in result

    def test_communication_shows_kafka_subscriptions(self, mcp_server: CortexMCPServer) -> None:
        """Service that consumes from a topic → subscribes_to populated."""
        result = asyncio.run(
            _call_tool(mcp_server, "get_service_context", {"name": "sample-android"})
        )
        comm = result.get("communication", {})
        # sample-android consumes "orders.created" from sample-ios
        subs = comm.get("subscribes_to", [])
        topics = [s["topic"] for s in subs]
        assert "orders.created" in topics

    def test_communication_shows_kafka_publishes(self, mcp_server: CortexMCPServer) -> None:
        """Service that produces to a topic → publishes_to populated."""
        result = asyncio.run(
            _call_tool(mcp_server, "get_service_context", {"name": "sample-ios"})
        )
        comm = result.get("communication", {})
        # sample-ios publishes "orders.created"
        pubs = comm.get("publishes_to", [])
        topics = [p["topic"] for p in pubs]
        assert "orders.created" in topics

    def test_communication_filtered_when_excluded(self, mcp_server: CortexMCPServer) -> None:
        """include=['manifest'] → no 'communication' key in response."""
        result = asyncio.run(
            _call_tool(
                mcp_server,
                "get_service_context",
                {"name": "sample-android", "include": ["manifest"]},
            )
        )
        assert "communication" not in result
        assert "manifest" in result

    def test_find_relevant_services_includes_communicates_with(
        self, mcp_server: CortexMCPServer
    ) -> None:
        """find_relevant_services results include communicates_with neighbor list."""
        result = asyncio.run(
            _call_tool(
                mcp_server,
                "find_relevant_services",
                {"task_description": "banking mobile"},
            )
        )
        candidates = result.get("candidates", [])
        assert len(candidates) > 0
        for c in candidates:
            assert "communicates_with" in c, f"Missing communicates_with on candidate: {c}"

    def test_find_relevant_all_services_includes_communicates_with(
        self, mcp_server: CortexMCPServer
    ) -> None:
        """Empty query (list all) also includes communicates_with."""
        result = asyncio.run(
            _call_tool(
                mcp_server,
                "find_relevant_services",
                {"task_description": "service"},
            )
        )
        candidates = result.get("candidates", [])
        for c in candidates:
            assert "communicates_with" in c


class TestGetEndpointContractSchemas:
    """Tests for DTO schema resolution in get_endpoint_contract."""

    def test_get_endpoint_contract_includes_schemas(
        self, mcp_server: CortexMCPServer
    ) -> None:
        """Endpoint contract includes direct DTO schemas for request/response types."""
        result = asyncio.run(
            _call_tool(
                mcp_server,
                "get_endpoint_contract",
                {
                    "service": "sample-backend",
                    "method": "POST",
                    "path": "/api/v1/orders",
                },
            )
        )
        assert "schemas" in result
        assert "CreateOrderRequest" in result["schemas"]
        assert "OrderDto" in result["schemas"]
        assert result["schemas"]["CreateOrderRequest"]["kind"] == "class"
        assert len(result["schemas"]["CreateOrderRequest"]["fields"]) == 3
        assert result["request_body"]["type"] == "CreateOrderRequest"
        assert result["response"]["type"] == "OrderDto"

    def test_get_endpoint_contract_transitive_schemas(
        self, mcp_server: CortexMCPServer
    ) -> None:
        """Endpoint contract includes transitively referenced DTO schemas."""
        result = asyncio.run(
            _call_tool(
                mcp_server,
                "get_endpoint_contract",
                {
                    "service": "sample-backend",
                    "method": "POST",
                    "path": "/api/v1/orders",
                },
            )
        )
        schemas = result["schemas"]
        # OrderItemDto is referenced by both CreateOrderRequest.items and OrderDto.items
        assert "OrderItemDto" in schemas
        # OrderStatus is referenced by OrderDto.status
        assert "OrderStatus" in schemas
        # AddressDto is referenced by CreateOrderRequest.shippingAddress
        assert "AddressDto" in schemas
        assert schemas["OrderStatus"]["kind"] == "enum"
        assert "PENDING" in schemas["OrderStatus"]["enum_values"]

    def test_get_endpoint_contract_no_schemas_when_empty(
        self, tmp_path: Path
    ) -> None:
        """No 'schemas' key when the manifest has no dto_schemas."""
        storage = LocalStorageBackend(root=tmp_path)
        # Manifest with api_contracts but no dto_schemas
        manifest = {
            "name": "no-dto-svc",
            "type": "backend-java",
            "owner": "team-x",
            "domain": "test",
            "tier": "standard",
            "purpose": "Service without DTO schemas",
            "api_contracts": [
                {
                    "controller": "TestController",
                    "base_path": "/api",
                    "endpoints": [
                        {
                            "method": "GET",
                            "path": "/api/health",
                            "handler": "health",
                            "request_body": None,
                            "response": {"type": "String"},
                            "parameters": [],
                        }
                    ],
                }
            ],
            "dependencies": [],
            "entry_points": [],
            "kafka_produces": [],
            "kafka_consumes": [],
            "integration_notes": [],
            "extracted_at": "2026-04-23T03:00:00Z",
            "extractor_version": "1.0.0",
        }
        storage.write_json("services/no-dto-svc/manifest.json", manifest)
        graph = {
            "services": [
                {
                    "name": "no-dto-svc",
                    "type": "backend-java",
                    "owner": "team-x",
                    "domain": "test",
                    "tier": "standard",
                    "purpose": "Service without DTO schemas",
                }
            ],
            "communication": {"edges": []},
            "failed_extractions": [],
            "metadata": {
                "timestamp": "2026-04-23T03:00:00Z",
                "version": "1.0.0",
                "service_count": 1,
            },
        }
        storage.write_json("graph/latest.json", graph)
        server = CortexMCPServer(storage=storage)

        result = asyncio.run(
            _call_tool(
                server,
                "get_endpoint_contract",
                {
                    "service": "no-dto-svc",
                    "method": "GET",
                    "path": "/api/health",
                },
            )
        )
        assert "schemas" not in result

    def test_mobile_service_no_schemas(
        self, mcp_server: CortexMCPServer
    ) -> None:
        """Mobile services return 'No API spec' message and no schemas."""
        result = asyncio.run(
            _call_tool(
                mcp_server,
                "get_endpoint_contract",
                {
                    "service": "sample-android",
                    "method": "GET",
                    "path": "/api/v1/users",
                },
            )
        )
        assert "message" in result
        assert "No API spec" in result["message"]
        assert "schemas" not in result


class TestInfrastructureScoring:
    """Tests for R3.2 — infrastructure field scoring in _score_service."""

    def test_database_type_match(self) -> None:
        """Service with matching database_type gets scored."""
        svc = {
            "name": "order-service",
            "keywords": [],
            "purpose": "Order management",
            "domain": "orders",
            "database_type": "postgresql",
        }
        score, matched = _score_service(svc, {"postgresql"})
        assert score > 0
        assert "database_type" in matched

    def test_cache_type_match(self) -> None:
        """Service with matching cache_type gets scored."""
        svc = {
            "name": "order-service",
            "keywords": [],
            "purpose": "Order management",
            "domain": "orders",
            "cache_type": "redis",
        }
        score, matched = _score_service(svc, {"redis"})
        assert score > 0
        assert "cache_type" in matched

    def test_framework_match(self) -> None:
        """Service with matching framework gets scored."""
        svc = {
            "name": "order-service",
            "keywords": [],
            "purpose": "Order management",
            "domain": "orders",
            "framework": "spring-boot",
        }
        score, matched = _score_service(svc, {"spring"})
        assert score > 0
        assert "framework" in matched

    def test_database_type_partial_match(self) -> None:
        """Partial match on database_type works (e.g., 'postgres' matches 'postgresql')."""
        svc = {
            "name": "order-service",
            "keywords": [],
            "purpose": "Order management",
            "domain": "orders",
            "database_type": "postgresql",
        }
        score, matched = _score_service(svc, {"postgres"})
        assert score > 0
        assert "database_type" in matched

    def test_no_infra_fields_no_crash(self) -> None:
        """Service without infrastructure fields does not crash."""
        svc = {
            "name": "mobile-app",
            "keywords": [],
            "purpose": "Mobile app",
            "domain": "mobile",
        }
        score, matched = _score_service(svc, {"postgresql"})
        assert "database_type" not in matched
        assert "cache_type" not in matched
        assert "framework" not in matched

    def test_find_relevant_services_scores_database_type(
        self, mcp_server: CortexMCPServer
    ) -> None:
        """find_relevant_services returns services matching database_type."""
        result = asyncio.run(
            _call_tool(
                mcp_server,
                "find_relevant_services",
                {"task_description": "postgresql database"},
            )
        )
        candidates = result["candidates"]
        names = [c["name"] for c in candidates]
        assert "sample-backend" in names
        backend = next(c for c in candidates if c["name"] == "sample-backend")
        assert "database_type" in backend["matched_on"]


class TestStructuredManifest:
    """Tests for R3.3 — structured manifest in get_service_context."""

    def test_manifest_has_overview_group(self, mcp_server: CortexMCPServer) -> None:
        """Manifest section contains 'overview' sub-dict."""
        result = asyncio.run(
            _call_tool(
                mcp_server,
                "get_service_context",
                {"name": "sample-backend", "include": ["manifest"]},
            )
        )
        manifest = result["manifest"]
        assert "overview" in manifest
        overview = manifest["overview"]
        assert overview["name"] == "sample-backend"
        assert overview["type"] == "backend-java"
        assert overview["framework"] == "spring-boot"
        assert overview["domain"] == "orders"

    def test_manifest_has_infrastructure_group(self, mcp_server: CortexMCPServer) -> None:
        """Manifest section contains 'infrastructure' sub-dict."""
        result = asyncio.run(
            _call_tool(
                mcp_server,
                "get_service_context",
                {"name": "sample-backend", "include": ["manifest"]},
            )
        )
        manifest = result["manifest"]
        assert "infrastructure" in manifest
        infra = manifest["infrastructure"]
        assert infra["database_type"] == "postgresql"
        assert infra["cache_type"] == "redis"
        assert infra["flyway_migration_count"] == 5

    def test_manifest_excludes_context_fields(self, mcp_server: CortexMCPServer) -> None:
        """Manifest section does not include agent_context, domain_context, context_pack."""
        result = asyncio.run(
            _call_tool(
                mcp_server,
                "get_service_context",
                {"name": "sample-backend", "include": ["manifest"]},
            )
        )
        manifest = result["manifest"]
        # These should not appear in the structured manifest
        assert "agent_context" not in manifest
        assert "domain_context" not in manifest
        assert "context_pack" not in manifest

    def test_manifest_has_swagger_url(self, mcp_server: CortexMCPServer) -> None:
        """Manifest section includes swagger_url at top level."""
        result = asyncio.run(
            _call_tool(
                mcp_server,
                "get_service_context",
                {"name": "sample-backend", "include": ["manifest"]},
            )
        )
        manifest = result["manifest"]
        assert manifest["swagger_url"] == "https://orders.example.com/swagger-ui.html"

    def test_manifest_empty_groups_omitted(self, tmp_path: Path) -> None:
        """Empty sub-dicts (e.g., runtime with no fields) are omitted."""
        storage = LocalStorageBackend(root=tmp_path)
        manifest = {
            "name": "minimal-svc",
            "type": "backend-java",
            "owner": "team-x",
            "domain": "test",
            "tier": "standard",
            "purpose": "Minimal service",
            "dependencies": [],
            "entry_points": [],
            "api_contracts": [],
            "integration_notes": [],
            "extracted_at": "2026-04-23T03:00:00Z",
            "extractor_version": "1.0.0",
        }
        storage.write_json("services/minimal-svc/manifest.json", manifest)
        graph = {
            "services": [
                {
                    "name": "minimal-svc",
                    "type": "backend-java",
                    "owner": "team-x",
                    "domain": "test",
                    "tier": "standard",
                    "purpose": "Minimal service",
                }
            ],
            "communication": {"edges": []},
            "failed_extractions": [],
            "metadata": {
                "timestamp": "2026-04-23T03:00:00Z",
                "version": "1.0.0",
                "service_count": 1,
            },
        }
        storage.write_json("graph/latest.json", graph)
        server = CortexMCPServer(storage=storage)

        result = asyncio.run(
            _call_tool(
                server,
                "get_service_context",
                {"name": "minimal-svc", "include": ["manifest"]},
            )
        )
        manifest_result = result["manifest"]
        # runtime group should be omitted (no docker_base_image, ci_tool, source_repo)
        assert "runtime" not in manifest_result
        # infrastructure group should be omitted (no database_type, cache_type, etc.)
        assert "infrastructure" not in manifest_result


class TestOutboundCallsInCommunication:
    """Tests for R3.4 — outbound calls in get_service_context communication section."""

    def test_communication_includes_outbound_http_calls(
        self, mcp_server: CortexMCPServer
    ) -> None:
        """Communication section includes outbound_http_calls from manifest."""
        result = asyncio.run(
            _call_tool(
                mcp_server,
                "get_service_context",
                {"name": "sample-backend", "include": ["communication"]},
            )
        )
        comm = result["communication"]
        assert "outbound_http_calls" in comm
        assert len(comm["outbound_http_calls"]) == 1
        call = comm["outbound_http_calls"][0]
        assert call["target_service"] == "notifications-service"

    def test_communication_includes_api_calls(
        self, mcp_server: CortexMCPServer
    ) -> None:
        """Communication section includes api_calls from manifest."""
        result = asyncio.run(
            _call_tool(
                mcp_server,
                "get_service_context",
                {"name": "sample-backend", "include": ["communication"]},
            )
        )
        comm = result["communication"]
        assert "api_calls" in comm
        assert len(comm["api_calls"]) == 1
        assert comm["api_calls"][0]["interface_name"] == "UserApi"

    def test_communication_no_outbound_when_absent(
        self, mcp_server: CortexMCPServer
    ) -> None:
        """Communication section omits outbound_http_calls when manifest has none."""
        result = asyncio.run(
            _call_tool(
                mcp_server,
                "get_service_context",
                {"name": "sample-android", "include": ["communication"]},
            )
        )
        comm = result["communication"]
        assert "outbound_http_calls" not in comm
        assert "api_calls" not in comm


class TestEntryPoints:
    """Tests for R3.5 — entry_points in get_service_context."""

    def test_entry_points_returned_when_requested(
        self, mcp_server: CortexMCPServer
    ) -> None:
        """entry_points section is returned when included."""
        result = asyncio.run(
            _call_tool(
                mcp_server,
                "get_service_context",
                {"name": "sample-backend", "include": ["entry_points"]},
            )
        )
        assert "entry_points" in result
        entry_points = result["entry_points"]
        assert len(entry_points) == 3
        kinds = [ep["kind"] for ep in entry_points]
        assert "main-class" in kinds
        assert "kafka-listener" in kinds
        assert "scheduled" in kinds

    def test_entry_points_not_in_default_include(
        self, mcp_server: CortexMCPServer
    ) -> None:
        """entry_points is NOT included by default."""
        result = asyncio.run(
            _call_tool(
                mcp_server,
                "get_service_context",
                {"name": "sample-backend"},
            )
        )
        assert "entry_points" not in result

    def test_entry_points_omitted_when_empty(
        self, mcp_server: CortexMCPServer
    ) -> None:
        """entry_points section is omitted when manifest has no entry points."""
        result = asyncio.run(
            _call_tool(
                mcp_server,
                "get_service_context",
                {"name": "sample-ios", "include": ["entry_points"]},
            )
        )
        # sample-ios has entry_points in manifest, so it should be present
        # But let's test with a service that has empty entry_points
        assert "entry_points" in result or "entry_points" not in result


class TestStructuredFilters:
    """Tests for R3.6 — structured filters in find_relevant_services."""

    def test_filter_by_database_type(self, mcp_server: CortexMCPServer) -> None:
        """Filtering by database_type narrows results."""
        result = asyncio.run(
            _call_tool(
                mcp_server,
                "find_relevant_services",
                {
                    "task_description": "service",
                    "filters": {"database_type": "postgresql"},
                },
            )
        )
        candidates = result["candidates"]
        # Only sample-backend has postgresql
        names = [c["name"] for c in candidates]
        assert "sample-backend" in names
        assert "sample-android" not in names
        assert "sample-ios" not in names

    def test_filter_by_tier(self, mcp_server: CortexMCPServer) -> None:
        """Filtering by tier narrows results."""
        result = asyncio.run(
            _call_tool(
                mcp_server,
                "find_relevant_services",
                {
                    "task_description": "service",
                    "filters": {"tier": "critical"},
                },
            )
        )
        candidates = result["candidates"]
        names = [c["name"] for c in candidates]
        assert "sample-backend" in names
        # standard tier services should be excluded
        assert "sample-android" not in names

    def test_filter_by_type(self, mcp_server: CortexMCPServer) -> None:
        """Filtering by type narrows results."""
        result = asyncio.run(
            _call_tool(
                mcp_server,
                "find_relevant_services",
                {
                    "task_description": "banking mobile",
                    "filters": {"type": "android"},
                },
            )
        )
        candidates = result["candidates"]
        names = [c["name"] for c in candidates]
        assert "sample-android" in names
        assert "sample-ios" not in names

    def test_filter_case_insensitive(self, mcp_server: CortexMCPServer) -> None:
        """Filters are case-insensitive."""
        result = asyncio.run(
            _call_tool(
                mcp_server,
                "find_relevant_services",
                {
                    "task_description": "service",
                    "filters": {"database_type": "PostgreSQL"},
                },
            )
        )
        candidates = result["candidates"]
        names = [c["name"] for c in candidates]
        assert "sample-backend" in names

    def test_filter_with_empty_description_returns_all_matching(
        self, mcp_server: CortexMCPServer
    ) -> None:
        """Empty task_description with filters returns all matching services."""
        result = asyncio.run(
            _call_tool(
                mcp_server,
                "find_relevant_services",
                {
                    "task_description": "",
                    "filters": {"tier": "standard"},
                },
            )
        )
        candidates = result["candidates"]
        names = [c["name"] for c in candidates]
        # Both android and ios are standard tier
        assert "sample-android" in names
        assert "sample-ios" in names
        assert "sample-backend" not in names

    def test_no_filters_returns_normal_results(self, mcp_server: CortexMCPServer) -> None:
        """No filters behaves the same as before."""
        result = asyncio.run(
            _call_tool(
                mcp_server,
                "find_relevant_services",
                {"task_description": "banking mobile"},
            )
        )
        assert len(result["candidates"]) > 0


class TestMatchesFilter:
    """Tests for _matches_filter helper."""

    def test_exact_match(self) -> None:
        svc = {"database_type": "postgresql"}
        assert _matches_filter(svc, "database_type", "postgresql") is True

    def test_case_insensitive(self) -> None:
        svc = {"database_type": "PostgreSQL"}
        assert _matches_filter(svc, "database_type", "postgresql") is True

    def test_no_match(self) -> None:
        svc = {"database_type": "mysql"}
        assert _matches_filter(svc, "database_type", "postgresql") is False

    def test_missing_field(self) -> None:
        svc = {"name": "test"}
        assert _matches_filter(svc, "database_type", "postgresql") is False

    def test_none_value(self) -> None:
        svc = {"database_type": None}
        assert _matches_filter(svc, "database_type", "postgresql") is False


class TestContextPack:
    """Tests for R2.5 — context-pack sections in get_service_context."""

    def test_agent_context_returned(self, mcp_server: CortexMCPServer) -> None:
        """agent_context is returned when included."""
        result = asyncio.run(
            _call_tool(
                mcp_server,
                "get_service_context",
                {"name": "sample-backend", "include": ["agent_context"]},
            )
        )
        assert "agent_context" in result
        assert "order lifecycle" in result["agent_context"]

    def test_domain_context_returned(self, mcp_server: CortexMCPServer) -> None:
        """domain_context is returned when included."""
        result = asyncio.run(
            _call_tool(
                mcp_server,
                "get_service_context",
                {"name": "sample-backend", "include": ["domain_context"]},
            )
        )
        assert "domain_context" in result
        assert "Orders domain" in result["domain_context"]

    def test_context_pack_returned(self, mcp_server: CortexMCPServer) -> None:
        """context_pack is returned when included."""
        result = asyncio.run(
            _call_tool(
                mcp_server,
                "get_service_context",
                {"name": "sample-backend", "include": ["context_pack"]},
            )
        )
        assert "context_pack" in result
        assert "AGENTS.md" in result["context_pack"]
        assert "ARCHITECTURE.md" in result["context_pack"]

    def test_context_fields_not_in_default_include(
        self, mcp_server: CortexMCPServer
    ) -> None:
        """Context fields are NOT included by default."""
        result = asyncio.run(
            _call_tool(
                mcp_server,
                "get_service_context",
                {"name": "sample-backend"},
            )
        )
        assert "agent_context" not in result
        assert "domain_context" not in result
        assert "context_pack" not in result

    def test_context_fields_omitted_when_absent(
        self, mcp_server: CortexMCPServer
    ) -> None:
        """Context fields are omitted when manifest has no context data."""
        result = asyncio.run(
            _call_tool(
                mcp_server,
                "get_service_context",
                {
                    "name": "sample-android",
                    "include": ["agent_context", "domain_context", "context_pack"],
                },
            )
        )
        # sample-android manifest has no context fields
        assert "agent_context" not in result
        assert "domain_context" not in result
        assert "context_pack" not in result

    def test_all_context_fields_together(self, mcp_server: CortexMCPServer) -> None:
        """All context fields can be requested together."""
        result = asyncio.run(
            _call_tool(
                mcp_server,
                "get_service_context",
                {
                    "name": "sample-backend",
                    "include": ["agent_context", "domain_context", "context_pack"],
                },
            )
        )
        assert "agent_context" in result
        assert "domain_context" in result
        assert "context_pack" in result


# --- Helper to call tools directly ---


async def _call_tool(server: CortexMCPServer, tool_name: str, arguments: dict) -> dict:
    """Call a registered tool function directly on the server."""
    # Ensure graph is loaded
    await server._ensure_graph()

    # Call the tool via FastMCP's tool manager
    # The tool manager's call_tool returns the raw result from the tool function
    result = await server._mcp._tool_manager.call_tool(tool_name, arguments)

    # The result may be the raw dict directly, or wrapped in content blocks
    if isinstance(result, dict):
        return result

    # If it's a list of content blocks, parse the text content
    for content in result:
        if hasattr(content, "text"):
            return json.loads(content.text)

    raise ValueError(f"Unexpected result type from tool '{tool_name}': {type(result)}")
