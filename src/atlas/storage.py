"""Storage backend abstraction — local filesystem and GCS.

Factory pattern: StorageBackend.from_config(backend="local", bucket="/tmp/atlas-data")
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from pathlib import Path


class StorageError(Exception):
    """Raised on storage operation failures."""


class StorageBackend(ABC):
    """Abstract base class for storage backends."""

    @abstractmethod
    def write_json(self, key: str, data: dict) -> None:
        """Write a JSON-serializable dict to storage."""
        ...

    @abstractmethod
    def write_bytes(self, key: str, data: bytes) -> None:
        """Write raw bytes to storage."""
        ...

    @abstractmethod
    def read_json(self, key: str) -> dict:
        """Read a JSON object from storage."""
        ...

    @abstractmethod
    def read_bytes(self, key: str) -> bytes:
        """Read raw bytes from storage."""
        ...

    @abstractmethod
    def list(self, prefix: str) -> list[str]:
        """List all keys under a given prefix."""
        ...

    @abstractmethod
    def exists(self, key: str) -> bool:
        """Check if a key exists in storage."""
        ...

    @classmethod
    def from_config(cls, backend: str, bucket: str, **kwargs: object) -> StorageBackend:
        """Factory: create the appropriate storage backend.

        Args:
            backend: "local" or "gcs"
            bucket: For local, the root directory path. For GCS, the bucket name.
        """
        if backend == "local":
            return LocalStorageBackend(root=Path(bucket))
        elif backend == "gcs":
            return GCSStorageBackend(bucket_name=bucket, **kwargs)
        else:
            raise StorageError(f"Unknown storage backend: '{backend}'. Supported: local, gcs")


class LocalStorageBackend(StorageBackend):
    """Local filesystem storage backend. The bucket param is the root directory."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _resolve(self, key: str) -> Path:
        return self.root / key

    def write_json(self, key: str, data: dict) -> None:
        path = self._resolve(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            json.dump(data, f, indent=2, default=str)

    def write_bytes(self, key: str, data: bytes) -> None:
        path = self._resolve(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)

    def read_json(self, key: str) -> dict:
        path = self._resolve(key)
        if not path.exists():
            raise StorageError(f"Key not found: {key}")
        with open(path) as f:
            return json.load(f)

    def read_bytes(self, key: str) -> bytes:
        path = self._resolve(key)
        if not path.exists():
            raise StorageError(f"Key not found: {key}")
        with open(path, "rb") as f:
            return f.read()

    def list(self, prefix: str) -> list[str]:
        prefix_path = self._resolve(prefix)
        if not prefix_path.exists():
            return []
        results: list[str] = []
        for p in prefix_path.rglob("*"):
            if p.is_file():
                results.append(str(p.relative_to(self.root)))
        return sorted(results)

    def exists(self, key: str) -> bool:
        return self._resolve(key).exists()


class GCSStorageBackend(StorageBackend):
    """Google Cloud Storage backend."""

    def __init__(self, bucket_name: str, **kwargs: object):
        try:
            from google.cloud import storage as gcs_storage
        except ImportError as e:
            raise StorageError(
                "google-cloud-storage package is required for GCS backend. "
                "Install with: pip install google-cloud-storage"
            ) from e

        self._client = gcs_storage.Client()
        self._bucket = self._client.bucket(bucket_name)

    def write_json(self, key: str, data: dict) -> None:
        blob = self._bucket.blob(key)
        blob.upload_from_string(
            json.dumps(data, indent=2, default=str),
            content_type="application/json",
        )

    def write_bytes(self, key: str, data: bytes) -> None:
        blob = self._bucket.blob(key)
        blob.upload_from_string(data, content_type="application/octet-stream")

    def read_json(self, key: str) -> dict:
        blob = self._bucket.blob(key)
        if not blob.exists():
            raise StorageError(f"Key not found in GCS: {key}")
        content = blob.download_as_text()
        return json.loads(content)

    def read_bytes(self, key: str) -> bytes:
        blob = self._bucket.blob(key)
        if not blob.exists():
            raise StorageError(f"Key not found in GCS: {key}")
        return blob.download_as_bytes()

    def list(self, prefix: str) -> list[str]:
        blobs = self._client.list_blobs(self._bucket, prefix=prefix)
        return sorted(blob.name for blob in blobs)

    def exists(self, key: str) -> bool:
        return self._bucket.blob(key).exists()
