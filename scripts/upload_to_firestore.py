#!/usr/bin/env python3
"""Upload Cortex output data to Firestore.

Reads the essential data from a local cortex-output directory and upserts it
into the named Firestore database ("cortex") so the Cloud Run MCP server can
serve it.

What is uploaded:
  graph/latest.json              → collection "graph"    / doc "latest"
  services/{name}/manifest.json  → collection "services" / doc "{name}"

SAFETY: This script NEVER deletes or clears any Firestore documents.
        All writes use set(..., merge=True) — pure upsert semantics.

Usage (via shell wrapper):
  uv run python scripts/upload_to_firestore.py \\
    --output-dir ./cortex-output \\
    --database cortex \\
    --project your-gcp-project-id

Or directly:
  python scripts/upload_to_firestore.py --output-dir ./cortex-output
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from cortex.firestore_storage import _sanitise

# ─── Argument parsing ─────────────────────────────────────────────────────────


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Upload Cortex extracted data (graph + manifests) to Firestore.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        type=Path,
        help="Path to the local cortex-output directory (must contain graph/latest.json).",
    )
    parser.add_argument(
        "--database",
        default="cortex",
        help="Firestore named database to upload into (default: cortex).",
    )
    parser.add_argument(
        "--project",
        default=None,
        help="GCP project ID. Falls back to Application Default Credentials project.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be uploaded without writing to Firestore.",
    )
    return parser.parse_args()


# ─── Firestore helpers ────────────────────────────────────────────────────────


def _get_firestore_client(project: str | None, database: str) -> Any:
    """Return an authenticated Firestore client for the named database."""
    try:
        from google.cloud import firestore  # type: ignore[import]
    except ImportError:
        print(
            "ERROR: google-cloud-firestore is not installed.\n"
            "       Run: uv sync  (it is listed in pyproject.toml dependencies)",
            file=sys.stderr,
        )
        sys.exit(1)

    kwargs: dict[str, Any] = {"database": database}
    if project:
        kwargs["project"] = project

    return firestore.Client(**kwargs)


# ─── Upload functions ─────────────────────────────────────────────────────────


def upload_graph(
    db: Any,
    output_dir: Path,
    dry_run: bool,
) -> bool:
    """Upload graph/latest.json → Firestore collection 'graph' / doc 'latest'.

    Returns True on success, False on failure.
    """
    graph_path = output_dir / "graph" / "latest.json"

    if not graph_path.exists():
        print(f"  SKIP   graph/latest.json — not found at {graph_path}", file=sys.stderr)
        return False

    print(f"  GRAPH  {graph_path} → graph/latest")

    try:
        with open(graph_path) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"  ERROR  Failed to read graph/latest.json: {e}", file=sys.stderr)
        return False

    if dry_run:
        svc_count = len(data.get("services", []))
        print(f"         [dry-run] Would upload graph with {svc_count} services.")
        return True

    if db is None:
        raise RuntimeError("Firestore client is None outside of dry-run mode")

    ref = db.collection("graph").document("latest")
    # merge=True: never overwrites the entire document — upsert only
    ref.set({"_data": _sanitise(data), "_key": "graph/latest.json"}, merge=True)

    svc_count = len(data.get("services", []))
    print(f"         ✓ Uploaded graph ({svc_count} services)")
    return True


def upload_manifests(
    db: Any,
    output_dir: Path,
    dry_run: bool,
) -> tuple[int, int]:
    """Upload services/{name}/manifest.json files to Firestore.

    Returns (uploaded_count, failed_count).
    """
    services_dir = output_dir / "services"

    if not services_dir.exists():
        print(f"  SKIP   services/ — directory not found at {services_dir}", file=sys.stderr)
        return 0, 0

    manifest_paths = sorted(services_dir.glob("*/manifest.json"))

    if not manifest_paths:
        print("  SKIP   No manifest.json files found under services/", file=sys.stderr)
        return 0, 0

    uploaded = 0
    failed = 0

    for manifest_path in manifest_paths:
        svc_name = manifest_path.parent.name
        firestore_key = f"services/{svc_name}/manifest.json"
        print(f"  SVC    {manifest_path.relative_to(output_dir)} → {firestore_key}")

        try:
            with open(manifest_path) as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            print(f"  ERROR  Failed to read {manifest_path}: {e}", file=sys.stderr)
            failed += 1
            continue

        if dry_run:
            print(f"         [dry-run] Would upload manifest for '{svc_name}'")
            uploaded += 1
            continue

        if db is None:
            raise RuntimeError("Firestore client is None outside of dry-run mode")

        try:
            ref = db.collection("services").document(svc_name)
            # merge=True: upsert — preserves any other fields on the document
            ref.set(
                {"manifest": _sanitise(data), "_key": firestore_key},
                merge=True,
            )
            print(f"         ✓ Uploaded manifest for '{svc_name}'")
            uploaded += 1
        except Exception as e:  # noqa: BLE001
            print(f"  ERROR  Failed to upload '{svc_name}': {e}", file=sys.stderr)
            failed += 1

    return uploaded, failed


# ─── Main ─────────────────────────────────────────────────────────────────────


def main() -> None:
    args = _parse_args()
    output_dir: Path = args.output_dir.resolve()
    database: str = args.database
    project: str | None = args.project
    dry_run: bool = args.dry_run

    # ── Validate ───────────────────────────────────────────────────────────────
    if not output_dir.is_dir():
        print(f"ERROR: Output directory not found: {output_dir}", file=sys.stderr)
        print("       Run 'cortex run-local' first to generate the data.", file=sys.stderr)
        sys.exit(1)

    # ── Connect ────────────────────────────────────────────────────────────────
    if dry_run:
        print(f"[DRY RUN] Would connect to Firestore database '{database}'")
        db = None
    else:
        print(f"Connecting to Firestore (database: '{database}') ...")
        db = _get_firestore_client(project=project, database=database)
        print("Connected.\n")

    # ── Upload graph ───────────────────────────────────────────────────────────
    print("Uploading graph ...")
    graph_ok = upload_graph(db, output_dir, dry_run)

    # ── Upload manifests ───────────────────────────────────────────────────────
    print("\nUploading service manifests ...")
    uploaded, failed = upload_manifests(db, output_dir, dry_run)

    # ── Summary ────────────────────────────────────────────────────────────────
    print()
    print("─" * 56)
    prefix = "[DRY RUN] " if dry_run else ""
    print(f"  {prefix}Graph uploaded   : {'✓' if graph_ok else '✗'}")
    print(f"  {prefix}Manifests OK     : {uploaded}")
    if failed:
        print(f"  Manifests FAILED : {failed}  ← check errors above")
    print("─" * 56)

    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
