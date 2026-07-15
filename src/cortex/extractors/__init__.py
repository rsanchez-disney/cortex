"""Extractor registry — maps service type to extractor class.

Supports both built-in extractors and external plugins via entry points.

To create a custom extractor plugin:

1. Create a package with a class that subclasses cortex.extractors.base.Extractor
2. Register it in your pyproject.toml:
       [project.entry-points."cortex.extractors"]
       backend-dotnet = "my_package.extractor:DotNetExtractor"
3. Install the package in the same environment as cortex
4. Run `cortex list-extractors` to verify it's discovered
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from cortex.extractors.base import Extractor

logger = structlog.get_logger()


class ExtractorError(Exception):
    """Raised when no extractor is available for a given type."""


def _load_builtin_registry() -> dict[str, type[Extractor]]:
    """Load built-in extractors (lazy imports)."""
    from cortex.extractors.android import AndroidExtractor
    from cortex.extractors.backend_java import BackendJavaExtractor
    from cortex.extractors.backend_go import BackendGoExtractor
    from cortex.extractors.backend_python import BackendPythonExtractor
    from cortex.extractors.backend_typescript import BackendTypeScriptExtractor
    from cortex.extractors.frontend_angular import FrontendAngularExtractor
    from cortex.extractors.ios import IOSExtractor

    return {
        "android": AndroidExtractor,
        "ios": IOSExtractor,
        "backend-java": BackendJavaExtractor,
        "backend-typescript": BackendTypeScriptExtractor,
        "backend-go": BackendGoExtractor,
        "backend-python": BackendPythonExtractor,
        "frontend-angular": FrontendAngularExtractor,
    }


def _load_plugins() -> dict[str, type[Extractor]]:
    """Discover and load extractor plugins via entry points.

    External packages register plugins in their pyproject.toml:
        [project.entry-points."cortex.extractors"]
        backend-dotnet = "my_package.extractors:DotNetExtractor"
    """
    plugins: dict[str, type[Extractor]] = {}

    if sys.version_info >= (3, 10):
        from importlib.metadata import entry_points
    else:
        from importlib.metadata import entry_points  # type: ignore[assignment]

    discovered = entry_points(group="cortex.extractors")
    for ep in discovered:
        try:
            extractor_class = ep.load()
            plugins[ep.name] = extractor_class
            logger.info("loaded_extractor_plugin", name=ep.name, module=ep.value)
        except Exception as e:
            logger.warning("failed_to_load_plugin", name=ep.name, error=str(e))

    return plugins


def get_extractor(service_type: str) -> Extractor:
    """Return an extractor instance for the given service type.

    Checks built-in extractors first, then plugins.

    Raises:
        ExtractorError: if no extractor is registered for the type
    """
    registry = _load_builtin_registry()
    plugins = _load_plugins()

    # Plugins can override built-ins (intentional for customization)
    full_registry = {**registry, **plugins}

    extractor_class = full_registry.get(service_type)
    if extractor_class is None:
        supported = ", ".join(sorted(full_registry.keys()))
        raise ExtractorError(
            f"No extractor registered for type '{service_type}'. Supported types: {supported}"
        )

    return extractor_class()


def list_extractors() -> dict[str, str]:
    """List all available extractors (built-in + plugins) with their source.

    Returns:
        Dict mapping extractor type name to source ('built-in' or 'plugin').
    """
    registry = _load_builtin_registry()
    plugins = _load_plugins()

    result: dict[str, str] = {}
    for name in sorted(registry):
        result[name] = "built-in"
    for name in sorted(plugins):
        result[name] = "plugin"
    return result
