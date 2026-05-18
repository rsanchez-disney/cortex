"""Repo cloning domain service.

Handles git cloning, URL deduplication, PAT injection, and local config
generation.  Framework-free — all functions accept plain data and return
plain data so they are fully unit-testable without CLI or I/O dependencies.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import structlog
import yaml

logger = structlog.get_logger()

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class RepoCloneStatus:
    """Status of a single repo clone operation."""

    name: str
    status: str  # "cloned", "skipped-duplicate", "failed"
    path: Path | None = None
    error: str | None = None


@dataclass
class CloneResult:
    """Aggregate result of cloning all repos."""

    statuses: list[RepoCloneStatus] = field(default_factory=list)
    url_to_clone_name: dict[str, str] = field(default_factory=dict)

    @property
    def cloned(self) -> int:
        return sum(1 for s in self.statuses if s.status == "cloned")

    @property
    def skipped(self) -> int:
        return sum(1 for s in self.statuses if s.status == "skipped-duplicate")

    @property
    def failed(self) -> int:
        return sum(1 for s in self.statuses if s.status == "failed")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def inject_pat(url: str, pat: str) -> str:
    """Inject an authentication PAT into a git URL.

    For Azure DevOps URLs (``dev.azure.com``), any existing ``user@`` prefix
    is stripped first so we never produce a double-``@`` URL.

    For other HTTPS URLs a generic ``pat:<pat>@`` credential is injected.

    Args:
        url: The original HTTPS git URL.
        pat: The personal access token to inject.

    Returns:
        The URL with embedded credentials.
    """
    if "dev.azure.com" in url:
        clean_url = re.sub(r"https://[^@]+@", "https://", url)
        return clean_url.replace("https://", f"https://{pat}@")
    return url.replace("https://", f"https://pat:{pat}@")


def deduplicate_urls(repos: list[dict]) -> dict[str, str]:
    """Map each unique URL to the first repo name that uses it.

    Repos that use ``path`` instead of ``url`` are ignored.

    Returns:
        ``{url: first_repo_name}`` for every unique URL found.
    """
    url_to_name: dict[str, str] = {}
    for repo in repos:
        url = repo.get("url")
        if url and url not in url_to_name:
            url_to_name[url] = repo["name"]
    return url_to_name


def clone_repos(
    repos: list[dict],
    clone_dir: Path,
    azure_pat: str,
) -> CloneResult:
    """Clone all remote repos, deduplicating by URL.

    Each unique URL is cloned exactly once into ``<clone_dir>/<name>``.
    Existing directories are removed and re-cloned (fresh shallow clone).
    Repos that share a URL are logged as ``skipped-duplicate``.
    Repos with ``path`` instead of ``url`` are skipped entirely (no cloning).

    Args:
        repos: List of repo config dicts from ``repos-real.yaml``.
        clone_dir: Root directory for clones (e.g. ``.repos``).
        azure_pat: Azure DevOps Personal Access Token.

    Returns:
        A :class:`CloneResult` with per-repo status and a URL-to-name map.
    """
    result = CloneResult()
    url_to_name = deduplicate_urls(repos)
    result.url_to_clone_name = url_to_name

    # Track which URLs have already been cloned in this run
    cloned_urls: set[str] = set()

    clone_dir.mkdir(parents=True, exist_ok=True)

    for repo in repos:
        name = repo["name"]
        url = repo.get("url")

        if not url:
            # path-based entry — nothing to clone
            result.statuses.append(
                RepoCloneStatus(name=name, status="skipped-duplicate", path=None)
            )
            continue

        if url in cloned_urls:
            # Already cloned under the first repo name
            first_name = url_to_name[url]
            clone_path = clone_dir / first_name
            logger.info(
                "skipping duplicate URL",
                repo=name,
                shared_with=first_name,
            )
            result.statuses.append(
                RepoCloneStatus(name=name, status="skipped-duplicate", path=clone_path)
            )
            continue

        # Actually clone
        clone_path = clone_dir / name
        try:
            _clone_single_repo(
                name=name,
                url=url,
                target_dir=clone_path,
                pat=azure_pat,
                branch=repo.get("branch"),
            )
            cloned_urls.add(url)
            result.statuses.append(
                RepoCloneStatus(name=name, status="cloned", path=clone_path)
            )
        except RuntimeError as exc:
            logger.error("clone failed", repo=name, error=str(exc))
            result.statuses.append(
                RepoCloneStatus(name=name, status="failed", error=str(exc))
            )

    return result


def generate_local_config(
    repos: list[dict],
    clone_dir: Path,
    url_to_clone_name: dict[str, str],
) -> str:
    """Generate ``repos-local.yaml`` content from ``repos-real`` entries.

    Each ``url`` entry is transformed to a ``path`` entry pointing at the
    clone directory.  ``branch`` is dropped (not needed for local paths).
    All other service metadata fields are preserved.

    Repos that already have ``path`` are passed through unchanged.

    Args:
        repos: Original repo config dicts.
        clone_dir: Root directory where clones live.
        url_to_clone_name: Mapping of URL to the directory name used for the
            clone (from :func:`deduplicate_urls`).

    Returns:
        Complete YAML file content as a string, including a header comment.
    """
    local_entries: list[dict] = []

    for repo in repos:
        entry: dict = {}
        url = repo.get("url")

        for key, value in repo.items():
            if key == "url":
                # Replace url with path
                clone_name = url_to_clone_name.get(url, repo["name"])  # type: ignore[arg-type]
                entry["path"] = str(clone_dir / clone_name)
            elif key == "branch":
                # Drop branch — not needed for local paths
                continue
            else:
                entry[key] = value

        # If it was already a path-based entry, keep as-is
        if "path" not in entry and repo.get("path"):
            entry["path"] = repo["path"]

        local_entries.append(entry)

    yaml_body = yaml.dump(
        {"repos": local_entries},
        default_flow_style=False,
        sort_keys=False,
        allow_unicode=True,
    )

    header = (
        "# Auto-generated by `cortex clone-repos`.\n"
        "# Do not edit manually — re-run `cortex clone-repos` to refresh.\n"
    )
    return header + yaml_body


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _clone_single_repo(
    name: str,
    url: str,
    target_dir: Path,
    pat: str,
    branch: str | None = None,
) -> None:
    """Clone a single repo into *target_dir* (persistent, not temp).

    If *target_dir* already exists it is removed first.

    Raises:
        RuntimeError: on clone failure or timeout.
    """
    if target_dir.exists():
        shutil.rmtree(target_dir)

    auth_url = inject_pat(url, pat)

    clone_cmd = ["git", "clone", "--depth", "1"]
    if branch:
        clone_cmd.extend(["--branch", branch])
    clone_cmd.extend([auth_url, str(target_dir)])

    logger.info("cloning repo", repo=name, target=str(target_dir))

    try:
        result = subprocess.run(
            clone_cmd,
            capture_output=True,
            text=True,
            timeout=120,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
        if result.returncode != 0:
            raise RuntimeError(f"git clone failed for '{name}': {result.stderr.strip()}")
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"git clone timed out for '{name}'")

    logger.info("clone complete", repo=name)
