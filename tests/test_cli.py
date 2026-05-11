"""Unit tests for CLI helper functions: _extract_service_data and _clone_repo."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from atlas.cli import _clone_repo, _extract_service_data


# ---------------------------------------------------------------------------
# _extract_service_data
# ---------------------------------------------------------------------------


def test_extract_service_data_strips_path() -> None:
    """'path' is a config-only field and must not appear in service data."""
    repo = {
        "name": "my-app",
        "path": "/tmp/my-app",
        "type": "android",
        "owner": "team-mobile",
        "domain": "payments",
        "tier": "critical",
        "purpose": "Main app",
    }
    result = _extract_service_data(repo)
    assert "path" not in result
    assert result["name"] == "my-app"
    assert result["type"] == "android"


def test_extract_service_data_strips_url() -> None:
    """'url' is a config-only field and must not appear in service data."""
    repo = {
        "name": "my-app",
        "url": "https://dev.azure.com/org/project/_git/my-app",
        "type": "ios",
        "owner": "team-mobile",
        "domain": "payments",
        "tier": "critical",
        "purpose": "Main app",
    }
    result = _extract_service_data(repo)
    assert "url" not in result
    assert result["name"] == "my-app"


def test_extract_service_data_strips_branch() -> None:
    """'branch' is a config-only field and must not appear in service data."""
    repo = {
        "name": "my-app",
        "url": "https://dev.azure.com/org/project/_git/my-app",
        "branch": "develop",
        "type": "android",
        "owner": "team-mobile",
        "domain": "payments",
        "tier": "critical",
        "purpose": "Main app",
    }
    result = _extract_service_data(repo)
    assert "branch" not in result
    assert "url" not in result
    assert result["name"] == "my-app"
    assert result["type"] == "android"


def test_extract_service_data_keeps_name() -> None:
    """'name' must be kept — ServiceYaml requires it."""
    repo = {
        "name": "my-app",
        "path": "/tmp/my-app",
        "type": "android",
        "owner": "team-mobile",
        "domain": "payments",
        "tier": "critical",
        "purpose": "Main app",
    }
    result = _extract_service_data(repo)
    assert "name" in result
    assert result["name"] == "my-app"


def test_extract_service_data_keeps_optional_service_fields() -> None:
    """Optional service metadata fields (status, slack, keywords, etc.) are preserved."""
    repo = {
        "name": "my-app",
        "path": "/tmp/my-app",
        "type": "android",
        "owner": "team-mobile",
        "domain": "payments",
        "tier": "critical",
        "purpose": "Main app",
        "status": "active",
        "slack": "#team-mobile",
        "keywords": ["banking", "android"],
    }
    result = _extract_service_data(repo)
    assert result["status"] == "active"
    assert result["slack"] == "#team-mobile"
    assert result["keywords"] == ["banking", "android"]
    assert "path" not in result


# ---------------------------------------------------------------------------
# _clone_repo
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_env_with_pat(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set AZURE_PAT in the environment."""
    monkeypatch.setenv("AZURE_PAT", "test-pat-token")


def _make_successful_run() -> MagicMock:
    """Return a mock subprocess.CompletedProcess with returncode 0."""
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stderr = ""
    return mock_result


def test_clone_repo_without_branch(mock_env_with_pat: None, tmp_path: Path) -> None:
    """When branch is not provided, --branch must NOT appear in the git command."""
    with (
        patch("atlas.cli.tempfile.mkdtemp", return_value=str(tmp_path)),
        patch("atlas.cli.subprocess.run", return_value=_make_successful_run()) as mock_run,
    ):
        _clone_repo("my-app", "https://dev.azure.com/org/project/_git/my-app")

    cmd = mock_run.call_args[0][0]
    assert "--branch" not in cmd
    assert "git" in cmd
    assert "--depth" in cmd
    assert "1" in cmd


