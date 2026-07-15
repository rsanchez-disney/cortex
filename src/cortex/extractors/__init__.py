"""Extractor registry — maps service type to extractor class.

No auto-detection. The `type` field in the repos config is the sole source of truth.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cortex.extractors.base import Extractor


class ExtractorError(Exception):
    """Raised when no extractor is available for a given type."""


def get_extractor(service_type: str) -> Extractor:
    """Return an extractor instance for the given service type.

    Raises:
        ExtractorError: if no extractor is registered for the type
    """
    # Lazy imports to avoid circular dependencies
    from cortex.extractors.android import AndroidExtractor
    from cortex.extractors.backend_java import BackendJavaExtractor
    from cortex.extractors.backend_go import BackendGoExtractor
    from cortex.extractors.backend_python import BackendPythonExtractor
    from cortex.extractors.backend_typescript import BackendTypeScriptExtractor
    from cortex.extractors.frontend_angular import FrontendAngularExtractor
    from cortex.extractors.ios import IOSExtractor

    registry: dict[str, type[Extractor]] = {
        "android": AndroidExtractor,
        "ios": IOSExtractor,
        "backend-java": BackendJavaExtractor,
        "backend-typescript": BackendTypeScriptExtractor,
        "backend-go": BackendGoExtractor,
        "backend-python": BackendPythonExtractor,
        "frontend-angular": FrontendAngularExtractor,
    }

    extractor_class = registry.get(service_type)
    if extractor_class is None:
        supported = ", ".join(sorted(registry.keys()))
        raise ExtractorError(
            f"No extractor registered for type '{service_type}'. Supported types: {supported}"
        )

    return extractor_class()
