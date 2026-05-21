"""Tests for MCP server tools.

Tests all 4 tools against a fixture graph built from Android + iOS manifests.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from cortex.storage import LocalStorageBackend
from mcp_server.server import CortexMCPServer, _score_service, _tokenize


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
        "entry_points": [],
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
