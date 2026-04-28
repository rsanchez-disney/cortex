"""Tests for service metadata validation."""

from __future__ import annotations

import pytest

from atlas.validation import ValidationError, validate_service_yaml


# Minimal valid service metadata dict
_VALID_BASE = {
    "name": "test-app",
    "type": "android",
    "owner": "team",
    "domain": "mobile",
    "tier": "standard",
    "purpose": "Testing",
}


class TestValidateServiceYaml:
    """Tests for validate_service_yaml function."""

    def test_valid_service_metadata(self) -> None:
        """Valid metadata dict passes validation."""
        result = validate_service_yaml(
            {
                "name": "sample-android",
                "type": "android",
                "owner": "team-mobile",
                "domain": "mobile",
                "tier": "standard",
                "purpose": "Sample Android app for testing the extractor.",
            }
        )
        assert result.name == "sample-android"
        assert result.type == "android"
        assert result.owner == "team-mobile"
        assert result.domain == "mobile"
        assert result.tier == "standard"

    def test_not_a_dict_raises_error(self) -> None:
        """Passing a non-dict raises ValidationError."""
        with pytest.raises(ValidationError, match="must be a dict"):
            validate_service_yaml("not a dict")  # type: ignore[arg-type]

    def test_missing_required_field_name(self) -> None:
        """Missing 'name' field fails validation."""
        data = {k: v for k, v in _VALID_BASE.items() if k != "name"}
        with pytest.raises(ValidationError, match="name"):
            validate_service_yaml(data)

    def test_missing_required_field_type(self) -> None:
        """Missing 'type' field fails validation with clear error."""
        data = {k: v for k, v in _VALID_BASE.items() if k != "type"}
        with pytest.raises(ValidationError, match="type"):
            validate_service_yaml(data)

    def test_invalid_type_enum(self) -> None:
        """Invalid type value fails validation."""
        data = {**_VALID_BASE, "type": "invalid-type"}
        with pytest.raises(ValidationError):
            validate_service_yaml(data)

    def test_purpose_too_long(self) -> None:
        """Purpose exceeding 500 chars fails validation."""
        data = {**_VALID_BASE, "purpose": "x" * 501}
        with pytest.raises(ValidationError):
            validate_service_yaml(data)

    def test_too_many_keywords(self) -> None:
        """More than 10 keywords fails validation."""
        data = {**_VALID_BASE, "keywords": [f"kw-{i}" for i in range(11)]}
        with pytest.raises(ValidationError):
            validate_service_yaml(data)

    def test_too_many_global_integration_notes(self) -> None:
        """More than 10 global integration notes fails validation."""
        data = {
            **_VALID_BASE,
            "integration_notes": [{"scope": "global", "note": f"Note {i}"} for i in range(11)],
        }
        with pytest.raises(ValidationError, match="global"):
            validate_service_yaml(data)

    def test_too_many_per_endpoint_notes(self) -> None:
        """More than 3 notes per endpoint fails validation."""
        data = {
            **_VALID_BASE,
            "integration_notes": [
                {"scope": "POST /v1/pay", "note": f"Note {i}"} for i in range(4)
            ],
        }
        with pytest.raises(ValidationError, match="endpoint"):
            validate_service_yaml(data)

    def test_note_text_too_long(self) -> None:
        """Note text exceeding 200 chars fails validation."""
        data = {
            **_VALID_BASE,
            "integration_notes": [{"scope": "global", "note": "x" * 201}],
        }
        with pytest.raises(ValidationError):
            validate_service_yaml(data)

    def test_valid_with_all_optional_fields(self) -> None:
        """Valid metadata with all optional fields passes."""
        data = {
            "name": "full-service",
            "type": "ios",
            "owner": "team-ios",
            "domain": "mobile",
            "tier": "critical",
            "purpose": "Full service with all fields.",
            "status": "active",
            "slack": "#team-ios",
            "runbook": "https://example.com/runbook",
            "jira_component": "IOS-APP",
            "keywords": ["ios", "banking"],
            "integration_notes": [
                {"scope": "global", "note": "Auth required"},
            ],
            "extractor_hints": {
                "project_root": "ios/MyApp/",
                "additional_docs": ["docs/arch.md"],
            },
        }
        result = validate_service_yaml(data)
        assert result.name == "full-service"
        assert result.type == "ios"
        assert result.status == "active"
        assert result.extractor_hints is not None
        assert result.extractor_hints.project_root == "ios/MyApp/"
