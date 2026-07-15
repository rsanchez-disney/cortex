"""Pydantic v2 models for Platform Cortex schemas.

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
    target: str | None = Field(
        default=None,
        description=(
            "Xcode target name to scope extraction to (for monorepos with multiple apps "
            "in a single .xcodeproj). When set, the iOS extractor restricts bundle ID, "
            "entitlements, feature domains, and build configs to files associated with "
            "this target's source directory."
        ),
    )


class ServiceYaml(BaseModel):
    """Pydantic model for service.yaml — the human-authored input to the extractor."""

    name: str = Field(
        pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$", description="Unique identifier, kebab-case"
    )
    type: Literal["android", "ios", "backend-go", "backend-node", "backend-java", "backend-typescript", "backend-python", "frontend-angular", "frontend-react", "web-react"]
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
    swagger_url: str | None = None

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


class DtoFieldConstraint(BaseModel):
    """Validation constraint on a DTO field (e.g., @Size, @Min, @Pattern)."""

    kind: str  # "size", "min", "max", "pattern", "email"
    value: str | None = None  # constraint value as string
    min: int | None = None  # for @Size(min=...)
    max: int | None = None  # for @Size(max=...)


class DtoField(BaseModel):
    """A single field in a DTO class definition."""

    name: str
    type: str  # Java type as string, e.g. "String", "List<OrderItemDto>"
    required: bool = False  # true if @NotNull, @NotBlank, @NotEmpty
    json_name: str | None = None  # @JsonProperty override, if different from name
    constraints: list[DtoFieldConstraint] = Field(default_factory=list)
    description: str | None = None  # from @Schema(description=...) or Javadoc


class DtoSchema(BaseModel):
    """Extracted schema of a DTO class (like an OpenAPI schema component)."""

    name: str  # Simple class name, e.g. "CreateOrderRequest"
    kind: str = "class"  # "class", "record", "enum", "interface"
    fields: list[DtoField] = Field(default_factory=list)
    enum_values: list[str] = Field(default_factory=list)  # for kind="enum"
    parent: str | None = None  # superclass name if extends
    source_file: str | None = None  # relative path to source file


class EndpointParameter(BaseModel):
    """A single request parameter extracted from Spring annotations."""

    name: str
    location: str  # "query", "path", "header"
    type: str | None = None  # Java type: "String", "Long", "int", etc.
    required: bool | None = None  # None = not explicitly set
    default_value: str | None = None  # from defaultValue attribute


class EndpointRequestBody(BaseModel):
    """Request body type extracted from @RequestBody annotation."""

    type: str  # DTO class name: "CreateOrderRequest", "OrderDto"
    required: bool = True  # @RequestBody(required = false)


class EndpointResponse(BaseModel):
    """Response type extracted from method return type."""

    type: str  # Unwrapped type: "OrderDto", "List<OrderDto>", "void"
    wrapper: str | None = None  # Outer wrapper: "ResponseEntity", "Mono", "Flux", etc.


class EndpointIndex(BaseModel):
    """A single endpoint entry in the API contract index."""

    method: str | None = None
    path: str | None = None
    summary: str | None = None
    tags: list[str] = Field(default_factory=list)
    operation_id: str | None = None
    parameters: list[EndpointParameter] = Field(default_factory=list)
    request_body: EndpointRequestBody | None = None
    response: EndpointResponse | None = None


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
    type: Literal["android", "ios", "backend-go", "backend-node", "backend-java", "backend-typescript", "backend-python", "frontend-angular", "frontend-react", "web-react"]
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

    # Backend-Java-specific fields (None for non-Java types)
    spring_boot_version: str | None = None
    java_version: str | None = None
    framework: str | None = None  # "spring-boot", "micronaut", "quarkus", etc.
    flyway_migration_count: int | None = None
    kafka_topics: list[str] = Field(default_factory=list)
    kafka_produces: list[str] = Field(default_factory=list)
    kafka_consumes: list[str] = Field(default_factory=list)
    outbound_calls: list[OutboundCall] = Field(default_factory=list)
    api_calls: list[ApiCall] = Field(default_factory=list)
    database_type: str | None = None  # "postgresql", "mysql", "cosmos", etc.
    secondary_databases: list[str] = Field(default_factory=list)  # additional detected DBs
    cache_type: str | None = None  # "redis", "memcached", "caffeine", etc.

    dependencies: list[Dependency] = Field(default_factory=list)
    entry_points: list[EntryPoint] = Field(default_factory=list)
    api_contracts: list[ApiContract] = Field(default_factory=list)
    dto_schemas: dict[str, DtoSchema] = Field(default_factory=dict)
    runtime: RuntimeInfo | None = None
    ci: str | None = None
    integration_notes: list[IntegrationNote] = Field(default_factory=list, max_length=20)

    swagger_url: str | None = None

    # AI context fields (extracted from repo root / context-pack directory)
    agent_context: str | None = None
    domain_context: str | None = None
    context_pack: dict[str, str] | None = None
    enriched_purpose: str | None = None

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
    endpoints: list[EndpointIndex] = Field(default_factory=list)

    # Enriched fields for graph-level querying
    module_count: int = 0
    permissions: list[str] = Field(default_factory=list)
    gradle_plugins: list[str] = Field(default_factory=list)
    ci: str | None = None
    framework: str | None = None
    kafka_produces: list[str] = Field(default_factory=list)
    kafka_consumes: list[str] = Field(default_factory=list)

    # Infrastructure fields promoted from ServiceManifest for graph-level querying
    database_type: str | None = None
    cache_type: str | None = None
    swagger_url: str | None = None


class OutboundCall(BaseModel):
    """An outbound HTTP call to another service."""

    target_url: str | None = None  # raw base URL from config
    target_service: str | None = None  # resolved service name (set during aggregation)
    config_key: str | None = None  # the config property key (e.g., "services.identity.base-url")
    protocol: str = "http"
    client_interfaces: list[str] = Field(
        default_factory=list,
        description=(
            "Names of @HttpExchange client interfaces that use this base URL "
            "(e.g., ['NBAWebClient', 'NBAStatsWebClient'])"
        ),
    )
    endpoints: list[EndpointIndex] = Field(
        default_factory=list,
        description=(
            "Endpoint paths declared on @HttpExchange client interfaces "
            "(e.g., @GetExchange('/v0/api/scores'))"
        ),
    )


class ApiCall(BaseModel):
    """An outbound API call made by a mobile app."""

    method: str | None = None  # "GET", "POST", etc.
    path: str | None = None  # "/v1/accounts/{id}"
    interface_name: str | None = None  # "IdentityApi" (Ktorfit interface name)
    base_url_key: str | None = None  # BuildConfig field or DI qualifier


class ServiceEdge(BaseModel):
    """A communication link between two services."""

    source: str  # producing/calling service name
    target: str  # consuming/called service name
    protocol: str  # "kafka", "http"
    detail: str | None = None  # topic name for kafka, endpoint path for http
    confidence: float = 1.0  # 0.0-1.0


class CommunicationGraph(BaseModel):
    """Cross-service communication edges."""

    edges: list[ServiceEdge] = Field(default_factory=list)


class GraphMetadata(BaseModel):
    """Metadata about the aggregated graph."""

    timestamp: datetime
    version: str
    service_count: int


class PlatformGraph(BaseModel):
    """The aggregated graph — written to graph/latest.json."""

    services: list[GraphEntry] = Field(default_factory=list)
    communication: CommunicationGraph = Field(default_factory=CommunicationGraph)
    failed_extractions: list[ExtractionError] = Field(default_factory=list)
    metadata: GraphMetadata
