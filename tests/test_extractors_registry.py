"""Tests for the extractor registry."""

from __future__ import annotations

import pytest

from cortex.extractors import ExtractorError, get_extractor
from cortex.extractors.android import AndroidExtractor
from cortex.extractors.backend_java import BackendJavaExtractor
from cortex.extractors.backend_go import BackendGoExtractor
from cortex.extractors.backend_python import BackendPythonExtractor
from cortex.extractors.backend_typescript import BackendTypeScriptExtractor
from cortex.extractors.frontend_angular import FrontendAngularExtractor
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

    def test_backend_typescript_returns_correct_class(self) -> None:
        """Known type 'backend-typescript' returns BackendTypeScriptExtractor."""
        extractor = get_extractor("backend-typescript")
        assert isinstance(extractor, BackendTypeScriptExtractor)

    def test_backend_go_returns_correct_class(self) -> None:
        """Known type 'backend-go' returns BackendGoExtractor."""
        extractor = get_extractor("backend-go")
        assert isinstance(extractor, BackendGoExtractor)

    def test_backend_python_returns_correct_class(self) -> None:
        """Known type 'backend-python' returns BackendPythonExtractor."""
        extractor = get_extractor("backend-python")
        assert isinstance(extractor, BackendPythonExtractor)

    def test_frontend_angular_returns_correct_class(self) -> None:
        """Known type 'frontend-angular' returns FrontendAngularExtractor."""
        extractor = get_extractor("frontend-angular")
        assert isinstance(extractor, FrontendAngularExtractor)

    def test_unknown_type_raises_error(self) -> None:
        """Unknown type raises ExtractorError."""
        with pytest.raises(ExtractorError, match="No extractor registered for type 'totally-unknown'"):
            get_extractor("totally-unknown")

    def test_unknown_type_lists_supported(self) -> None:
        """Error message includes list of all supported types."""
        with pytest.raises(ExtractorError, match="Supported types: android, backend-go, backend-java, backend-python, backend-typescript, frontend-angular, ios"):
            get_extractor("totally-unknown")