def test_clone_repo_with_branch(mock_env_with_pat: None, tmp_path: Path) -> None:
    """When branch is provided, --branch <value> must appear in the git command."""
    with (
        patch("atlas.cli.tempfile.mkdtemp", return_value=str(tmp_path)),
        patch("atlas.cli.subprocess.run", return_value=_make_successful_run()) as mock_run,
    ):
        _clone_repo("my-app", "https://dev.azure.com/org/project/_git/my-app", branch="develop")

    cmd = mock_run.call_args[0][0]
    assert "--branch" in cmd
    branch_idx = cmd.index("--branch")
    assert cmd[branch_idx + 1] == "develop"


def test_clone_repo_none_branch_omits_flag(mock_env_with_pat: None, tmp_path: Path) -> None:
    """Passing branch=None explicitly is the same as omitting it — no --branch flag."""
    with (
        patch("atlas.cli.tempfile.mkdtemp", return_value=str(tmp_path)),
        patch("atlas.cli.subprocess.run", return_value=_make_successful_run()) as mock_run,
    ):
        _clone_repo("my-app", "https://dev.azure.com/org/project/_git/my-app", branch=None)

    cmd = mock_run.call_args[0][0]
    assert "--branch" not in cmd


def test_clone_repo_no_pat_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing AZURE_PAT must raise RuntimeError with a helpful message."""
    monkeypatch.delenv("AZURE_PAT", raising=False)

    with pytest.raises(RuntimeError, match="AZURE_PAT"):
        _clone_repo("my-app", "https://dev.azure.com/org/project/_git/my-app")


def test_clone_repo_git_failure_raises(mock_env_with_pat: None, tmp_path: Path) -> None:
    """A non-zero git returncode must raise RuntimeError."""
    mock_result = MagicMock()
    mock_result.returncode = 128
    mock_result.stderr = "repository not found"

    with (
        patch("atlas.cli.tempfile.mkdtemp", return_value=str(tmp_path)),
        patch("atlas.cli.subprocess.run", return_value=mock_result),
    ):
        with pytest.raises(RuntimeError, match="git clone failed"):
            _clone_repo("my-app", "https://dev.azure.com/org/project/_git/my-app")


def test_clone_repo_timeout_raises(mock_env_with_pat: None, tmp_path: Path) -> None:
    """A subprocess timeout must raise RuntimeError."""
    with (
        patch("atlas.cli.tempfile.mkdtemp", return_value=str(tmp_path)),
        patch(
            "atlas.cli.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="git", timeout=120),
        ),
    ):
        with pytest.raises(RuntimeError, match="timed out"):
            _clone_repo("my-app", "https://dev.azure.com/org/project/_git/my-app")


def test_clone_repo_injects_pat_into_azure_url(mock_env_with_pat: None, tmp_path: Path) -> None:
    """The PAT must be injected into the Azure DevOps URL."""
    with (
        patch("atlas.cli.tempfile.mkdtemp", return_value=str(tmp_path)),
        patch("atlas.cli.subprocess.run", return_value=_make_successful_run()) as mock_run,
    ):
        _clone_repo("my-app", "https://dev.azure.com/org/project/_git/my-app")

    cmd = mock_run.call_args[0][0]
    # The auth URL should contain the PAT
    auth_url = next(arg for arg in cmd if "dev.azure.com" in arg)
    assert "test-pat-token@" in auth_url


def test_clone_repo_strips_existing_user_from_azure_url(
    mock_env_with_pat: None, tmp_path: Path
) -> None:
    """An existing user@ prefix in the Azure URL must be stripped before PAT injection."""
    with (
        patch("atlas.cli.tempfile.mkdtemp", return_value=str(tmp_path)),
        patch("atlas.cli.subprocess.run", return_value=_make_successful_run()) as mock_run,
    ):
        _clone_repo(
            "my-app",
            "https://OrgName@dev.azure.com/org/project/_git/my-app",
        )

    cmd = mock_run.call_args[0][0]
    auth_url = next(arg for arg in cmd if "dev.azure.com" in arg)
    # Should not have double @ (OrgName@ replaced by test-pat-token@)
    assert auth_url.count("@") == 1
    assert "test-pat-token@" in auth_url
    assert "OrgName@" not in auth_url
