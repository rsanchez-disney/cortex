"""Unit tests for the repo_cloner domain service."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import yaml

from atlas.repo_cloner import (
    clone_repos,
    deduplicate_urls,
    generate_local_config,
    inject_pat,
)

# ---------------------------------------------------------------------------
# inject_pat
# ---------------------------------------------------------------------------


class TestInjectPat:
    """Tests for PAT injection into git URLs."""

    def test_azure_devops_plain(self) -> None:
        url = "https://dev.azure.com/Org/Project/_git/repo"
        result = inject_pat(url, "my-token")
        assert result == "https://my-token@dev.azure.com/Org/Project/_git/repo"

    def test_azure_devops_with_existing_user(self) -> None:
        """Strips existing user@ prefix before injecting PAT."""
        url = "https://IntuitDome@dev.azure.com/IntuitDome/FanApp/_git/mobile-apps-android"
        result = inject_pat(url, "my-token")
        assert result == "https://my-token@dev.azure.com/IntuitDome/FanApp/_git/mobile-apps-android"
        # Must not have double @
        assert "@@" not in result

    def test_generic_https(self) -> None:
        """Non-Azure URLs use pat:<token>@ format."""
        url = "https://github.com/org/repo.git"
        result = inject_pat(url, "my-token")
        assert result == "https://pat:my-token@github.com/org/repo.git"

    def test_azure_devops_no_user_prefix(self) -> None:
        """Azure URL without any user@ prefix."""
        url = "https://dev.azure.com/IntuitDome/identity-microservice/_git/identity-microservice"
        result = inject_pat(url, "abc123")
        assert result == "https://abc123@dev.azure.com/IntuitDome/identity-microservice/_git/identity-microservice"


# ---------------------------------------------------------------------------
# deduplicate_urls
# ---------------------------------------------------------------------------


class TestDeduplicateUrls:
    """Tests for URL deduplication logic."""

    def test_unique_urls(self) -> None:
        repos = [
            {"name": "repo-a", "url": "https://example.com/a.git"},
            {"name": "repo-b", "url": "https://example.com/b.git"},
        ]
        result = deduplicate_urls(repos)
        assert result == {
            "https://example.com/a.git": "repo-a",
            "https://example.com/b.git": "repo-b",
        }

    def test_duplicate_url_keeps_first_name(self) -> None:
        """When two repos share the same URL, the first name wins."""
        repos = [
            {"name": "fan-app-ios", "url": "https://dev.azure.com/Org/P/_git/mobile-apps-ios"},
            {"name": "staff-app-ios", "url": "https://dev.azure.com/Org/P/_git/mobile-apps-ios"},
        ]
        result = deduplicate_urls(repos)
        assert result == {
            "https://dev.azure.com/Org/P/_git/mobile-apps-ios": "fan-app-ios",
        }

    def test_path_entries_ignored(self) -> None:
        """Repos with 'path' instead of 'url' are not included."""
        repos = [
            {"name": "local-repo", "path": "/some/path"},
            {"name": "remote-repo", "url": "https://example.com/repo.git"},
        ]
        result = deduplicate_urls(repos)
        assert result == {"https://example.com/repo.git": "remote-repo"}

    def test_empty_list(self) -> None:
        assert deduplicate_urls([]) == {}


# ---------------------------------------------------------------------------
# generate_local_config
# ---------------------------------------------------------------------------


class TestGenerateLocalConfig:
    """Tests for local config YAML generation."""

    def test_basic_url_to_path_transform(self) -> None:
        repos = [
            {
                "name": "my-app",
                "url": "https://example.com/repo.git",
                "type": "android",
                "owner": "team-mobile",
                "domain": "payments",
                "tier": "critical",
                "purpose": "Main app",
            }
        ]
        url_to_name = {"https://example.com/repo.git": "my-app"}
        clone_dir = Path(".repos")

        result = generate_local_config(repos, clone_dir, url_to_name)
        parsed = yaml.safe_load(result)

        assert len(parsed["repos"]) == 1
        entry = parsed["repos"][0]
        assert entry["path"] == ".repos/my-app"
        assert "url" not in entry
        assert entry["name"] == "my-app"
        assert entry["type"] == "android"

    def test_preserves_metadata(self) -> None:
        """Keywords, extractor_hints, and other fields survive the transform."""
        repos = [
            {
                "name": "fan-app-ios",
                "url": "https://example.com/ios.git",
                "type": "ios",
                "owner": "team-mobile",
                "domain": "entertainment",
                "tier": "critical",
                "purpose": "Fan app",
                "keywords": ["ios", "fanapp"],
                "extractor_hints": {"project_root": "App", "target": "LaClippers"},
            }
        ]
        url_to_name = {"https://example.com/ios.git": "fan-app-ios"}
        clone_dir = Path(".repos")

        result = generate_local_config(repos, clone_dir, url_to_name)
        parsed = yaml.safe_load(result)

        entry = parsed["repos"][0]
        assert entry["keywords"] == ["ios", "fanapp"]
        assert entry["extractor_hints"] == {"project_root": "App", "target": "LaClippers"}

    def test_drops_branch(self) -> None:
        """Branch field is dropped since local paths don't need it."""
        repos = [
            {
                "name": "staging",
                "url": "https://example.com/repo.git",
                "branch": "develop",
                "type": "android",
                "owner": "team",
                "domain": "d",
                "tier": "standard",
                "purpose": "Staging",
            }
        ]
        url_to_name = {"https://example.com/repo.git": "staging"}
        clone_dir = Path(".repos")

        result = generate_local_config(repos, clone_dir, url_to_name)
        parsed = yaml.safe_load(result)

        entry = parsed["repos"][0]
        assert "branch" not in entry
        assert entry["path"] == ".repos/staging"

    def test_dedup_entries_share_path(self) -> None:
        """Two repos sharing a URL both point to the same clone directory."""
        shared_url = "https://dev.azure.com/Org/P/_git/mobile-apps-ios"
        repos = [
            {
                "name": "fan-app-ios",
                "url": shared_url,
                "type": "ios",
                "owner": "team",
                "domain": "d",
                "tier": "critical",
                "purpose": "Fan app",
                "extractor_hints": {"target": "LaClippers"},
            },
            {
                "name": "staff-app-ios",
                "url": shared_url,
                "type": "ios",
                "owner": "team",
                "domain": "d",
                "tier": "critical",
                "purpose": "Staff app",
                "extractor_hints": {"target": "LACStaff"},
            },
        ]
        url_to_name = {shared_url: "fan-app-ios"}
        clone_dir = Path(".repos")

        result = generate_local_config(repos, clone_dir, url_to_name)
        parsed = yaml.safe_load(result)

        entries = parsed["repos"]
        assert len(entries) == 2
        # Both point to the same clone directory (the first repo's name)
        assert entries[0]["path"] == ".repos/fan-app-ios"
        assert entries[1]["path"] == ".repos/fan-app-ios"
        # But they keep their own metadata
        assert entries[0]["name"] == "fan-app-ios"
        assert entries[1]["name"] == "staff-app-ios"
        assert entries[0]["extractor_hints"]["target"] == "LaClippers"
        assert entries[1]["extractor_hints"]["target"] == "LACStaff"

    def test_header_comment(self) -> None:
        """Output starts with the auto-generated comment."""
        repos = [
            {
                "name": "x",
                "url": "https://example.com/x.git",
                "type": "android",
                "owner": "o",
                "domain": "d",
                "tier": "standard",
                "purpose": "p",
            }
        ]
        url_to_name = {"https://example.com/x.git": "x"}
        result = generate_local_config(repos, Path(".repos"), url_to_name)

        assert result.startswith("# Auto-generated by `atlas clone-repos`.")
        assert "Do not edit manually" in result

    def test_path_entries_pass_through(self) -> None:
        """Repos that already have 'path' are kept unchanged."""
        repos = [
            {
                "name": "local-only",
                "path": "/some/local/path",
                "type": "android",
                "owner": "o",
                "domain": "d",
                "tier": "standard",
                "purpose": "p",
            }
        ]
        result = generate_local_config(repos, Path(".repos"), {})
        parsed = yaml.safe_load(result)

        entry = parsed["repos"][0]
        assert entry["path"] == "/some/local/path"
        assert "url" not in entry


