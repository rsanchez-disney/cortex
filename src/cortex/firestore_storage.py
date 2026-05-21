"""Firestore storage backend for the Cortex MCP server on Cloud Run.

Maps the existing key-based storage API onto a Firestore named database.

Document layout
---------------
Storage key                       → Firestore location
─────────────────────────────────────────────────────────────────────────────
graph/latest.json                 → collection "graph"  / doc "latest"
services/{name}/manifest.json     → collection "services" / doc "{name}"
                                    stored as field "manifest" (nested map)
logs/mcp/{date}.jsonl             → collection "logs_mcp" / doc "{date}"
                                    stored as field "entries" (array, appended)

The MCP server only uses read_json / write_json / exists at runtime.
list(), read_bytes(), write_bytes() are implemented for interface completeness
and to support future tooling, but are not performance-critical paths.

SAFETY: This module contains NO delete or clear operations. All writes use
set(..., merge=True) (upsert semantics) — existing documents are never removed.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from cortex.storage import StorageBackend, StorageError

if TYPE_CHECKING:
    from google.cloud.firestore_v1 import Client as FirestoreClient  # type: ignore[import]
    from google.cloud.firestore_v1.base_document import DocumentSnapshot  # type: ignore[import]


# ─── Key parsing helpers ──────────────────────────────────────────────────────


def _parse_key(key: str) -> tuple[str, str, str | None]:
    """Parse a storage key into (collection, document, sub_field|None).

    Examples:
        "graph/latest.json"            → ("graph", "latest", None)
        "services/my-svc/manifest.json" → ("services", "my-svc", "manifest")
        "logs/mcp/2024-01-15.jsonl"   → ("logs_mcp", "2024-01-15", "entries")
    """
    # Normalise: strip leading slash, remove .json / .jsonl extension
    key = key.lstrip("/")
    parts = key.split("/")

    if key.startswith("graph/"):
        doc = parts[1].replace(".json", "").replace(".jsonl", "")
        return "graph", doc, None

    if key.startswith("services/") and len(parts) >= 3:
        svc_name = parts[1]
        # field name derived from the filename without extension
        field = parts[2].replace(".json", "").replace(".jsonl", "")
        return "services", svc_name, field

    if key.startswith("logs/mcp/"):
        date_doc = parts[2].replace(".jsonl", "").replace(".json", "")
        return "logs_mcp", date_doc, "entries"

    # Generic fallback: top-level collection from first segment
    collection = parts[0]
    doc = "_".join(parts[1:]).replace(".json", "").replace(".jsonl", "") or "default"
    return collection, doc, None


# ─── Backend implementation ───────────────────────────────────────────────────


class FirestoreStorageBackend(StorageBackend):
    """Google Cloud Firestore storage backend.

    Args:
        database: Name of the Firestore database (default: "cortex").
                  Passed as the ``database`` kwarg to the Firestore client so
                  it targets the named database, fully isolated from the default
                  database and Flow's ``archon-prod`` database.
    """

    def __init__(self, database: str = "cortex", project: str | None = None) -> None:
        try:
            from google.cloud import firestore as _firestore  # type: ignore[import]
        except ImportError as exc:
            raise StorageError(
                "google-cloud-firestore package is required for the Firestore backend. "
                "Install with: pip install google-cloud-firestore"
            ) from exc

        kwargs: dict[str, str] = {"database": database}
        if project:
            kwargs["project"] = project
        self._db: FirestoreClient = _firestore.Client(**kwargs)
        self._database = database

    # ── write_json ─────────────────────────────────────────────────────────────

    def write_json(self, key: str, data: dict) -> None:
        """Upsert a JSON-serialisable dict into Firestore.

        Uses merge=True so existing fields not present in *data* are preserved.
        NEVER deletes or overwrites the entire document unless all fields match.
        """
        collection, doc_id, field = _parse_key(key)
        ref = self._db.collection(collection).document(doc_id)

        if field is None:
            # Store the entire dict as document fields (top-level)
            ref.set({"_data": _sanitise(data), "_key": key}, merge=True)
        else:
            # Store under a named field so sibling fields are preserved
            ref.set({field: _sanitise(data), "_key": key}, merge=True)

    # ── read_json ──────────────────────────────────────────────────────────────

    def read_json(self, key: str) -> dict:
        """Read a JSON object from Firestore.

        Raises StorageError if the document or expected field does not exist.
        """
        collection, doc_id, field = _parse_key(key)
        ref = self._db.collection(collection).document(doc_id)
        snap: DocumentSnapshot = ref.get()

        if not snap.exists:
            raise StorageError(f"Key not found in Firestore ({self._database}): {key}")

        doc_data: dict[str, Any] = snap.to_dict() or {}

        if field is None:
            payload = doc_data.get("_data")
            if payload is None:
                raise StorageError(f"Key not found in Firestore ({self._database}): {key}")
            return payload  # type: ignore[return-value]
        else:
            payload = doc_data.get(field)
            if payload is None:
                raise StorageError(
                    f"Field '{field}' not found in Firestore doc "
                    f"{collection}/{doc_id} ({self._database})"
                )
            return payload  # type: ignore[return-value]

    # ── exists ─────────────────────────────────────────────────────────────────

    def exists(self, key: str) -> bool:
        """Return True if the key's document (and field, if applicable) exists.

        Infrastructure errors (network, auth, permissions) are NOT caught —
        they propagate so callers can distinguish "key missing" from
        "Firestore unreachable".  Firestore's ``ref.get()`` returns a snapshot
        with ``exists=False`` for non-existent documents; it does not raise.
        """
        collection, doc_id, field = _parse_key(key)
        ref = self._db.collection(collection).document(doc_id)
        snap: DocumentSnapshot = ref.get()
        if not snap.exists:
            return False
        if field is not None:
            doc_data: dict[str, Any] = snap.to_dict() or {}
            return field in doc_data
        return True

    # ── list ───────────────────────────────────────────────────────────────────

    def list(self, prefix: str) -> list[str]:
        """List all keys whose storage path starts with *prefix*.

        For services/, returns ["services/{name}/manifest"] per document.
        For graph/, returns ["graph/latest"].
        """
        prefix = prefix.rstrip("/")
        collection, _, _ = _parse_key(prefix + "/placeholder.json")
        results: list[str] = []
        for doc_ref in self._db.collection(collection).stream():
            snap: DocumentSnapshot = doc_ref  # type: ignore[assignment]
            doc_data: dict[str, Any] = snap.to_dict() or {}
            if "_data" in doc_data:
                # Top-level document (e.g. graph/latest) — stored via write_json
                # with field=None.  Reconstruct the key from the document ID.
                results.append(f"{collection}/{snap.id}.json")
            else:
                # Field-based document (e.g. services/{name}/manifest) — each
                # non-internal field is a separate logical key.
                for field_name in doc_data:
                    if field_name.startswith("_"):
                        continue
                    results.append(f"{collection}/{snap.id}/{field_name}.json")
        return sorted(results)

    # ── write_bytes / read_bytes ───────────────────────────────────────────────

    def write_bytes(self, key: str, data: bytes) -> None:
        """Store raw bytes as a base64-encoded string field in Firestore.

        Used for JSONL log files and similar small binary payloads.
        For the logs/mcp/{date} pattern, appends line-delimited entries.
        """
        import base64

        collection, doc_id, field = _parse_key(key)
        ref = self._db.collection(collection).document(doc_id)
        field_name = field or "_bytes"

        # For log files: decode text lines and store as an array (append)
        if key.endswith(".jsonl"):
            lines = [
                line for line in data.decode("utf-8", errors="replace").splitlines() if line.strip()
            ]
            parsed_entries: list[Any] = []
            for line in lines:
                try:
                    parsed_entries.append(json.loads(line))
                except json.JSONDecodeError:
                    parsed_entries.append({"_raw": line})

            # Use ArrayUnion so new entries are appended without overwriting existing ones
            from google.cloud.firestore_v1 import ArrayUnion  # type: ignore[import]

            ref.set({field_name: ArrayUnion(parsed_entries), "_key": key}, merge=True)
        else:
            encoded = base64.b64encode(data).decode("ascii")
            ref.set({field_name: encoded, "_key": key}, merge=True)

    def read_bytes(self, key: str) -> bytes:
        """Read raw bytes previously stored by write_bytes."""
        import base64

        collection, doc_id, field = _parse_key(key)
        ref = self._db.collection(collection).document(doc_id)
        snap: DocumentSnapshot = ref.get()

        if not snap.exists:
            raise StorageError(f"Key not found in Firestore ({self._database}): {key}")

        doc_data: dict[str, Any] = snap.to_dict() or {}
        field_name = field or "_bytes"
        raw = doc_data.get(field_name)
        if raw is None:
            raise StorageError(f"Field '{field_name}' not found in Firestore: {key}")

        if key.endswith(".jsonl") and isinstance(raw, list):
            # Re-encode list of dicts back to JSONL bytes
            lines = "\n".join(json.dumps(entry) for entry in raw)
            return (lines + "\n").encode("utf-8")

        if isinstance(raw, str):
            return base64.b64decode(raw)

        return json.dumps(raw).encode("utf-8")


# ─── Serialisation helper ─────────────────────────────────────────────────────


def _sanitise(obj: Any) -> Any:
    """Recursively convert non-Firestore-native types to serialisable forms.

    Firestore supports: str, int, float, bool, None, list, dict, datetime.
    Everything else is coerced to string.
    """
    if isinstance(obj, dict):
        return {str(k): _sanitise(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitise(v) for v in obj]
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    # Fallback: convert to string (e.g. datetime, UUID, Decimal)
    return str(obj)
