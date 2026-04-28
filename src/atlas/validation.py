"""Validation for service metadata dicts.

Validates against both JSON Schema and Pydantic models,
enforcing all size limits from PRD §3.3.
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
from pydantic import ValidationError as PydanticValidationError

from atlas.schema import ServiceYaml

# Path to the JSON schema file
_SCHEMA_DIR = Path(__file__).parent.parent.parent / "schemas"
_SERVICE_SCHEMA_PATH = _SCHEMA_DIR / "service.schema.json"


class ValidationError(Exception):
    """Raised when service metadata validation fails."""

    def __init__(self, message: str, errors: list[str] | None = None):
        self.message = message
        self.errors = errors or []
        super().__init__(message)


def _load_json_schema() -> dict:
    """Load the service.yaml JSON schema."""
    with open(_SERVICE_SCHEMA_PATH) as f:
        return json.load(f)


def validate_service_yaml(data: dict) -> ServiceYaml:
    """Validate a service metadata dict and return a ServiceYaml model.

    Validates against:
    1. JSON Schema (structural validation)
    2. Pydantic model (type + business rule validation)

    Args:
        data: A dict containing service metadata fields (e.g. from repos config).

    Raises:
        ValidationError: with clear messages on any failure
    """
    if not isinstance(data, dict):
        raise ValidationError(f"Service metadata must be a dict, got {type(data).__name__}")

    # Validate against JSON Schema
    try:
        schema = _load_json_schema()
        jsonschema.validate(instance=data, schema=schema)
    except jsonschema.ValidationError as e:
        raise ValidationError(
            f"JSON Schema validation failed: {e.message}",
            errors=[e.message],
        ) from e

    # Validate with Pydantic (enforces additional business rules)
    try:
        return ServiceYaml(**data)
    except PydanticValidationError as e:
        error_messages = [err["msg"] for err in e.errors()]
        raise ValidationError(
            f"Pydantic validation failed with {len(error_messages)} error(s): {'; '.join(error_messages)}",
            errors=error_messages,
        ) from e
