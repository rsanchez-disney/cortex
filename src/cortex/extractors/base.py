"""Abstract base class for all ecosystem-specific extractors."""

from __future__ import annotations

import re
import subprocess
from abc import ABC, abstractmethod
from pathlib import Path

import structlog

from cortex.schema import ApiContract, ServiceManifest, ServiceYaml, SourceRepo

log = structlog.get_logger()


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

    # --- AI context extraction methods ---

    # Directories to search for context-pack files, in priority order.
    _CONTEXT_PACK_DIRS = (
        ".ai/context-pack",
        "ai/context-pack",
        "ia-context-pack",
        ".ia/context-pack",  # typo variant found in some repos
    )

    def _extract_agent_context(self, repo_path: Path) -> str | None:
        """Read AGENTS.md or CLAUDE.md from the repo root.

        Checks for ``AGENTS.md`` first, falls back to ``CLAUDE.md``.
        Returns the file content as a string, or ``None`` if neither exists.
        """
        for filename in ("AGENTS.md", "CLAUDE.md"):
            candidate = repo_path / filename
            if candidate.is_file():
                try:
                    return candidate.read_text(encoding="utf-8")
                except OSError:
                    log.warning(
                        "failed_to_read_agent_context",
                        path=str(candidate),
                    )
                    return None
        return None

    def _find_context_pack_dir(self, repo_path: Path) -> Path | None:
        """Locate the first existing context-pack directory in the repo."""
        for rel in self._CONTEXT_PACK_DIRS:
            candidate = repo_path / rel
            if candidate.is_dir():
                return candidate
        return None

    def _extract_domain_context(self, repo_path: Path) -> str | None:
        """Read ``domain.md`` from the repo's AI context-pack directory.

        Searches :pyattr:`_CONTEXT_PACK_DIRS` in order and returns the
        content of the first ``domain.md`` found, or ``None``.
        """
        pack_dir = self._find_context_pack_dir(repo_path)
        if pack_dir is None:
            return None
        domain_file = pack_dir / "domain.md"
        if domain_file.is_file():
            try:
                return domain_file.read_text(encoding="utf-8")
            except OSError:
                log.warning(
                    "failed_to_read_domain_context",
                    path=str(domain_file),
                )
        return None

    def _extract_context_pack(self, repo_path: Path) -> dict[str, str] | None:
        """Index all ``.md`` files in the repo's AI context-pack directory.

        Returns a dict mapping each file's stem (filename without ``.md``)
        to its content.  Returns ``None`` if no context-pack directory
        exists or it contains no readable markdown files.
        """
        pack_dir = self._find_context_pack_dir(repo_path)
        if pack_dir is None:
            return None
        result: dict[str, str] = {}
        for md_file in sorted(pack_dir.glob("*.md")):
            if md_file.is_file():
                try:
                    result[md_file.stem] = md_file.read_text(encoding="utf-8")
                except OSError:
                    log.warning(
                        "failed_to_read_context_pack_file",
                        path=str(md_file),
                    )
        return result if result else None

    # Maximum length for the enriched_purpose field.
    _ENRICHED_PURPOSE_MAX_LEN = 500

    # Regex to find a "What This Project/Service Is", "Project DNA", or "Overview" heading.
    _SECTION_HEADING_RE = re.compile(
        r"^#{1,3}\s+(?:What This (?:Project|Service) Is|Project DNA|Overview)\s*$",
        re.MULTILINE | re.IGNORECASE,
    )

    # Regex to extract "- **Key**: Value" or "* **Key**: Value" metadata lines.
    _METADATA_LINE_RE = re.compile(r"[-*]\s+\*\*(.+?)\*\*:\s*(.+)")

    def _generate_enriched_purpose(self, agent_context: str | None) -> str | None:
        """Generate an enriched purpose string from AGENTS.md / CLAUDE.md content.

        Deterministically parses the markdown to extract key summary information:
        a recognised section ("What This Project Is", "Project DNA", "Overview"),
        metadata bullet points (Type, Language, Database, etc.), and falls back
        to the first non-heading paragraph when no recognised section is found.

        Args:
            agent_context: Raw content of AGENTS.md or CLAUDE.md, or ``None``.

        Returns:
            A concise summary string (≤500 chars), or ``None`` when no content
            is available.
        """
        if agent_context is None:
            return None

        section_text = self._extract_summary_section(agent_context)
        if section_text is None:
            section_text = self._extract_first_paragraph(agent_context)
        if section_text is None:
            return None

        metadata = self._extract_metadata(section_text)
        prose = self._extract_prose(section_text)

        parts: list[str] = []

        # Build a compact header from metadata
        # e.g. "Spring Boot microservice (Java 17, PostgreSQL)"
        header = self._build_metadata_header(metadata)
        if header:
            parts.append(header)

        if prose:
            parts.append(prose)

        # Append integration/external-service info if present in metadata
        integrations = self._build_integrations_line(metadata)
        if integrations:
            parts.append(integrations)

        result = " ".join(parts).strip()
        if not result:
            return None

        return self._truncate(result, self._ENRICHED_PURPOSE_MAX_LEN)

    # -- private helpers for _generate_enriched_purpose --

    def _extract_summary_section(self, content: str) -> str | None:
        """Extract text under a recognised summary heading."""
        match = self._SECTION_HEADING_RE.search(content)
        if not match:
            return None
        start = match.end()
        next_heading = re.search(r"^#{1,3}\s+", content[start:], re.MULTILINE)
        if next_heading:
            return content[start : start + next_heading.start()].strip()
        return content[start:].strip()

    def _extract_first_paragraph(self, content: str) -> str | None:
        """Fallback: return the first non-heading, non-empty paragraph."""
        for line in content.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            # Skip markdown headings
            if stripped.startswith("#"):
                continue
            # Collect the paragraph (may be a single line or a bullet block)
            return stripped
        return None

    def _extract_metadata(self, section_text: str) -> dict[str, str]:
        """Extract ``**Key**: Value`` pairs from bullet lines."""
        metadata: dict[str, str] = {}
        for m in self._METADATA_LINE_RE.finditer(section_text):
            metadata[m.group(1).strip().lower()] = m.group(2).strip()
        return metadata

    def _extract_prose(self, section_text: str) -> str:
        """Extract non-bullet, non-heading lines as prose."""
        lines: list[str] = []
        for line in section_text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("#"):
                continue
            if self._METADATA_LINE_RE.match(stripped):
                continue
            lines.append(stripped)
        return " ".join(lines)

    def _build_metadata_header(self, metadata: dict[str, str]) -> str:
        """Build a compact header like ``Spring Boot microservice (Java 17, PostgreSQL).``"""
        type_val = metadata.get("type", "")
        extras: list[str] = []
        for key in ("language", "java", "java version"):
            if key in metadata:
                extras.append(metadata[key])
                break
        for key in ("database", "db"):
            if key in metadata:
                extras.append(metadata[key])
                break
        for key in ("framework",):
            if key in metadata and key not in type_val.lower():
                extras.append(metadata[key])

        if not type_val and not extras:
            return ""

        header = type_val
        if extras:
            qualifier = ", ".join(extras)
            if header:
                header = f"{header} ({qualifier})"
            else:
                header = qualifier
        if header and not header.endswith("."):
            header += "."
        return header

    def _build_integrations_line(self, metadata: dict[str, str]) -> str:
        """Build an ``Integrates with: ...`` line from metadata."""
        for key in ("key integrations", "integrations", "external services"):
            if key in metadata:
                return f"Integrates with: {metadata[key]}"
        return ""

    @staticmethod
    def _truncate(text: str, max_len: int) -> str:
        """Truncate *text* to *max_len* characters, breaking at a word boundary."""
        if len(text) <= max_len:
            return text
        truncated = text[: max_len - 3]
        # Try to break at the last space
        last_space = truncated.rfind(" ")
        if last_space > max_len // 2:
            truncated = truncated[:last_space]
        return truncated.rstrip(".,;: ") + "..."

    def _enrich_with_context(self, manifest: ServiceManifest, repo_path: Path) -> None:
        """Enrich manifest with AI context files from the repo."""
        manifest.agent_context = self._extract_agent_context(repo_path)
        manifest.domain_context = self._extract_domain_context(repo_path)
        manifest.context_pack = self._extract_context_pack(repo_path)
        manifest.enriched_purpose = self._generate_enriched_purpose(manifest.agent_context)
