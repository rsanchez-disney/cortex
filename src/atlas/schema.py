"""Pydantic v2 models for Platform Atlas schemas.

Covers:
- ServiceYaml: input schema for service.yaml
- ServiceManifest: normalized extractor output
- PlatformGraph: aggregated graph of all services
- Supporting models: IntegrationNote, Dependency, EntryPoint, etc.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator


# --- service.yaml input models ---


class IntegrationNote(BaseModel):
    """A single integration note, scoped globally or to a specific endpoint."""

    scope: str = Field(description="'global' or an endpoint like 'POST /v1/transfers'")
    note: str = Field(max_length=200, description="Integration note text")


class ExtractorHints(BaseModel):
    """Optional hints for extractors dealing with non-standard repo layouts."""

    project_root: str | None = Field(
        default=None,
        description="Subdirectory containing the actual project (for nested layouts)",
    )
    additional_docs: list[str] | None = Field(
        default=None, description="Extra markdown files worth indexing"
    )


class ServiceYaml(BaseModel):
    """Pydantic model for service.yaml — the human-authored input to the extractor."""

    name: str = Field(
        pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$", description="Unique identifier, kebab-case"
    )
    type: Literal["android", "ios", "backend-go", "backend-node", "web-react"]
    owner: str
    domain: str
    tier: Literal["critical", "standard", "experimental", "deprecated"]
    purpose: str = Field(max_length=500)

    # Optional fields
    status: Literal["active", "deprecated", "archived"] = "active"
    slack: str | None = None
    runbook: str | None = None
    jira_component: str | None = None
    keywords: list[str] = Field(default_factory=list, max_length=10)
    integration_notes: list[IntegrationNote] = Field(default_factory=list, max_length=20)
    extractor_hints: ExtractorHints | None = None

    @field_validator("integration_notes")
    @classmethod
    def validate_integration_notes(cls, v: list[IntegrationNote]) -> list[IntegrationNote]:
        """Enforce size limits: max 10 global notes, max 3 per specific endpoint."""
        global_count = 0
        endpoint_counts: dict[str, int] = {}
        for note in v:
            if note.scope == "global":
                global_count += 1
                if global_count > 10:
                    raise ValueError(f"Too many global integration notes: {global_count} (max 10)")
            else:
                endpoint_counts[note.scope] = endpoint_counts.get(note.scope, 0) + 1
                if endpoint_counts[note.scope] > 3:
                    raise ValueError(
                        f"Too many integration notes for endpoint '{note.scope}': "
                        f"{endpoint_counts[note.scope]} (max 3 per endpoint)"
                    )
        return v


# --- Extractor output models ---


class Dependency(BaseModel):
    """A single dependency declaration."""

    name: str
    version: str | None = None
    source: str | None = None
    direct: bool = True
    category: str | None = None  # "runtime", "test", "build", "debug"


class EntryPoint(BaseModel):
    """An entry point into the service/app."""

    kind: str
    ref: str


class EndpointIndex(BaseModel):
    """A single endpoint entry in the API contract index."""

    method: str | None = None
    path: str | None = None
    summary: str | None = None
    tags: list[str] = Field(default_factory=list)
    operation_id: str | None = None


class ApiContract(BaseModel):
    """A reference to an API contract file with an endpoint index."""

    kind: str
    version: str | None = None
    path: str | None = None
    endpoints: list[EndpointIndex] = Field(default_factory=list)


class RuntimeInfo(BaseModel):
    """Runtime environment information."""

    docker: bool = False
    k8s_manifests: str | None = None


class SourceRepo(BaseModel):
    """Source repository metadata."""

    url: str | None = None
    commit: str | None = None


class ModuleInfo(BaseModel):
    """Metadata for a single Gradle module."""

    name: str
    type: str  # "application", "library", "kmp", "unknown"
    dependencies: list[str] = Field(default_factory=list)


class ServiceManifest(BaseModel):
    """The full normalized extractor output — written to services/{name}/manifest.json."""

    name: str = Field(pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$")
    type: Literal["android", "ios", "backend-go", "backend-node", "web-react"]
    owner: str
    domain: str
    tier: Literal["critical", "standard", "experimental", "deprecated"]
    status: Literal["active", "deprecated", "archived"] = "active"
    purpose: str = Field(max_length=500)
    keywords: list[str] = Field(default_factory=list, max_length=10)

    language: str | None = None
    language_version: str | None = None
    slack: str | None = None
    runbook: str | None = None
    jira_component: str | None = None

    # Android-specific fields (None for non-Android types)
    application_id: str | None = None
    min_sdk: str | None = None
    target_sdk: str | None = None
    compile_sdk: str | None = None
    android_gradle_plugin: str | None = None

    # Android module graph
    modules: list[ModuleInfo] = Field(default_factory=list)
    permissions: list[str] = Field(default_factory=list)
    gradle_plugins: list[str] = Field(default_factory=list)
    build_variants: list[str] = Field(default_factory=list)

    dependencies: list[Dependency] = Field(default_factory=list)
    entry_points: list[EntryPoint] = Field(default_factory=list)
    api_contracts: list[ApiContract] = Field(default_factory=list)
    runtime: RuntimeInfo | None = None
    ci: str | None = None
    integration_notes: list[IntegrationNote] = Field(default_factory=list, max_length=20)

    extracted_at: datetime
    extractor_version: str
    source_repo: SourceRepo | None = None


# --- Error tracking ---


class ExtractionError(BaseModel):
    """Error record for a failed extraction — written to services/{name}/extraction-error.json."""

    repo: str
    timestamp: datetime
    error: str
    phase: str


# --- Aggregation models ---


class GraphEntry(BaseModel):
    """Lightweight service summary for the aggregate graph."""

    name: str
    type: str
    owner: str
    domain: str
    tier: str
    status: str = "active"
    purpose: str
    keywords: list[str] = Field(default_factory=list)
    language: str | None = None
    dependencies: list[str] = Field(default_factory=list)
    endpoints: list[EndpointIndex] = Field(default_factory=list)

    # Enriched fields for graph-level querying
    module_count: int = 0
    permissions: list[str] = Field(default_factory=list)
    gradle_plugins: list[str] = Field(default_factory=list)
    ci: str | None = None


class GraphMetadata(BaseModel):
    """Metadata about the aggregated graph."""

    timestamp: datetime
    version: str
    service_count: int


class PlatformGraph(BaseModel):
    """The aggregated graph — written to graph/latest.json."""

    services: list[GraphEntry] = Field(default_factory=list)
    failed_extractions: list[ExtractionError] = Field(default_factory=list)
    metadata: GraphMetadata
