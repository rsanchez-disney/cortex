"""Tests for the extractor registry."""

from __future__ import annotations

import pytest

from cortex.extractors import ExtractorError, get_extractor
from cortex.extractors.android import AndroidExtractor
from cortex.extractors.backend_java import BackendJavaExtractor
from cortex.extractors.ios import IOSExtractor


class TestGetExtractor:
    """Tests for get_extractor function."""

    def test_android_returns_correct_class(self) -> None:
        """Known type 'android' returns AndroidExtractor."""
        extractor = get_extractor("android")
        assert isinstance(extractor, AndroidExtractor)

    def test_ios_returns_correct_class(self) -> None:
        """Known type 'ios' returns IOSExtractor."""
        extractor = get_extractor("ios")
        assert isinstance(extractor, IOSExtractor)

    def test_backend_java_returns_correct_class(self) -> None:
        """Known type 'backend-java' returns BackendJavaExtractor."""
        extractor = get_extractor("backend-java")
        assert isinstance(extractor, BackendJavaExtractor)

    def test_unknown_type_raises_error(self) -> None:
        """Unknown type raises ExtractorError with supported types listed."""
        with pytest.raises(ExtractorError, match="No extractor registered for type 'backend-go'"):
            get_extractor("backend-go")

    def test_unknown_type_lists_supported(self) -> None:
        """Error message includes list of all supported types."""
        with pytest.raises(ExtractorError, match="Supported types: android, backend-java, ios"):
            get_extractor("totally-unknown")