# ---------------------------------------------------------------------------
# clone_repos (with mocked subprocess)
# ---------------------------------------------------------------------------


class TestCloneRepos:
    """Tests for the clone_repos orchestration function."""

    def test_clones_unique_urls(self, tmp_path: Path) -> None:
        repos = [
            {"name": "repo-a", "url": "https://dev.azure.com/Org/P/_git/a", "type": "android",
             "owner": "o", "domain": "d", "tier": "standard", "purpose": "p"},
            {"name": "repo-b", "url": "https://dev.azure.com/Org/P/_git/b", "type": "android",
             "owner": "o", "domain": "d", "tier": "standard", "purpose": "p"},
        ]
        clone_dir = tmp_path / "clones"

        with patch("atlas.repo_cloner._clone_single_repo") as mock_clone:
            result = clone_repos(repos, clone_dir, "fake-pat")

        assert mock_clone.call_count == 2
        assert result.cloned == 2
        assert result.failed == 0

    def test_deduplicates_shared_url(self, tmp_path: Path) -> None:
        shared_url = "https://dev.azure.com/Org/P/_git/ios"
        repos = [
            {"name": "fan-app", "url": shared_url, "type": "ios",
             "owner": "o", "domain": "d", "tier": "critical", "purpose": "p"},
            {"name": "staff-app", "url": shared_url, "type": "ios",
             "owner": "o", "domain": "d", "tier": "critical", "purpose": "p"},
        ]
        clone_dir = tmp_path / "clones"

        with patch("atlas.repo_cloner._clone_single_repo") as mock_clone:
            result = clone_repos(repos, clone_dir, "fake-pat")

        # Only one actual clone
        assert mock_clone.call_count == 1
        assert result.cloned == 1
        assert result.skipped == 1

        # The deduped entry still gets a path
        staff_status = [s for s in result.statuses if s.name == "staff-app"][0]
        assert staff_status.status == "skipped-duplicate"
        assert staff_status.path == clone_dir / "fan-app"

    def test_handles_clone_failure(self, tmp_path: Path) -> None:
        repos = [
            {"name": "bad-repo", "url": "https://dev.azure.com/Org/P/_git/bad", "type": "android",
             "owner": "o", "domain": "d", "tier": "standard", "purpose": "p"},
            {"name": "good-repo", "url": "https://dev.azure.com/Org/P/_git/good", "type": "android",
             "owner": "o", "domain": "d", "tier": "standard", "purpose": "p"},
        ]
        clone_dir = tmp_path / "clones"

        def side_effect(name: str, **kwargs) -> None:  # noqa: ARG001
            if name == "bad-repo":
                raise RuntimeError("auth failed")

        with patch("atlas.repo_cloner._clone_single_repo", side_effect=side_effect):
            result = clone_repos(repos, clone_dir, "fake-pat")

        assert result.cloned == 1
        assert result.failed == 1

        bad = [s for s in result.statuses if s.name == "bad-repo"][0]
        assert bad.status == "failed"
        assert "auth failed" in (bad.error or "")

    def test_path_entries_skipped(self, tmp_path: Path) -> None:
        """Repos with 'path' instead of 'url' are not cloned."""
        repos = [
            {"name": "local", "path": "/some/path", "type": "android",
             "owner": "o", "domain": "d", "tier": "standard", "purpose": "p"},
        ]
        clone_dir = tmp_path / "clones"

        with patch("atlas.repo_cloner._clone_single_repo") as mock_clone:
            result = clone_repos(repos, clone_dir, "fake-pat")

        mock_clone.assert_not_called()
        assert result.cloned == 0

    def test_url_to_clone_name_populated(self, tmp_path: Path) -> None:
        repos = [
            {"name": "my-repo", "url": "https://example.com/repo.git", "type": "android",
             "owner": "o", "domain": "d", "tier": "standard", "purpose": "p"},
        ]
        clone_dir = tmp_path / "clones"

        with patch("atlas.repo_cloner._clone_single_repo"):
            result = clone_repos(repos, clone_dir, "fake-pat")

        assert result.url_to_clone_name == {"https://example.com/repo.git": "my-repo"}
