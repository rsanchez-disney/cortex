"""Shared fixtures for Platform Atlas tests."""

from __future__ import annotations

from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"
SAMPLE_ANDROID_REPO = FIXTURES_DIR / "sample-android-repo"
SAMPLE_IOS_REPO = FIXTURES_DIR / "sample-ios-repo"
SAMPLE_BACKEND_JAVA_REPO = FIXTURES_DIR / "sample-backend-java-repo"


@pytest.fixture
def android_repo_path() -> Path:
    """Path to the sample Android repo fixture."""
    return SAMPLE_ANDROID_REPO


@pytest.fixture
def ios_repo_path() -> Path:
    """Path to the sample iOS repo fixture."""
    return SAMPLE_IOS_REPO


@pytest.fixture
def backend_java_repo_path() -> Path:
    """Path to the sample backend-java repo fixture."""
    return SAMPLE_BACKEND_JAVA_REPO


@pytest.fixture
def tmp_storage(tmp_path: Path) -> Path:
    """Temporary directory for storage backend tests."""
    return tmp_path / "atlas-storage"
