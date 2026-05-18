"""Tests for storage backend."""

from __future__ import annotations

from pathlib import Path

import pytest

from cortex.storage import LocalStorageBackend, StorageBackend, StorageError


class TestLocalStorageBackend:
    """Tests for LocalStorageBackend."""

    def test_write_and_read_json(self, tmp_storage: Path) -> None:
        """JSON round-trip: write_json then read_json."""
        backend = LocalStorageBackend(root=tmp_storage)
        data = {"name": "test", "value": 42, "nested": {"key": "val"}}
        backend.write_json("test/data.json", data)
        result = backend.read_json("test/data.json")
        assert result == data

    def test_write_and_read_bytes(self, tmp_storage: Path) -> None:
        """Bytes round-trip: write_bytes then read_bytes."""
        backend = LocalStorageBackend(root=tmp_storage)
        data = b"hello bytes \x00\x01\x02"
        backend.write_bytes("test/raw.bin", data)
        result = backend.read_bytes("test/raw.bin")
        assert result == data

    def test_read_json_not_found(self, tmp_storage: Path) -> None:
        """read_json for missing key raises StorageError."""
        backend = LocalStorageBackend(root=tmp_storage)
        with pytest.raises(StorageError, match="Key not found"):
            backend.read_json("nonexistent.json")

    def test_read_bytes_not_found(self, tmp_storage: Path) -> None:
        """read_bytes for missing key raises StorageError."""
        backend = LocalStorageBackend(root=tmp_storage)
        with pytest.raises(StorageError, match="Key not found"):
            backend.read_bytes("nonexistent.bin")

    def test_list_with_prefix(self, tmp_storage: Path) -> None:
        """list() returns files under a prefix."""
        backend = LocalStorageBackend(root=tmp_storage)
        backend.write_json("services/app-a/manifest.json", {"name": "a"})
        backend.write_json("services/app-b/manifest.json", {"name": "b"})
        backend.write_json("graph/latest.json", {"services": []})

        result = backend.list("services")
        assert len(result) == 2
        assert "services/app-a/manifest.json" in result
        assert "services/app-b/manifest.json" in result

    def test_list_empty_prefix(self, tmp_storage: Path) -> None:
        """list() with non-existent prefix returns empty list."""
        backend = LocalStorageBackend(root=tmp_storage)
        result = backend.list("nonexistent")
        assert result == []

    def test_exists_true(self, tmp_storage: Path) -> None:
        """exists() returns True for existing key."""
        backend = LocalStorageBackend(root=tmp_storage)
        backend.write_json("test/exists.json", {"ok": True})
        assert backend.exists("test/exists.json") is True

    def test_exists_false(self, tmp_storage: Path) -> None:
        """exists() returns False for non-existing key."""
        backend = LocalStorageBackend(root=tmp_storage)
        assert backend.exists("test/nope.json") is False

    def test_write_creates_subdirectories(self, tmp_storage: Path) -> None:
        """write_json creates nested subdirectories automatically."""
        backend = LocalStorageBackend(root=tmp_storage)
        backend.write_json("a/b/c/d/deep.json", {"deep": True})
        assert backend.exists("a/b/c/d/deep.json")
        result = backend.read_json("a/b/c/d/deep.json")
        assert result["deep"] is True


class TestStorageBackendFactory:
    """Tests for StorageBackend.from_config factory."""

    def test_creates_local_backend(self, tmp_storage: Path) -> None:
        """Factory creates LocalStorageBackend for 'local'."""
        backend = StorageBackend.from_config("local", str(tmp_storage))
        assert isinstance(backend, LocalStorageBackend)

    def test_unknown_backend_raises(self) -> None:
        """Unknown backend name raises StorageError."""
        with pytest.raises(StorageError, match="Unknown storage backend"):
            StorageBackend.from_config("s3", "my-bucket")
