"""Abstract base class for all ecosystem-specific extractors."""

from __future__ import annotations

import re
import subprocess
from abc import ABC, abstractmethod
from pathlib import Path

from atlas.schema import ApiContract, ServiceManifest, ServiceYaml, SourceRepo


class Extractor(ABC):
    """Base class for all ecosystem-specific extractors.

    Each extractor knows its ecosystem's conventions.
    No ecosystem knowledge leaks between extractors.
    """

    type: str  # e.g. "android", "ios"

    @abstractmethod
    def extract(self, repo_path: Path, service_yaml: ServiceYaml) -> ServiceManifest:
        """Parse repo-specific files and return a structured manifest.

        Args:
            repo_path: Path to the root of the cloned/local repo
            service_yaml: Validated ServiceYaml model

        Returns:
            A fully populated ServiceManifest
        """
        ...

    @abstractmethod
    def find_api_contracts(self, repo_path: Path) -> list[ApiContract]:
        """Find API contract files; return references, not contents.

        For mobile apps (Android/iOS), this typically returns an empty list
        unless extractor_hints.additional_docs lists them.
        """
        ...

    def _get_source_repo(self, repo_path: Path) -> SourceRepo | None:
        """Detect git remote URL and HEAD commit for the given repo path.

        Runs ``git remote get-url origin`` and ``git rev-parse HEAD`` via
        subprocess with a short timeout. Fails silently — returns ``None``
        if the directory is not a git repo or any command fails.

        Args:
            repo_path: Absolute path to the repository root.

        Returns:
            A ``SourceRepo`` instance if git info is available, else ``None``.
        """
        try:
            url_result = subprocess.run(
                ["git", "remote", "get-url", "origin"],
                cwd=str(repo_path),
                capture_output=True,
                text=True,
                timeout=10,
            )
            commit_result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=str(repo_path),
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return None

        url = url_result.stdout.strip() if url_result.returncode == 0 else None
        commit = commit_result.stdout.strip() if commit_result.returncode == 0 else None

        if url is None and commit is None:
            return None

        # Strip embedded credentials (e.g. https://{pat}@dev.azure.com/...)
        if url:
            url = re.sub(r"(https?://)([^@]+@)", r"\1", url)

        return SourceRepo(url=url, commit=commit)
