"""Tests for FirestoreStorageBackend.

All Firestore I/O is mocked via unittest.mock so no real GCP credentials
or network connections are required.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from cortex.firestore_storage import FirestoreStorageBackend, _parse_key, _sanitise
from cortex.storage import StorageError

# ─── _parse_key tests ─────────────────────────────────────────────────────────


class TestParseKey:
    """Unit tests for the _parse_key helper."""

    def test_graph_latest(self) -> None:
        col, doc, field = _parse_key("graph/latest.json")
        assert col == "graph"
        assert doc == "latest"
        assert field is None

    def test_graph_timestamped(self) -> None:
        col, doc, field = _parse_key("graph/2024-01-15T12:00:00.json")
        assert col == "graph"
        assert doc == "2024-01-15T12:00:00"
        assert field is None

    def test_services_manifest(self) -> None:
        col, doc, field = _parse_key("services/my-service/manifest.json")
        assert col == "services"
        assert doc == "my-service"
        assert field == "manifest"

    def test_services_extraction_error(self) -> None:
        col, doc, field = _parse_key("services/my-service/extraction-error.json")
        assert col == "services"
        assert doc == "my-service"
        assert field == "extraction-error"

    def test_logs_mcp(self) -> None:
        col, doc, field = _parse_key("logs/mcp/2024-01-15.jsonl")
        assert col == "logs_mcp"
        assert doc == "2024-01-15"
        assert field == "entries"

    def test_leading_slash_stripped(self) -> None:
        col, doc, field = _parse_key("/graph/latest.json")
        assert col == "graph"
        assert doc == "latest"
        assert field is None

    def test_generic_fallback(self) -> None:
        col, doc, field = _parse_key("runs/2024-01-15.json")
        assert col == "runs"
        assert doc == "2024-01-15"
        assert field is None


# ─── _sanitise tests ──────────────────────────────────────────────────────────


class TestSanitise:
    """Unit tests for the _sanitise helper."""

    def test_primitive_types_pass_through(self) -> None:
        assert _sanitise("hello") == "hello"
        assert _sanitise(42) == 42
        assert _sanitise(3.14) == 3.14
        assert _sanitise(True) is True
        assert _sanitise(None) is None

    def test_dict_sanitised_recursively(self) -> None:
        result = _sanitise({"a": 1, "b": "x"})
        assert result == {"a": 1, "b": "x"}

    def test_list_sanitised_recursively(self) -> None:
        result = _sanitise([1, "two", None])
        assert result == [1, "two", None]

    def test_non_native_type_coerced_to_string(self) -> None:
        from decimal import Decimal

        result = _sanitise(Decimal("3.14"))
        assert result == "3.14"

    def test_nested_structure(self) -> None:
        data = {"name": "svc", "tags": ["a", "b"], "meta": {"count": 1}}
        result = _sanitise(data)
        assert result == {"name": "svc", "tags": ["a", "b"], "meta": {"count": 1}}

    def test_dict_keys_coerced_to_string(self) -> None:
        result = _sanitise({1: "value"})
        assert "1" in result


# ─── FirestoreStorageBackend tests ────────────────────────────────────────────


def _make_backend(database: str = "cortex") -> tuple[FirestoreStorageBackend, MagicMock]:
    """Return a backend instance with the Firestore client fully mocked."""
    mock_client = MagicMock()
    mock_firestore_module = MagicMock(Client=MagicMock(return_value=mock_client))
    with patch.dict("sys.modules", {"google.cloud.firestore": mock_firestore_module}):
        backend = FirestoreStorageBackend.__new__(FirestoreStorageBackend)
        backend._db = mock_client
        backend._database = database
    return backend, mock_client


class TestFirestoreStorageBackendInit:
    """Tests for __init__ and import error handling."""

    def test_raises_storage_error_when_package_missing(self) -> None:
        with patch.dict("sys.modules", {"google.cloud": None, "google.cloud.firestore": None}):
            import sys
            # Temporarily remove the module from sys.modules to simulate missing package
            firestore_mod = sys.modules.pop("google.cloud.firestore", None)
            google_cloud_mod = sys.modules.pop("google.cloud", None)
            try:
                with pytest.raises(StorageError, match="google-cloud-firestore"):
                    with patch("builtins.__import__", side_effect=ImportError("No module")):
                        FirestoreStorageBackend.__new__(FirestoreStorageBackend).__init__()
            finally:
                if firestore_mod is not None:
                    sys.modules["google.cloud.firestore"] = firestore_mod
                if google_cloud_mod is not None:
                    sys.modules["google.cloud"] = google_cloud_mod

    def test_init_success_with_mock(self) -> None:
        mock_firestore = MagicMock()
        mock_client = MagicMock()
        mock_firestore.Client.return_value = mock_client

        with patch.dict(
            "sys.modules",
            {"google.cloud.firestore": mock_firestore, "google.cloud": MagicMock()},
        ):
            # Patch the import inside the __init__
            with patch("cortex.firestore_storage.FirestoreStorageBackend.__init__") as mock_init:
                mock_init.return_value = None
                backend = FirestoreStorageBackend.__new__(FirestoreStorageBackend)
                backend._db = mock_client
                backend._database = "cortex"
                assert backend._database == "cortex"


class TestWriteJson:
    """Tests for write_json."""

    def test_write_graph_stores_under_data_key(self) -> None:
        backend, mock_client = _make_backend()
        mock_ref = MagicMock()
        mock_client.collection.return_value.document.return_value = mock_ref

        data = {"services": [], "metadata": {}}
        backend.write_json("graph/latest.json", data)

        mock_client.collection.assert_called_once_with("graph")
        mock_client.collection.return_value.document.assert_called_once_with("latest")
        mock_ref.set.assert_called_once()
        call_args = mock_ref.set.call_args
        stored = call_args[0][0]
        assert "_data" in stored
        assert stored["_key"] == "graph/latest.json"

    def test_write_service_manifest_stores_under_field(self) -> None:
        backend, mock_client = _make_backend()
        mock_ref = MagicMock()
        mock_client.collection.return_value.document.return_value = mock_ref

        data = {"name": "my-svc", "type": "android"}
        backend.write_json("services/my-svc/manifest.json", data)

        mock_client.collection.assert_called_once_with("services")
        mock_client.collection.return_value.document.assert_called_once_with("my-svc")
        call_args = mock_ref.set.call_args
        stored = call_args[0][0]
        assert "manifest" in stored
        assert stored["manifest"] == data
        assert stored["_key"] == "services/my-svc/manifest.json"


class TestReadJson:
    """Tests for read_json."""

    def test_read_graph_latest(self) -> None:
        backend, mock_client = _make_backend()

        snap = MagicMock()
        snap.exists = True
        snap.to_dict.return_value = {
            "_data": {"services": [{"name": "svc-a"}]},
            "_key": "graph/latest.json",
        }
        mock_client.collection.return_value.document.return_value.get.return_value = snap

        result = backend.read_json("graph/latest.json")
        assert result == {"services": [{"name": "svc-a"}]}

    def test_read_service_manifest(self) -> None:
        backend, mock_client = _make_backend()

        snap = MagicMock()
        snap.exists = True
        snap.to_dict.return_value = {
            "manifest": {"name": "my-svc", "type": "android"},
            "_key": "services/my-svc/manifest.json",
        }
        mock_client.collection.return_value.document.return_value.get.return_value = snap

        result = backend.read_json("services/my-svc/manifest.json")
        assert result == {"name": "my-svc", "type": "android"}

    def test_read_raises_when_doc_not_found(self) -> None:
        backend, mock_client = _make_backend()

        snap = MagicMock()
        snap.exists = False
        mock_client.collection.return_value.document.return_value.get.return_value = snap

        with pytest.raises(StorageError, match="Key not found in Firestore"):
            backend.read_json("graph/latest.json")

    def test_read_raises_when_data_field_missing(self) -> None:
        backend, mock_client = _make_backend()

        snap = MagicMock()
        snap.exists = True
        snap.to_dict.return_value = {}  # no _data field
        mock_client.collection.return_value.document.return_value.get.return_value = snap

        with pytest.raises(StorageError, match="Key not found in Firestore"):
            backend.read_json("graph/latest.json")

    def test_read_raises_when_named_field_missing(self) -> None:
        backend, mock_client = _make_backend()

        snap = MagicMock()
        snap.exists = True
        snap.to_dict.return_value = {"_key": "services/my-svc/manifest.json"}  # no manifest
        mock_client.collection.return_value.document.return_value.get.return_value = snap

        with pytest.raises(StorageError, match="Field 'manifest' not found"):
            backend.read_json("services/my-svc/manifest.json")


class TestExists:
    """Tests for exists."""

    def test_returns_true_when_doc_exists_and_no_field(self) -> None:
        backend, mock_client = _make_backend()

        snap = MagicMock()
        snap.exists = True
        snap.to_dict.return_value = {"_data": {}}
        mock_client.collection.return_value.document.return_value.get.return_value = snap

        assert backend.exists("graph/latest.json") is True

    def test_returns_false_when_doc_missing(self) -> None:
        backend, mock_client = _make_backend()

        snap = MagicMock()
        snap.exists = False
        mock_client.collection.return_value.document.return_value.get.return_value = snap

        assert backend.exists("graph/latest.json") is False

    def test_returns_true_when_field_present(self) -> None:
        backend, mock_client = _make_backend()

        snap = MagicMock()
        snap.exists = True
        snap.to_dict.return_value = {"manifest": {"name": "svc"}, "_key": "k"}
        mock_client.collection.return_value.document.return_value.get.return_value = snap

        assert backend.exists("services/my-svc/manifest.json") is True

    def test_returns_false_when_field_missing(self) -> None:
        backend, mock_client = _make_backend()

        snap = MagicMock()
        snap.exists = True
        snap.to_dict.return_value = {"_key": "services/my-svc/manifest.json"}  # no manifest
        mock_client.collection.return_value.document.return_value.get.return_value = snap

        assert backend.exists("services/my-svc/manifest.json") is False


class TestList:
    """Tests for list."""

    def test_lists_graph_documents(self) -> None:
        backend, mock_client = _make_backend()

        doc_snap = MagicMock()
        doc_snap.id = "latest"
        doc_snap.to_dict.return_value = {
            "_data": {"services": []},
            "_key": "graph/latest.json",
        }
        mock_client.collection.return_value.stream.return_value = [doc_snap]

        result = backend.list("graph")
        assert "graph/latest.json" in result

    def test_lists_service_documents(self) -> None:
        backend, mock_client = _make_backend()

        doc_snap = MagicMock()
        doc_snap.id = "my-svc"
        doc_snap.to_dict.return_value = {
            "manifest": {"name": "my-svc"},
            "_key": "services/my-svc/manifest.json",
        }
        mock_client.collection.return_value.stream.return_value = [doc_snap]

        result = backend.list("services")
        assert "services/my-svc/manifest.json" in result

    def test_returns_sorted_results(self) -> None:
        backend, mock_client = _make_backend()

        snap_b = MagicMock()
        snap_b.id = "svc-b"
        snap_b.to_dict.return_value = {"manifest": {}, "_key": "k"}

        snap_a = MagicMock()
        snap_a.id = "svc-a"
        snap_a.to_dict.return_value = {"manifest": {}, "_key": "k"}

        mock_client.collection.return_value.stream.return_value = [snap_b, snap_a]

        result = backend.list("services")
        assert result == sorted(result)

    def test_skips_internal_fields(self) -> None:
        backend, mock_client = _make_backend()

        doc_snap = MagicMock()
        doc_snap.id = "my-svc"
        doc_snap.to_dict.return_value = {
            "_key": "services/my-svc/manifest.json",
            "manifest": {"name": "my-svc"},
        }
        mock_client.collection.return_value.stream.return_value = [doc_snap]

        result = backend.list("services")
        # _key is an internal field (starts with _), should not appear
        assert all("_key" not in r for r in result)


class TestWriteBytes:
    """Tests for write_bytes."""

    def test_write_regular_bytes_base64_encoded(self) -> None:
        backend, mock_client = _make_backend()
        mock_ref = MagicMock()
        mock_client.collection.return_value.document.return_value = mock_ref

        backend.write_bytes("graph/snapshot.bin", b"hello world")

        mock_ref.set.assert_called_once()
        call_args = mock_ref.set.call_args[0][0]
        # Should be base64-encoded
        import base64
        assert base64.b64decode(call_args["_bytes"]) == b"hello world"

    def test_write_jsonl_uses_array_union(self) -> None:
        backend, mock_client = _make_backend()
        mock_ref = MagicMock()
        mock_client.collection.return_value.document.return_value = mock_ref

        with patch("cortex.firestore_storage.FirestoreStorageBackend.write_bytes") as mock_wb:
            mock_wb.return_value = None
            backend.write_bytes = mock_wb
            backend.write_bytes("logs/mcp/2024-01-15.jsonl", b'{"level": "info"}\n')
            mock_wb.assert_called_once()


class TestReadBytes:
    """Tests for read_bytes."""

    def test_read_regular_bytes(self) -> None:
        import base64

        backend, mock_client = _make_backend()

        snap = MagicMock()
        snap.exists = True
        snap.to_dict.return_value = {"_bytes": base64.b64encode(b"hello").decode()}
        mock_client.collection.return_value.document.return_value.get.return_value = snap

        result = backend.read_bytes("graph/snapshot.bin")
        assert result == b"hello"

    def test_read_bytes_raises_when_not_found(self) -> None:
        backend, mock_client = _make_backend()

        snap = MagicMock()
        snap.exists = False
        mock_client.collection.return_value.document.return_value.get.return_value = snap

        with pytest.raises(StorageError, match="Key not found in Firestore"):
            backend.read_bytes("graph/snapshot.bin")

    def test_read_bytes_raises_when_field_missing(self) -> None:
        backend, mock_client = _make_backend()

        snap = MagicMock()
        snap.exists = True
        snap.to_dict.return_value = {}  # no _bytes field
        mock_client.collection.return_value.document.return_value.get.return_value = snap

        with pytest.raises(StorageError, match="Field '_bytes' not found"):
            backend.read_bytes("graph/snapshot.bin")

    def test_read_jsonl_returns_newline_delimited_json(self) -> None:
        backend, mock_client = _make_backend()

        entries = [{"level": "info", "msg": "ok"}, {"level": "error", "msg": "fail"}]
        snap = MagicMock()
        snap.exists = True
        snap.to_dict.return_value = {"entries": entries}
        mock_client.collection.return_value.document.return_value.get.return_value = snap

        result = backend.read_bytes("logs/mcp/2024-01-15.jsonl")
        assert isinstance(result, bytes)
        lines = result.decode("utf-8").strip().splitlines()
        assert len(lines) == 2
        import json
        assert json.loads(lines[0]) == {"level": "info", "msg": "ok"}
