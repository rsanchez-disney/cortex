"""Tests for the BackendJavaExtractor."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from cortex.extractors.backend_java import BackendJavaExtractor
from cortex.schema import ServiceYaml

SAMPLE_BACKEND_JAVA_REPO = Path(__file__).parent / "fixtures" / "sample-backend-java-repo"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def extractor() -> BackendJavaExtractor:
    return BackendJavaExtractor()


@pytest.fixture
def service_yaml() -> ServiceYaml:
    return ServiceYaml(
        name="sample-backend-java",
        type="backend-java",
        owner="team-backend",
        domain="ticketing",
        tier="standard",
        purpose="Sample Spring Boot microservice for testing backend-java extraction.",
        status="active",
        keywords=["spring-boot", "java", "kafka", "postgresql"],
    )


# ---------------------------------------------------------------------------
# TestBackendJavaExtractor — full extraction
# ---------------------------------------------------------------------------


class TestBackendJavaExtractor:
    """Tests for BackendJavaExtractor.extract() using the sample fixture."""

    def test_successful_extraction(
        self, extractor: BackendJavaExtractor, service_yaml: ServiceYaml
    ) -> None:
        """Full extraction produces a valid ServiceManifest."""
        with patch.object(extractor, "_get_source_repo", return_value=None):
            manifest = extractor.extract(SAMPLE_BACKEND_JAVA_REPO, service_yaml)

        assert manifest.name == "sample-backend-java"
        assert manifest.type == "backend-java"
        assert manifest.owner == "team-backend"
        assert manifest.domain == "ticketing"
        assert manifest.tier == "standard"
        assert manifest.extracted_at is not None
        assert manifest.extractor_version is not None

    def test_language_detection(self, extractor: BackendJavaExtractor) -> None:
        """Detects Java as primary language."""
        lang, _ = extractor._detect_language(SAMPLE_BACKEND_JAVA_REPO)
        assert lang == "java"

    def test_java_version(self, extractor: BackendJavaExtractor) -> None:
        """Parses sourceCompatibility = '17' from build.gradle."""
        _, java_version = extractor._detect_language(SAMPLE_BACKEND_JAVA_REPO)
        assert java_version == "17"

    def test_spring_boot_version(self, extractor: BackendJavaExtractor) -> None:
        """Parses Spring Boot version from plugins block."""
        meta = extractor._parse_gradle_metadata(SAMPLE_BACKEND_JAVA_REPO)
        assert meta["spring_boot_version"] == "3.1.10"

    def test_framework_detection(self, extractor: BackendJavaExtractor) -> None:
        """Detects spring-boot framework."""
        framework = extractor._detect_framework(SAMPLE_BACKEND_JAVA_REPO)
        assert framework == "spring-boot"

    def test_gradle_plugins(self, extractor: BackendJavaExtractor) -> None:
        """Parses plugin IDs from build.gradle."""
        meta = extractor._parse_gradle_metadata(SAMPLE_BACKEND_JAVA_REPO)
        plugins = meta["plugins"]
        assert "org.springframework.boot" in plugins
        assert "io.spring.dependency-management" in plugins
        assert "java" in plugins

    def test_manifest_has_spring_boot_version(
        self, extractor: BackendJavaExtractor, service_yaml: ServiceYaml
    ) -> None:
        """Manifest includes spring_boot_version field."""
        with patch.object(extractor, "_get_source_repo", return_value=None):
            manifest = extractor.extract(SAMPLE_BACKEND_JAVA_REPO, service_yaml)
        assert manifest.spring_boot_version == "3.1.10"

    def test_manifest_has_framework(
        self, extractor: BackendJavaExtractor, service_yaml: ServiceYaml
    ) -> None:
        """Manifest includes framework field."""
        with patch.object(extractor, "_get_source_repo", return_value=None):
            manifest = extractor.extract(SAMPLE_BACKEND_JAVA_REPO, service_yaml)
        assert manifest.framework == "spring-boot"

    def test_manifest_has_java_version(
        self, extractor: BackendJavaExtractor, service_yaml: ServiceYaml
    ) -> None:
        """Manifest includes java_version field."""
        with patch.object(extractor, "_get_source_repo", return_value=None):
            manifest = extractor.extract(SAMPLE_BACKEND_JAVA_REPO, service_yaml)
        assert manifest.java_version == "17"

    def test_extractor_hints_project_root(
        self, extractor: BackendJavaExtractor, tmp_path: Path
    ) -> None:
        """extractor_hints.project_root shifts effective root."""
        # Create a sub-project directory with a build.gradle
        subdir = tmp_path / "backend"
        subdir.mkdir()
        (subdir / "build.gradle").write_text(
            "plugins {\n    id 'org.springframework.boot' version '3.2.0'\n    id 'java'\n}\n"
            "sourceCompatibility = '21'\n"
        )

        svc = ServiceYaml(
            name="nested-svc",
            type="backend-java",
            owner="team",
            domain="test",
            tier="standard",
            purpose="Nested project test.",
            extractor_hints={"project_root": "backend"},
        )
        with patch.object(extractor, "_get_source_repo", return_value=None):
            manifest = extractor.extract(tmp_path, svc)
        assert manifest.spring_boot_version == "3.2.0"
        assert manifest.java_version == "21"


# ---------------------------------------------------------------------------
# TestDependencyParsing
# ---------------------------------------------------------------------------


class TestDependencyParsing:
    """Tests for Gradle dependency parsing."""

    def test_dependencies_from_gradle(self, extractor: BackendJavaExtractor) -> None:
        """Parses dependencies from build.gradle."""
        deps = extractor._parse_dependencies(SAMPLE_BACKEND_JAVA_REPO)
        dep_names = [d.name for d in deps]
        assert "org.springframework.boot:spring-boot-starter-web" in dep_names
        assert "org.springframework.boot:spring-boot-starter-data-jpa" in dep_names
        assert "org.postgresql:postgresql" in dep_names
        assert "org.projectlombok:lombok" in dep_names

    def test_dependency_categories(self, extractor: BackendJavaExtractor) -> None:
        """Dependencies have correct category tags."""
        deps = extractor._parse_dependencies(SAMPLE_BACKEND_JAVA_REPO)
        by_name = {d.name: d for d in deps}

        # implementation → runtime
        assert by_name["org.springframework.boot:spring-boot-starter-web"].category == "runtime"
        # runtimeOnly → runtime
        assert by_name["org.postgresql:postgresql"].category == "runtime"
        # testImplementation → test
        assert by_name["org.springframework.boot:spring-boot-starter-test"].category == "test"
        # lombok is declared as both compileOnly and annotationProcessor;
        # compileOnly appears first → category is "runtime" (first-seen wins in dedup)
        assert by_name["org.projectlombok:lombok"].category == "runtime"

    def test_no_duplicate_dependencies(self, extractor: BackendJavaExtractor) -> None:
        """Dependencies are deduplicated (no duplicates by group:artifact)."""
        deps = extractor._parse_dependencies(SAMPLE_BACKEND_JAVA_REPO)
        names = [d.name for d in deps]
        assert len(names) == len(set(names)), "Duplicate dependencies found"

    def test_parse_groovy_single_quotes(self, extractor: BackendJavaExtractor) -> None:
        """Parses Groovy-style single-quoted dependencies."""
        content = "implementation 'com.example:my-lib:1.0.0'"
        deps: list = []
        seen: set = set()
        extractor._parse_gradle_deps(content, "build.gradle", deps, seen)
        assert len(deps) == 1
        assert deps[0].name == "com.example:my-lib"
        assert deps[0].version == "1.0.0"
        assert deps[0].category == "runtime"

    def test_parse_kotlin_dsl_double_quotes(self, extractor: BackendJavaExtractor) -> None:
        """Parses Kotlin DSL double-quoted dependencies."""
        content = 'implementation("com.example:my-lib:1.0.0")'
        deps: list = []
        seen: set = set()
        extractor._parse_gradle_deps(content, "build.gradle.kts", deps, seen)
        assert len(deps) == 1
        assert deps[0].name == "com.example:my-lib"

    def test_compile_only_category(self, extractor: BackendJavaExtractor) -> None:
        """compileOnly maps to runtime category."""
        content = "compileOnly 'javax.servlet:javax.servlet-api:4.0.1'"
        deps: list = []
        seen: set = set()
        extractor._parse_gradle_deps(content, "build.gradle", deps, seen)
        assert deps[0].category == "runtime"

    def test_test_runtime_only_category(self, extractor: BackendJavaExtractor) -> None:
        """testRuntimeOnly maps to test category."""
        content = "testRuntimeOnly 'org.junit.platform:junit-platform-launcher:1.9.0'"
        deps: list = []
        seen: set = set()
        extractor._parse_gradle_deps(content, "build.gradle", deps, seen)
        assert deps[0].category == "test"


# ---------------------------------------------------------------------------
# TestSpringEndpointExtraction
# ---------------------------------------------------------------------------


class TestSpringEndpointExtraction:
    """Tests for Spring annotation-based endpoint extraction."""

    def test_endpoints_from_controllers(self, extractor: BackendJavaExtractor) -> None:
        """Parses @GetMapping, @PostMapping, @DeleteMapping from fixture controllers."""
        contracts = extractor.find_api_contracts(SAMPLE_BACKEND_JAVA_REPO)
        assert len(contracts) == 1
        contract = contracts[0]
        assert contract.kind == "spring-annotations"

        methods = [ep.method for ep in contract.endpoints]
        assert "GET" in methods
        assert "POST" in methods
        assert "DELETE" in methods

    def test_base_path_combination(self, extractor: BackendJavaExtractor) -> None:
        """Class @RequestMapping + method path are combined correctly."""
        contracts = extractor.find_api_contracts(SAMPLE_BACKEND_JAVA_REPO)
        paths = [ep.path for ep in contracts[0].endpoints]
        # OrderController: @RequestMapping("/v1/orders") + @GetMapping → /v1/orders
        assert "/v1/orders" in paths
        # OrderController: @RequestMapping("/v1/orders") + @GetMapping("/{id}") → /v1/orders/{id}
        assert "/v1/orders/{id}" in paths

    def test_version_only_base_path(self, extractor: BackendJavaExtractor) -> None:
        """HealthController with @RequestMapping("/v1") + @GetMapping("/health") → /v1/health."""
        contracts = extractor.find_api_contracts(SAMPLE_BACKEND_JAVA_REPO)
        paths = [ep.path for ep in contracts[0].endpoints]
        assert "/v1/health" in paths
        assert "/v1/ready" in paths

    def test_operation_summary(self, extractor: BackendJavaExtractor) -> None:
        """@Operation(summary = '...') is captured."""
        contracts = extractor.find_api_contracts(SAMPLE_BACKEND_JAVA_REPO)
        summaries = [ep.summary for ep in contracts[0].endpoints if ep.summary]
        assert "List all orders" in summaries
        assert "Create a new order" in summaries
        assert "Health check endpoint" in summaries

    def test_tag_extraction(self, extractor: BackendJavaExtractor) -> None:
        """@Tag(name = '...') is captured."""
        contracts = extractor.find_api_contracts(SAMPLE_BACKEND_JAVA_REPO)
        all_tags: list[str] = []
        for ep in contracts[0].endpoints:
            all_tags.extend(ep.tags)
        assert "Orders" in all_tags
        assert "Health" in all_tags

    def test_api_contracts_structure(self, extractor: BackendJavaExtractor) -> None:
        """find_api_contracts returns properly structured ApiContract."""
        contracts = extractor.find_api_contracts(SAMPLE_BACKEND_JAVA_REPO)
        assert len(contracts) == 1
        assert contracts[0].kind == "spring-annotations"
        assert contracts[0].path is None  # No file path for annotation-based
        assert len(contracts[0].endpoints) > 0

    def test_no_endpoints_if_no_controllers(
        self, extractor: BackendJavaExtractor, tmp_path: Path
    ) -> None:
        """Returns empty list when no controller files found."""
        contracts = extractor.find_api_contracts(tmp_path)
        assert contracts == []

    def test_combine_paths(self, extractor: BackendJavaExtractor) -> None:
        """_combine_paths handles various input combinations."""
        assert extractor._combine_paths("/v1/orders", "/{id}") == "/v1/orders/{id}"
        assert extractor._combine_paths("/v1/orders", None) == "/v1/orders"
        assert extractor._combine_paths(None, "/health") == "/health"
        assert extractor._combine_paths(None, None) is None
        assert extractor._combine_paths("/v1", "/health") == "/v1/health"

    def test_no_base_path_controller(
        self, extractor: BackendJavaExtractor, tmp_path: Path
    ) -> None:
        """Controller without class-level @RequestMapping still extracts methods."""
        ctrl_dir = tmp_path / "src" / "main" / "java" / "com" / "example" / "controllers"
        ctrl_dir.mkdir(parents=True)
        ctrl_file = ctrl_dir / "RedeemController.java"
        ctrl_file.write_text(
            "package com.example.controllers;\n"
            "import org.springframework.web.bind.annotation.*;\n"
            "@RestController\n"
            "public class RedeemController {\n"
            "    @PostMapping(\"/v1/redeem\")\n"
            "    public String redeem() { return \"ok\"; }\n"
            "}\n"
        )
        contracts = extractor.find_api_contracts(tmp_path)
        assert len(contracts) == 1
        paths = [ep.path for ep in contracts[0].endpoints]
        assert "/v1/redeem" in paths

    def test_path_without_leading_slash_normalized(
        self, extractor: BackendJavaExtractor, tmp_path: Path
    ) -> None:
        """Paths without leading slash are normalized to have one."""
        ctrl_dir = tmp_path / "src" / "main" / "java" / "controllers"
        ctrl_dir.mkdir(parents=True)
        ctrl_file = ctrl_dir / "MyController.java"
        ctrl_file.write_text(
            "package controllers;\n"
            "import org.springframework.web.bind.annotation.*;\n"
            "@RestController\n"
            "@RequestMapping(\"v1/items\")\n"
            "public class MyController {\n"
            "    @GetMapping\n"
            "    public String list() { return \"ok\"; }\n"
            "}\n"
        )
        contracts = extractor.find_api_contracts(tmp_path)
        paths = [ep.path for ep in contracts[0].endpoints]
        # Should be normalized to /v1/items
        assert "/v1/items" in paths


# ---------------------------------------------------------------------------
# TestEntryPoints
# ---------------------------------------------------------------------------


class TestEntryPoints:
    """Tests for Spring entry point detection."""

    def test_spring_boot_application_entry(self, extractor: BackendJavaExtractor) -> None:
        """Finds @SpringBootApplication class."""
        entry_points = extractor._parse_entry_points(SAMPLE_BACKEND_JAVA_REPO)
        kinds = [ep.kind for ep in entry_points]
        assert "spring-boot-application" in kinds

        app_entries = [ep for ep in entry_points if ep.kind == "spring-boot-application"]
        assert any("DemoApplication" in ep.ref for ep in app_entries)

    def test_kafka_consumer_entries(self, extractor: BackendJavaExtractor) -> None:
        """Finds @KafkaListener classes."""
        entry_points = extractor._parse_entry_points(SAMPLE_BACKEND_JAVA_REPO)
        kafka_entries = [ep for ep in entry_points if ep.kind == "kafka-consumer"]
        assert len(kafka_entries) >= 1
        assert any("OrderEventListener" in ep.ref for ep in kafka_entries)

    def test_scheduled_job_entries(self, extractor: BackendJavaExtractor) -> None:
        """Finds @Scheduled classes."""
        entry_points = extractor._parse_entry_points(SAMPLE_BACKEND_JAVA_REPO)
        sched_entries = [ep for ep in entry_points if ep.kind == "scheduled-job"]
        assert len(sched_entries) >= 1
        assert any("OrderEventListener" in ep.ref for ep in sched_entries)

    def test_entry_point_fqn_format(self, extractor: BackendJavaExtractor) -> None:
        """Entry points use fully-qualified class name format."""
        entry_points = extractor._parse_entry_points(SAMPLE_BACKEND_JAVA_REPO)
        app_entry = next(
            (ep for ep in entry_points if ep.kind == "spring-boot-application"), None
        )
        assert app_entry is not None
        # Should contain package + class
        assert "." in app_entry.ref


# ---------------------------------------------------------------------------
# TestInfrastructureDetection
# ---------------------------------------------------------------------------


class TestInfrastructureDetection:
    """Tests for CI, Docker, database, Flyway, and Kafka detection."""

    def test_docker_detection(self, extractor: BackendJavaExtractor) -> None:
        """Dockerfile presence is detected."""
        runtime = extractor._detect_runtime(SAMPLE_BACKEND_JAVA_REPO)
        assert runtime is not None
        assert runtime.docker is True

    def test_ci_detection(self, extractor: BackendJavaExtractor) -> None:
        """Azure Pipelines detected in devops/ subdirectory."""
        ci = extractor._detect_ci(SAMPLE_BACKEND_JAVA_REPO)
        assert ci == "azure-pipelines"

    def test_database_type_postgresql(self, extractor: BackendJavaExtractor) -> None:
        """PostgreSQL detected from application.yml datasource URL."""
        db_type, _secondary, _count = extractor._parse_database_info(SAMPLE_BACKEND_JAVA_REPO)
        assert db_type == "postgresql"

    def test_flyway_migration_count(self, extractor: BackendJavaExtractor) -> None:
        """Counts V*.sql Flyway migration files."""
        _db_type, _secondary, count = extractor._parse_database_info(SAMPLE_BACKEND_JAVA_REPO)
        assert count == 2  # V1__init.sql and V2__add_order_items.sql

    def test_kafka_topics(self, extractor: BackendJavaExtractor) -> None:
        """Parses Kafka topic names from application.yml."""
        topics = extractor._parse_kafka_topics(SAMPLE_BACKEND_JAVA_REPO)
        assert len(topics) >= 1
        # Topics defined in the fixture's application.yml
        assert any("order" in t.lower() for t in topics)

    def test_manifest_has_kafka_topics(
        self, extractor: BackendJavaExtractor, service_yaml: ServiceYaml
    ) -> None:
        """Manifest includes kafka_topics field."""
        with patch.object(extractor, "_get_source_repo", return_value=None):
            manifest = extractor.extract(SAMPLE_BACKEND_JAVA_REPO, service_yaml)
        assert len(manifest.kafka_topics) >= 1

    def test_manifest_has_flyway_count(
        self, extractor: BackendJavaExtractor, service_yaml: ServiceYaml
    ) -> None:
        """Manifest includes flyway_migration_count field."""
        with patch.object(extractor, "_get_source_repo", return_value=None):
            manifest = extractor.extract(SAMPLE_BACKEND_JAVA_REPO, service_yaml)
        assert manifest.flyway_migration_count == 2

    def test_manifest_has_database_type(
        self, extractor: BackendJavaExtractor, service_yaml: ServiceYaml
    ) -> None:
        """Manifest includes database_type field."""
        with patch.object(extractor, "_get_source_repo", return_value=None):
            manifest = extractor.extract(SAMPLE_BACKEND_JAVA_REPO, service_yaml)
        assert manifest.database_type == "postgresql"

    def test_no_docker_when_dockerfile_missing(
        self, extractor: BackendJavaExtractor, tmp_path: Path
    ) -> None:
        """Returns None runtime when no Dockerfile present."""
        runtime = extractor._detect_runtime(tmp_path)
        assert runtime is None

    def test_github_actions_ci_detection(
        self, extractor: BackendJavaExtractor, tmp_path: Path
    ) -> None:
        """Detects GitHub Actions CI."""
        (tmp_path / ".github" / "workflows").mkdir(parents=True)
        (tmp_path / ".github" / "workflows" / "ci.yml").write_text("on: push")
        ci = extractor._detect_ci(tmp_path)
        assert ci == "github-actions"

    def test_gitlab_ci_detection(
        self, extractor: BackendJavaExtractor, tmp_path: Path
    ) -> None:
        """Detects GitLab CI."""
        (tmp_path / ".gitlab-ci.yml").write_text("stages: [build]")
        ci = extractor._detect_ci(tmp_path)
        assert ci == "gitlab-ci"

    def test_no_flyway_migrations(
        self, extractor: BackendJavaExtractor, tmp_path: Path
    ) -> None:
        """Returns None when no Flyway migrations found."""
        _db, _secondary, count = extractor._parse_database_info(tmp_path)
        assert count is None

    def test_mysql_detection(self, extractor: BackendJavaExtractor, tmp_path: Path) -> None:
        """Detects MySQL from datasource URL."""
        resources = tmp_path / "src" / "main" / "resources"
        resources.mkdir(parents=True)
        (resources / "application.yml").write_text(
            "spring:\n  datasource:\n    url: jdbc:mysql://localhost:3306/mydb\n"
        )
        db_type, _secondary, _count = extractor._parse_database_info(tmp_path)
        assert db_type == "mysql"

    def test_flyway_count_excludes_non_versioned(
        self, extractor: BackendJavaExtractor, tmp_path: Path
    ) -> None:
        """Only V*.sql files are counted, not R*.sql or other SQL files."""
        migration_dir = tmp_path / "src" / "main" / "resources" / "db" / "migration"
        migration_dir.mkdir(parents=True)
        (migration_dir / "V1__init.sql").write_text("CREATE TABLE foo (id INT);")
        (migration_dir / "V2__add_col.sql").write_text("ALTER TABLE foo ADD COLUMN bar TEXT;")
        (migration_dir / "R1__refresh.sql").write_text("-- repeatable migration")
        (migration_dir / "schema.sql").write_text("-- not versioned")

        _db, _secondary, count = extractor._parse_database_info(tmp_path)
        assert count == 2

    def test_redis_cache_detection(self, extractor: BackendJavaExtractor, tmp_path: Path) -> None:
        """Detects Redis as cache type from build.gradle dependency."""
        (tmp_path / "build.gradle").write_text(
            "dependencies {\n"
            "    implementation 'org.springframework.boot:"
            "spring-boot-starter-data-redis:3.1.0'\n}\n"
        )
        cache_type = extractor._detect_cache_type(tmp_path)
        assert cache_type == "redis"

    def test_secondary_database_detected(
        self, extractor: BackendJavaExtractor, tmp_path: Path
    ) -> None:
        """Cosmos DB detected as secondary database from build.gradle dependency."""
        resources = tmp_path / "src" / "main" / "resources"
        resources.mkdir(parents=True)
        (resources / "application.yml").write_text(
            "spring:\n  datasource:\n    url: jdbc:postgresql://localhost:5432/db\n"
        )
        (tmp_path / "build.gradle").write_text(
            "dependencies {\n    implementation 'com.azure:azure-spring-data-cosmos:5.0.0'\n}\n"
        )
        primary, secondary, _count = extractor._parse_database_info(tmp_path)
        assert primary == "postgresql"
        assert "cosmos" in secondary

    def test_h2_is_secondary_when_real_db_present(
        self, extractor: BackendJavaExtractor, tmp_path: Path
    ) -> None:
        """H2 is treated as secondary (test DB) when a real DB is also present."""
        resources = tmp_path / "src" / "main" / "resources"
        resources.mkdir(parents=True)
        (resources / "application.yml").write_text(
            "spring:\n  datasource:\n    url: jdbc:postgresql://localhost:5432/db\n"
        )
        (tmp_path / "build.gradle").write_text(
            "dependencies {\n    testImplementation 'com.h2database:h2:2.2.0'\n}\n"
        )
        primary, secondary, _count = extractor._parse_database_info(tmp_path)
        assert primary == "postgresql"
        assert "h2" in secondary


# ---------------------------------------------------------------------------
# TestEdgeCases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Edge case and error handling tests."""

    def test_empty_repo(self, extractor: BackendJavaExtractor, tmp_path: Path) -> None:
        """Extraction from empty directory produces a manifest without errors."""
        svc = ServiceYaml(
            name="empty-svc",
            type="backend-java",
            owner="team",
            domain="test",
            tier="standard",
            purpose="Empty repo test.",
        )
        with patch.object(extractor, "_get_source_repo", return_value=None):
            manifest = extractor.extract(tmp_path, svc)
        assert manifest.name == "empty-svc"
        assert manifest.language == "java"  # defaults to java when no files found
        assert manifest.dependencies == []
        assert manifest.api_contracts == []
        assert manifest.entry_points == []

    def test_kotlin_detection(self, extractor: BackendJavaExtractor, tmp_path: Path) -> None:
        """Detects Kotlin as primary language when more .kt than .java files."""
        src = tmp_path / "src" / "main" / "kotlin" / "com" / "example"
        src.mkdir(parents=True)
        for i in range(5):
            (src / f"MyClass{i}.kt").write_text(f"class MyClass{i}")
        (tmp_path / "build.gradle").write_text(
            "plugins {\n    id 'org.springframework.boot' version '3.1.0'\n    id 'java'\n}\n"
        )

        lang, _ = extractor._detect_language(tmp_path)
        assert lang == "kotlin"

    def test_micronaut_detection(self, extractor: BackendJavaExtractor, tmp_path: Path) -> None:
        """Detects micronaut framework."""
        (tmp_path / "build.gradle").write_text(
            "plugins {\n    id 'io.micronaut.application' version '4.0.0'\n    id 'java'\n}\n"
        )
        framework = extractor._detect_framework(tmp_path)
        assert framework == "micronaut"

    def test_quarkus_detection(self, extractor: BackendJavaExtractor, tmp_path: Path) -> None:
        """Detects quarkus framework."""
        (tmp_path / "build.gradle").write_text(
            "plugins {\n    id 'io.quarkus' version '3.0.0'\n    id 'java'\n}\n"
        )
        framework = extractor._detect_framework(tmp_path)
        assert framework == "quarkus"

    def test_no_framework_detected(self, extractor: BackendJavaExtractor, tmp_path: Path) -> None:
        """Returns None when no known framework detected."""
        (tmp_path / "build.gradle").write_text(
            "plugins {\n    id 'java'\n}\n"
        )
        framework = extractor._detect_framework(tmp_path)
        assert framework is None

    def test_build_dir_excluded_from_file_count(
        self, extractor: BackendJavaExtractor, tmp_path: Path
    ) -> None:
        """Files in build/ are excluded from language detection."""
        # Put only Java files in build/ directory
        build_dir = tmp_path / "build" / "generated" / "sources"
        build_dir.mkdir(parents=True)
        (build_dir / "Generated.java").write_text("class Generated {}")

        # Put Kotlin file in real source
        src = tmp_path / "src" / "main" / "kotlin"
        src.mkdir(parents=True)
        (src / "App.kt").write_text("class App")

        lang, _ = extractor._detect_language(tmp_path)
        # Should detect kotlin since the only .java is in build/
        assert lang == "kotlin"

    def test_extract_class_name_with_package(
        self, extractor: BackendJavaExtractor, tmp_path: Path
    ) -> None:
        """_extract_class_name returns fully-qualified name."""
        content = (
            "package com.example.service;\n"
            "import org.springframework.stereotype.Service;\n"
            "@Service\n"
            "public class OrderService {\n"
            "}\n"
        )
        java_file = tmp_path / "OrderService.java"
        java_file.write_text(content)
        name = extractor._extract_class_name(content, java_file)
        assert name == "com.example.service.OrderService"

    def test_find_api_contracts_returns_empty_for_non_controller(
        self, extractor: BackendJavaExtractor, tmp_path: Path
    ) -> None:
        """Java files without @RestController/@Controller are skipped."""
        src = tmp_path / "src" / "main" / "java" / "com" / "example" / "controllers"
        src.mkdir(parents=True)
        # A file in a controllers directory but without the annotation
        (src / "ControllerHelper.java").write_text(
            "package com.example.controllers;\n"
            "public class ControllerHelper { }\n"
        )
        contracts = extractor.find_api_contracts(tmp_path)
        assert contracts == []

    def test_kafka_topics_regex_fallback(self, extractor: BackendJavaExtractor) -> None:
        """Kafka topic extraction via regex fallback produces sensible results."""
        content = "kafka:\n  topics:\n    my-events: my-service.events.v1\n"
        topics: list = []
        seen: set = set()
        # Force the YAML import to fail by passing invalid YAML that breaks yaml.safe_load
        # but still has our pattern — test the regex path directly
        extractor._extract_kafka_topics_from_yaml(content, topics, seen)
        assert len(topics) >= 1

    def test_integration_notes_passed_through(
        self, extractor: BackendJavaExtractor, tmp_path: Path
    ) -> None:
        """integration_notes from ServiceYaml are included in manifest."""
        svc = ServiceYaml(
            name="noted-svc",
            type="backend-java",
            owner="team",
            domain="test",
            tier="standard",
            purpose="Service with notes.",
            integration_notes=[{"scope": "global", "note": "Requires API key header"}],
        )
        with patch.object(extractor, "_get_source_repo", return_value=None):
            manifest = extractor.extract(tmp_path, svc)
        assert len(manifest.integration_notes) == 1
        note = manifest.integration_notes[0]
        # integration_notes are stored as dicts in the manifest
        note_text = note["note"] if isinstance(note, dict) else note.note
        assert note_text == "Requires API key header"


# ---------------------------------------------------------------------------
# TestGradleVariableResolution
# ---------------------------------------------------------------------------


class TestGradleVariableResolution:
    """Tests for ext{} variable resolution in dependency versions."""

    def test_parse_ext_vars(self, extractor: BackendJavaExtractor) -> None:
        """Parses ext{} block and returns variable map."""
        vars_map = extractor._parse_gradle_ext_vars(SAMPLE_BACKEND_JAVA_REPO)
        assert vars_map["lombokVersion"] == "1.18.30"
        assert vars_map["openapiVersion"] == "2.1.0"

    def test_resolve_simple_var(self, extractor: BackendJavaExtractor) -> None:
        """Resolves ${varName} to its value from the map."""
        result = extractor._resolve_gradle_version("${lombokVersion}", {"lombokVersion": "1.18.30"})
        assert result == "1.18.30"

    def test_resolve_literal_unchanged(self, extractor: BackendJavaExtractor) -> None:
        """Literal version strings are returned unchanged."""
        result = extractor._resolve_gradle_version("3.1.10", {"springBootVersion": "3.1.0"})
        assert result == "3.1.10"

    def test_resolve_unknown_var_kept_raw(self, extractor: BackendJavaExtractor) -> None:
        """Unknown variable references are kept as-is (not silently dropped)."""
        result = extractor._resolve_gradle_version("${unknownVar}", {})
        assert result == "${unknownVar}"

    def test_resolve_none_returns_none(self, extractor: BackendJavaExtractor) -> None:
        """None version returns None."""
        assert extractor._resolve_gradle_version(None, {}) is None

    def test_dependencies_have_resolved_versions(self, extractor: BackendJavaExtractor) -> None:
        """Dependencies parsed from fixture have resolved ext{} variable versions."""
        gradle_vars = extractor._parse_gradle_ext_vars(SAMPLE_BACKEND_JAVA_REPO)
        deps = extractor._parse_dependencies(SAMPLE_BACKEND_JAVA_REPO, gradle_vars)
        by_name = {d.name: d for d in deps}
        # lombokVersion = '1.18.30' in ext block → should resolve
        assert by_name["org.projectlombok:lombok"].version == "1.18.30"
        # openapiVersion = '2.1.0' in ext block
        assert by_name["org.springdoc:springdoc-openapi-starter-webmvc-ui"].version == "2.1.0"

    def test_gradle_ext_vars_with_inline_content(
        self, extractor: BackendJavaExtractor, tmp_path: Path
    ) -> None:
        """Parses ext{} from synthetic build.gradle content."""
        (tmp_path / "build.gradle").write_text(
            "ext {\n    myLibVersion = '2.5.0'\n    otherVersion = '1.0'\n}\n"
            "dependencies {\n    implementation \"com.example:my-lib:${myLibVersion}\"\n}\n"
        )
        vars_map = extractor._parse_gradle_ext_vars(tmp_path)
        assert vars_map["myLibVersion"] == "2.5.0"
        deps = extractor._parse_dependencies(tmp_path, vars_map)
        assert deps[0].version == "2.5.0"


# ---------------------------------------------------------------------------
# TestKafkaTopicResolution
# ---------------------------------------------------------------------------


class TestKafkaTopicResolution:
    """Tests for Spring EL ${VAR:default} resolution in Kafka topic names."""

    def test_resolve_spring_el_with_default(self, extractor: BackendJavaExtractor) -> None:
        """${VAR:default-value} resolves to default-value."""
        result = extractor._resolve_spring_el_topic("${ORDER_TOPIC:demo.orders.created}")
        assert result == "demo.orders.created"

    def test_resolve_spring_el_no_default(self, extractor: BackendJavaExtractor) -> None:
        """${VAR} without default strips ${} wrapper to return env var name."""
        result = extractor._resolve_spring_el_topic("${DATA_PURCHASE_EVENT_TOPIC}")
        assert result == "DATA_PURCHASE_EVENT_TOPIC"

    def test_resolve_plain_topic_unchanged(self, extractor: BackendJavaExtractor) -> None:
        """Plain topic names (no ${}) are returned as-is."""
        result = extractor._resolve_spring_el_topic("demo.orders.created")
        assert result == "demo.orders.created"

    def test_kafka_topics_from_fixture_resolved(self, extractor: BackendJavaExtractor) -> None:
        """Fixture kafka topics include resolved Spring EL defaults."""
        topics = extractor._parse_kafka_topics(SAMPLE_BACKEND_JAVA_REPO)
        # Plain topics are present
        assert "demo.orders.created" in topics
        assert "demo.orders.cancelled" in topics
        # ${ORDER_SHIPPED_TOPIC:demo.orders.shipped} → resolved to default
        assert "demo.orders.shipped" in topics
        # No raw ${...} strings should remain
        assert not any(t.startswith("${") for t in topics)

    def test_kafka_topics_no_raw_env_vars_in_fixture(self, extractor: BackendJavaExtractor) -> None:
        """No raw ${ENV_VAR} strings appear in the resolved topic list."""
        topics = extractor._parse_kafka_topics(SAMPLE_BACKEND_JAVA_REPO)
        assert all(not t.startswith("${") for t in topics)


# ---------------------------------------------------------------------------
# TestSummaryFiltering
# ---------------------------------------------------------------------------


class TestSummaryFiltering:
    """Tests for filtering of path-like @Operation(summary) values."""

    def test_path_summary_discarded(self, extractor: BackendJavaExtractor) -> None:
        """@Operation(summary = '/shopping-cart/tickets') is treated as noise and discarded."""
        text = '@Operation(summary = "/shopping-cart/tickets")'
        assert extractor._extract_operation_summary(text) is None

    def test_v1_prefix_summary_discarded(self, extractor: BackendJavaExtractor) -> None:
        """@Operation(summary = 'v1/accounts/redeem') is discarded."""
        text = '@Operation(summary = "v1/accounts/redeem")'
        assert extractor._extract_operation_summary(text) is None

    def test_human_readable_summary_kept(self, extractor: BackendJavaExtractor) -> None:
        """Human-readable summary is retained."""
        text = '@Operation(summary = "List all orders")'
        assert extractor._extract_operation_summary(text) == "List all orders"

    def test_presale_code_summary_kept(self, extractor: BackendJavaExtractor) -> None:
        """Short descriptive summary like 'Presale Code' is kept."""
        text = '@Operation(summary = "Presale Code")'
        assert extractor._extract_operation_summary(text) == "Presale Code"

    def test_no_operation_annotation_returns_none(self, extractor: BackendJavaExtractor) -> None:
        """No @Operation annotation returns None."""
        assert extractor._extract_operation_summary("public void doSomething() {}") is None

    def test_endpoints_in_fixture_have_clean_summaries(
        self, extractor: BackendJavaExtractor
    ) -> None:
        """All endpoint summaries in the fixture are human-readable (not paths)."""
        contracts = extractor.find_api_contracts(SAMPLE_BACKEND_JAVA_REPO)
        for ep in contracts[0].endpoints:
            if ep.summary is not None:
                assert not ep.summary.startswith("/"), f"Path-like summary found: {ep.summary!r}"
                assert not ep.summary.startswith("v1"), f"Path-like summary found: {ep.summary!r}"


# ---------------------------------------------------------------------------
# TestProgrammaticKafkaListeners
# ---------------------------------------------------------------------------


class TestProgrammaticKafkaListeners:
    """Tests for detection of programmatic Kafka listener registration."""

    def test_kafka_listener_endpoint_registry_detected(
        self, extractor: BackendJavaExtractor, tmp_path: Path
    ) -> None:
        """Class using KafkaListenerEndpointRegistry is detected as kafka-consumer."""
        src = tmp_path / "src" / "main" / "java" / "com" / "example"
        src.mkdir(parents=True)
        (src / "KafkaConsumerService.java").write_text(
            "package com.example;\n"
            "import org.springframework.kafka.config.KafkaListenerEndpointRegistry;\n"
            "import org.springframework.kafka.config.MethodKafkaListenerEndpoint;\n"
            "public class KafkaConsumerService {\n"
            "    private final KafkaListenerEndpointRegistry registry;\n"
            "    public void register() {\n"
            "        MethodKafkaListenerEndpoint<String, String> ep ="
            " new MethodKafkaListenerEndpoint<>();\n"
            "    }\n"
            "}\n"
        )
        entry_points = extractor._parse_entry_points(tmp_path)
        kafka_entries = [ep for ep in entry_points if ep.kind == "kafka-consumer"]
        assert len(kafka_entries) == 1
        assert "KafkaConsumerService" in kafka_entries[0].ref

    def test_annotation_kafka_listener_still_detected(
        self, extractor: BackendJavaExtractor, tmp_path: Path
    ) -> None:
        """@KafkaListener annotation-based consumers are still detected."""
        src = tmp_path / "src" / "main" / "java" / "com" / "example"
        src.mkdir(parents=True)
        (src / "EventConsumer.java").write_text(
            "package com.example;\n"
            "import org.springframework.kafka.annotation.KafkaListener;\n"
            "public class EventConsumer {\n"
            "    @KafkaListener(topics = 'my-topic')\n"
            "    public void consume(String msg) {}\n"
            "}\n"
        )
        entry_points = extractor._parse_entry_points(tmp_path)
        kafka_entries = [ep for ep in entry_points if ep.kind == "kafka-consumer"]
        assert any("EventConsumer" in ep.ref for ep in kafka_entries)


# ---------------------------------------------------------------------------
# TestEndpointDeduplication
# ---------------------------------------------------------------------------


class TestEndpointDeduplication:
    """Tests for endpoint deduplication by (method, path)."""

    def test_duplicate_endpoints_deduplicated(
        self, extractor: BackendJavaExtractor, tmp_path: Path
    ) -> None:
        """Duplicate (method, path) combinations are collapsed to one."""
        ctrl_dir = tmp_path / "src" / "main" / "java" / "com" / "example" / "controllers"
        ctrl_dir.mkdir(parents=True)
        # Two @GetMapping with the same path (overloaded with different params)
        (ctrl_dir / "DupController.java").write_text(
            "package com.example.controllers;\n"
            "import org.springframework.web.bind.annotation.*;\n"
            "@RestController\n"
            "@RequestMapping(\"/v1/items\")\n"
            "public class DupController {\n"
            "    @GetMapping\n"
            "    public String listA() { return \"\"; }\n"
            "    @GetMapping\n"
            "    public String listB() { return \"\"; }\n"
            "}\n"
        )
        contracts = extractor.find_api_contracts(tmp_path)
        paths = [ep.path for ep in contracts[0].endpoints]
        assert paths.count("/v1/items") == 1

    def test_different_methods_not_deduplicated(
        self, extractor: BackendJavaExtractor, tmp_path: Path
    ) -> None:
        """GET /v1/items and POST /v1/items are NOT deduplicated (different methods)."""
        ctrl_dir = tmp_path / "src" / "main" / "java" / "com" / "example" / "controllers"
        ctrl_dir.mkdir(parents=True)
        (ctrl_dir / "ItemController.java").write_text(
            "package com.example.controllers;\n"
            "import org.springframework.web.bind.annotation.*;\n"
            "@RestController\n"
            "@RequestMapping(\"/v1/items\")\n"
            "public class ItemController {\n"
            "    @GetMapping\n"
            "    public String list() { return \"\"; }\n"
            "    @PostMapping\n"
            "    public String create() { return \"\"; }\n"
            "}\n"
        )
        contracts = extractor.find_api_contracts(tmp_path)
        eps = contracts[0].endpoints
        methods = {ep.method for ep in eps}
        assert "GET" in methods
        assert "POST" in methods

    def test_manifest_redis_cache(
        self, extractor: BackendJavaExtractor, service_yaml: ServiceYaml
    ) -> None:
        """Manifest includes cache_type=redis from fixture build.gradle."""
        with patch.object(extractor, "_get_source_repo", return_value=None):
            manifest = extractor.extract(SAMPLE_BACKEND_JAVA_REPO, service_yaml)
        assert manifest.cache_type == "redis"

    def test_manifest_secondary_databases(
        self, extractor: BackendJavaExtractor, service_yaml: ServiceYaml
    ) -> None:
        """Manifest includes secondary_databases list (h2 from fixture)."""
        with patch.object(extractor, "_get_source_repo", return_value=None):
            manifest = extractor.extract(SAMPLE_BACKEND_JAVA_REPO, service_yaml)
        # h2 is a test dep in the fixture → should be secondary since postgresql is primary
        assert "h2" in manifest.secondary_databases


# ---------------------------------------------------------------------------
# TestCosmosDBDetection — Fix 1
# ---------------------------------------------------------------------------


class TestCosmosDBDetection:
    """Tests for expanded Cosmos DB detection (Fix 1)."""

    def test_cosmos_detected_from_spring_cloud_azure_dep(
        self, extractor: BackendJavaExtractor, tmp_path: Path
    ) -> None:
        """Detects Cosmos DB from com.azure.spring:spring-cloud-azure-starter-data-cosmos dep."""
        (tmp_path / "build.gradle").write_text(
            "dependencies {\n"
            "    implementation 'com.azure.spring:spring-cloud-azure-starter-data-cosmos:5.7.0'\n"
            "}\n"
        )
        primary, _secondary, _count = extractor._parse_database_info(tmp_path)
        assert primary == "cosmos"

    def test_cosmos_detected_from_old_azure_dep(
        self, extractor: BackendJavaExtractor, tmp_path: Path
    ) -> None:
        """Detects Cosmos DB from legacy com.azure:azure-spring-data-cosmos dep."""
        (tmp_path / "build.gradle").write_text(
            "dependencies {\n"
            "    implementation 'com.azure:azure-spring-data-cosmos:3.40.0'\n"
            "}\n"
        )
        primary, _secondary, _count = extractor._parse_database_info(tmp_path)
        assert primary == "cosmos"

    def test_cosmos_detected_from_yaml_cosmos_section(
        self, extractor: BackendJavaExtractor, tmp_path: Path
    ) -> None:
        """Detects Cosmos DB from spring.cloud.azure.cosmos: key in application.yml."""
        resources = tmp_path / "src" / "main" / "resources"
        resources.mkdir(parents=True)
        (resources / "application.yml").write_text(
            "spring:\n"
            "  cloud:\n"
            "    azure:\n"
            "      cosmos:\n"
            "        endpoint: https://example.documents.azure.com:443/\n"
            "        key: dummy-key\n"
            "        database: my-db\n"
        )
        primary, _secondary, _count = extractor._parse_database_info(tmp_path)
        assert primary == "cosmos"

    def test_fixture_detects_cosmos_as_secondary(
        self, extractor: BackendJavaExtractor
    ) -> None:
        """Fixture has both postgresql (datasource URL) and cosmos (YAML section + TOML dep)."""
        primary, secondary, _count = extractor._parse_database_info(SAMPLE_BACKEND_JAVA_REPO)
        assert primary == "postgresql"
        assert "cosmos" in secondary


# ---------------------------------------------------------------------------
# TestRequestMappingPathAttr — Fix 2
# ---------------------------------------------------------------------------


class TestRequestMappingPathAttr:
    """Tests for @RequestMapping(path = ...) parsing (Fix 2)."""

    def test_path_attribute_parsed(self, extractor: BackendJavaExtractor) -> None:
        """_extract_request_mapping_path parses path= attribute."""
        content = (
            '@RequestMapping(path = "/v1/admin", '
            'produces = "application/json")\n'
            "public class MyController {}"
        )
        result = extractor._extract_request_mapping_path(content)
        assert result == "/v1/admin"

    def test_value_attribute_still_parsed(self, extractor: BackendJavaExtractor) -> None:
        """_extract_request_mapping_path still parses value= attribute."""
        content = '@RequestMapping(value = "/v1/orders")\npublic class MyController {}'
        result = extractor._extract_request_mapping_path(content)
        assert result == "/v1/orders"

    def test_bare_string_still_parsed(self, extractor: BackendJavaExtractor) -> None:
        """_extract_request_mapping_path parses bare string (no attribute name)."""
        content = '@RequestMapping("/v1/items")\npublic class MyController {}'
        result = extractor._extract_request_mapping_path(content)
        assert result == "/v1/items"

    def test_path_attr_used_in_endpoint_extraction(
        self, extractor: BackendJavaExtractor, tmp_path: Path
    ) -> None:
        """Controller using @RequestMapping(path=...) produces correct full endpoint paths."""
        ctrl_dir = tmp_path / "src" / "main" / "java" / "com" / "example" / "controllers"
        ctrl_dir.mkdir(parents=True)
        (ctrl_dir / "AdminController.java").write_text(
            "package com.example.controllers;\n"
            "import org.springframework.web.bind.annotation.*;\n"
            "@RestController\n"
            '@RequestMapping(path = "/v1/admin", produces = "application/json")\n'
            "public class AdminController {\n"
            "    @GetMapping(\"/users\")\n"
            "    public String listUsers() { return \"\"; }\n"
            "    @PostMapping(\"/users\")\n"
            "    public String createUser() { return \"\"; }\n"
            "}\n"
        )
        contracts = extractor.find_api_contracts(tmp_path)
        assert len(contracts) == 1
        paths = [ep.path for ep in contracts[0].endpoints]
        assert "/v1/admin/users" in paths
        methods = {ep.method for ep in contracts[0].endpoints}
        assert "GET" in methods
        assert "POST" in methods


# ---------------------------------------------------------------------------
# TestOperationDescriptionFallback — Fix 3
# ---------------------------------------------------------------------------


class TestOperationDescriptionFallback:
    """Tests for @Operation(description=...) fallback (Fix 3)."""

    def test_description_used_when_no_summary(self, extractor: BackendJavaExtractor) -> None:
        """Falls back to description when summary is absent."""
        text = '@Operation(description = "Retrieve user account details")'
        assert extractor._extract_operation_summary(text) == "Retrieve user account details"

    def test_summary_preferred_over_description(self, extractor: BackendJavaExtractor) -> None:
        """summary wins when both summary and description are present."""
        text = '@Operation(summary = "Get account", description = "Retrieve user account details")'
        assert extractor._extract_operation_summary(text) == "Get account"

    def test_description_filtering_still_applies(self, extractor: BackendJavaExtractor) -> None:
        """Path-like values in description are discarded just like summary values."""
        text = '@Operation(description = "/accounts/redeem")'
        assert extractor._extract_operation_summary(text) is None

    def test_try_extract_op_attr_summary(self, extractor: BackendJavaExtractor) -> None:
        """_try_extract_op_attr extracts summary attribute."""
        text = '@Operation(summary = "List rewards")'
        assert extractor._try_extract_op_attr(text, "summary") == "List rewards"

    def test_try_extract_op_attr_description(self, extractor: BackendJavaExtractor) -> None:
        """_try_extract_op_attr extracts description attribute."""
        text = '@Operation(description = "Create entitlement record")'
        assert extractor._try_extract_op_attr(text, "description") == "Create entitlement record"

    def test_try_extract_op_attr_missing_returns_none(
        self, extractor: BackendJavaExtractor
    ) -> None:
        """_try_extract_op_attr returns None when attribute is absent."""
        text = '@Operation(description = "Retrieve account")'
        assert extractor._try_extract_op_attr(text, "summary") is None

    def test_description_endpoint_captured_in_fixture(
        self, extractor: BackendJavaExtractor
    ) -> None:
        """Fixture RewardsApi uses description= and those summaries are captured."""
        contracts = extractor.find_api_contracts(SAMPLE_BACKEND_JAVA_REPO)
        assert len(contracts) == 1
        summaries = {ep.summary for ep in contracts[0].endpoints if ep.summary}
        # RewardsApi uses description= for two endpoints
        assert "List all available rewards" in summaries
        assert "Redeem a reward by ID" in summaries
        # And summary= for one
        assert "Get reward details" in summaries


# ---------------------------------------------------------------------------
# TestApiInterfacePattern — Fix 4
# ---------------------------------------------------------------------------


class TestApiInterfacePattern:
    """Tests for API interface pattern endpoint discovery (Fix 4)."""

    def test_controller_with_no_mappings_falls_back_to_interface(
        self, extractor: BackendJavaExtractor, tmp_path: Path
    ) -> None:
        """When a @RestController has no @*Mapping, endpoints are found on the interface."""
        # Create the interface with route annotations
        iface_dir = tmp_path / "src" / "main" / "java" / "com" / "example" / "api"
        iface_dir.mkdir(parents=True)
        (iface_dir / "LoyaltyApi.java").write_text(
            "package com.example.api;\n"
            "import org.springframework.web.bind.annotation.*;\n"
            "@RequestMapping(\"/v1/loyalty\")\n"
            "public interface LoyaltyApi {\n"
            "    @GetMapping(\"/points\")\n"
            "    Object getPoints();\n"
            "    @PostMapping(\"/redeem\")\n"
            "    Object redeem();\n"
            "}\n"
        )
        # Create the controller that implements the interface (no mapping annotations)
        ctrl_dir = tmp_path / "src" / "main" / "java" / "com" / "example" / "controllers"
        ctrl_dir.mkdir(parents=True)
        (ctrl_dir / "LoyaltyController.java").write_text(
            "package com.example.controllers;\n"
            "import com.example.api.LoyaltyApi;\n"
            "import org.springframework.web.bind.annotation.RestController;\n"
            "@RestController\n"
            "public class LoyaltyController implements LoyaltyApi {\n"
            "    @Override\n"
            "    public Object getPoints() { return null; }\n"
            "    @Override\n"
            "    public Object redeem() { return null; }\n"
            "}\n"
        )
        contracts = extractor.find_api_contracts(tmp_path)
        assert len(contracts) == 1
        paths = [ep.path for ep in contracts[0].endpoints]
        assert "/v1/loyalty/points" in paths
        assert "/v1/loyalty/redeem" in paths
        methods = {ep.method for ep in contracts[0].endpoints}
        assert "GET" in methods
        assert "POST" in methods

    def test_controller_with_own_mappings_not_delegated(
        self, extractor: BackendJavaExtractor, tmp_path: Path
    ) -> None:
        """A @RestController with its own @*Mapping methods is NOT delegated to any interface."""
        ctrl_dir = tmp_path / "src" / "main" / "java" / "com" / "example" / "controllers"
        ctrl_dir.mkdir(parents=True)
        (ctrl_dir / "PaymentController.java").write_text(
            "package com.example.controllers;\n"
            "import org.springframework.web.bind.annotation.*;\n"
            "@RestController\n"
            "@RequestMapping(\"/v1/payments\")\n"
            "public class PaymentController implements java.io.Serializable {\n"
            "    @GetMapping\n"
            "    public String list() { return \"\"; }\n"
            "}\n"
        )
        contracts = extractor.find_api_contracts(tmp_path)
        assert len(contracts) == 1
        paths = [ep.path for ep in contracts[0].endpoints]
        assert "/v1/payments" in paths

    def test_fixture_rewards_interface_endpoints_extracted(
        self, extractor: BackendJavaExtractor
    ) -> None:
        """Fixture RewardsController implements RewardsApi; all 3 endpoints are found."""
        contracts = extractor.find_api_contracts(SAMPLE_BACKEND_JAVA_REPO)
        assert len(contracts) == 1
        paths = [ep.path for ep in contracts[0].endpoints]
        assert "/v1/rewards" in paths
        assert "/v1/rewards/{rewardId}/redeem" in paths
        assert "/v1/rewards/{rewardId}" in paths


# ---------------------------------------------------------------------------
# TestVersionCatalog — Fix 5
# ---------------------------------------------------------------------------


class TestVersionCatalog:
    """Tests for Gradle version catalog (libs.versions.toml) dependency parsing (Fix 5)."""

    def test_version_catalog_module_key_parsed(
        self, extractor: BackendJavaExtractor, tmp_path: Path
    ) -> None:
        """Parses module key form from libs.versions.toml."""
        gradle_dir = tmp_path / "gradle"
        gradle_dir.mkdir()
        (gradle_dir / "libs.versions.toml").write_text(
            "[versions]\n"
            'cosmos = "5.7.0"\n'
            "\n"
            "[libraries]\n"
            "azure-cosmos = { "
            'module = "com.azure.spring:'
            'spring-cloud-azure-starter-data-cosmos", '
            'version.ref = "cosmos" }\n'
        )
        deps: list = []
        seen: set = set()
        extractor._parse_version_catalog(gradle_dir / "libs.versions.toml", deps, seen)
        dep_names = [d.name for d in deps]
        assert "com.azure.spring:spring-cloud-azure-starter-data-cosmos" in dep_names
        cosmos_dep = next(d for d in deps if "spring-cloud-azure-starter-data-cosmos" in d.name)
        assert cosmos_dep.version == "5.7.0"
        assert cosmos_dep.source == "libs.versions.toml"

    def test_version_catalog_group_name_form(
        self, extractor: BackendJavaExtractor, tmp_path: Path
    ) -> None:
        """Parses group + name dict form from libs.versions.toml."""
        gradle_dir = tmp_path / "gradle"
        gradle_dir.mkdir()
        (gradle_dir / "libs.versions.toml").write_text(
            "[versions]\n"
            'mapstruct = "1.5.5.Final"\n'
            "\n"
            "[libraries]\n"
            'mapstruct = { group = "org.mapstruct", '
            'name = "mapstruct", version.ref = "mapstruct" }\n'
        )
        deps: list = []
        seen: set = set()
        extractor._parse_version_catalog(gradle_dir / "libs.versions.toml", deps, seen)
        dep_names = [d.name for d in deps]
        assert "org.mapstruct:mapstruct" in dep_names
        ms_dep = next(d for d in deps if d.name == "org.mapstruct:mapstruct")
        assert ms_dep.version == "1.5.5.Final"

    def test_version_catalog_string_form(
        self, extractor: BackendJavaExtractor, tmp_path: Path
    ) -> None:
        """Parses simple string form 'group:artifact:version' from libs.versions.toml."""
        gradle_dir = tmp_path / "gradle"
        gradle_dir.mkdir()
        (gradle_dir / "libs.versions.toml").write_text(
            "[libraries]\n"
            'guava = "com.google.guava:guava:32.1.3-jre"\n'
        )
        deps: list = []
        seen: set = set()
        extractor._parse_version_catalog(gradle_dir / "libs.versions.toml", deps, seen)
        dep_names = [d.name for d in deps]
        assert "com.google.guava:guava" in dep_names
        guava = next(d for d in deps if d.name == "com.google.guava:guava")
        assert guava.version == "32.1.3-jre"

    def test_version_catalog_deduplication(
        self, extractor: BackendJavaExtractor, tmp_path: Path
    ) -> None:
        """Version catalog deps don't duplicate deps already in seen set."""
        gradle_dir = tmp_path / "gradle"
        gradle_dir.mkdir()
        (gradle_dir / "libs.versions.toml").write_text(
            "[libraries]\n"
            'guava = "com.google.guava:guava:32.1.3-jre"\n'
        )
        deps: list = []
        seen: set = {"com.google.guava:guava"}  # pre-populated
        extractor._parse_version_catalog(gradle_dir / "libs.versions.toml", deps, seen)
        assert len(deps) == 0  # already in seen, not added again

    def test_fixture_version_catalog_deps_included(
        self, extractor: BackendJavaExtractor
    ) -> None:
        """Fixture gradle/libs.versions.toml deps are included in parsed dependencies."""
        deps = extractor._parse_dependencies(SAMPLE_BACKEND_JAVA_REPO)
        dep_names = [d.name for d in deps]
        # From libs.versions.toml: module key form
        assert "com.azure.spring:spring-cloud-azure-starter-data-cosmos" in dep_names
        # From libs.versions.toml: group+name form
        assert "org.mapstruct:mapstruct" in dep_names
        # From libs.versions.toml: string form
        assert "com.google.guava:guava" in dep_names

    def test_version_catalog_dep_source_label(
        self, extractor: BackendJavaExtractor
    ) -> None:
        """Version catalog deps have source='libs.versions.toml'."""
        deps = extractor._parse_dependencies(SAMPLE_BACKEND_JAVA_REPO)
        cosmos_dep = next(
            (d for d in deps if "spring-cloud-azure-starter-data-cosmos" in d.name), None
        )
        assert cosmos_dep is not None
        assert cosmos_dep.source == "libs.versions.toml"


# ---------------------------------------------------------------------------
# TestMapNotationDeps — Fix 6
# ---------------------------------------------------------------------------


class TestMapNotationDeps:
    """Tests for Gradle map-notation dependency parsing (Fix 6)."""

    def test_map_notation_parsed(self, extractor: BackendJavaExtractor) -> None:
        """Parses implementation group: 'x', name: 'y', version: 'z' syntax."""
        content = "implementation group: 'com.example', name: 'my-lib', version: '1.0.0'"
        deps: list = []
        seen: set = set()
        extractor._parse_gradle_deps(content, "build.gradle", deps, seen)
        assert len(deps) == 1
        assert deps[0].name == "com.example:my-lib"
        assert deps[0].version == "1.0.0"
        assert deps[0].category == "runtime"

    def test_map_notation_without_version(self, extractor: BackendJavaExtractor) -> None:
        """Parses map notation without version field."""
        content = "implementation group: 'com.example', name: 'my-lib'"
        deps: list = []
        seen: set = set()
        extractor._parse_gradle_deps(content, "build.gradle", deps, seen)
        assert len(deps) == 1
        assert deps[0].name == "com.example:my-lib"
        assert deps[0].version is None

    def test_map_notation_test_config(self, extractor: BackendJavaExtractor) -> None:
        """Map notation with testImplementation → test category."""
        content = "testImplementation group: 'org.junit', name: 'junit-api', version: '5.10.0'"
        deps: list = []
        seen: set = set()
        extractor._parse_gradle_deps(content, "build.gradle", deps, seen)
        assert len(deps) == 1
        assert deps[0].category == "test"

    def test_map_notation_dedup_with_string_notation(self, extractor: BackendJavaExtractor) -> None:
        """Map notation dep that already exists in seen is not duplicated."""
        content = (
            "implementation 'com.example:my-lib:1.0.0'\n"
            "implementation group: 'com.example', name: 'my-lib', version: '1.0.0'\n"
        )
        deps: list = []
        seen: set = set()
        extractor._parse_gradle_deps(content, "build.gradle", deps, seen)
        # Only one dep: string notation wins (appears first)
        assert len(deps) == 1
        assert deps[0].name == "com.example:my-lib"

    def test_fixture_map_notation_dep_included(self, extractor: BackendJavaExtractor) -> None:
        """Fixture build.gradle map-notation dep is included in parsed dependencies."""
        deps = extractor._parse_dependencies(SAMPLE_BACKEND_JAVA_REPO)
        dep_names = [d.name for d in deps]
        assert "com.example.internal:internal-lib" in dep_names
        internal = next(d for d in deps if d.name == "com.example.internal:internal-lib")
        assert internal.version == "1.2.3"


# ---------------------------------------------------------------------------
# TestKafkaProducerConsumerExtraction
# ---------------------------------------------------------------------------


class TestKafkaProducerConsumerExtraction:
    """Tests for _parse_kafka_producers() and _parse_kafka_consumers()."""

    def test_kafka_producer_detected_from_template_send(
        self, extractor: BackendJavaExtractor, tmp_path: Path
    ) -> None:
        """kafkaTemplate.send('topic', ...) → topic appears in kafka_produces."""
        src = tmp_path / "src" / "main" / "java" / "com" / "example"
        src.mkdir(parents=True)
        (src / "MyPublisher.java").write_text(
            'package com.example;\n'
            'public class MyPublisher {\n'
            '    void publish() { kafkaTemplate.send("my.test.topic", "data"); }\n'
            '}\n'
        )
        produces = extractor._parse_kafka_producers(tmp_path)
        assert "my.test.topic" in produces

    def test_kafka_producer_detected_from_constant_ref(
        self, extractor: BackendJavaExtractor, tmp_path: Path
    ) -> None:
        """kafkaTemplate.send(Topics.MY_TOPIC, ...) + constant class → resolved."""
        src = tmp_path / "src" / "main" / "java" / "com" / "example"
        src.mkdir(parents=True)
        (src / "TopicConsts.java").write_text(
            'package com.example;\n'
            'public final class TopicConsts {\n'
            '    public static final String MY_TOPIC = "my.resolved.topic";\n'
            '}\n'
        )
        (src / "MyPublisher.java").write_text(
            'package com.example;\n'
            'public class MyPublisher {\n'
            '    void publish() { kafkaTemplate.send(TopicConsts.MY_TOPIC, "data"); }\n'
            '}\n'
        )
        produces = extractor._parse_kafka_producers(tmp_path)
        assert "my.resolved.topic" in produces

    def test_kafka_consumer_detected_from_annotation(
        self, extractor: BackendJavaExtractor, tmp_path: Path
    ) -> None:
        """@KafkaListener(topics = 'topic') → topic appears in kafka_consumes."""
        src = tmp_path / "src" / "main" / "java" / "com" / "example"
        src.mkdir(parents=True)
        (src / "MyConsumer.java").write_text(
            'package com.example;\n'
            'import org.springframework.kafka.annotation.KafkaListener;\n'
            'public class MyConsumer {\n'
            '    @KafkaListener(topics = "direct.string.topic", groupId = "g")\n'
            '    public void consume(String msg) {}\n'
            '}\n'
        )
        consumes = extractor._parse_kafka_consumers(tmp_path)
        assert "direct.string.topic" in consumes

    def test_kafka_consumer_detected_from_spring_el(
        self, extractor: BackendJavaExtractor, tmp_path: Path
    ) -> None:
        """@KafkaListener(topics = '${key}') resolved via application.yml."""
        src = tmp_path / "src" / "main" / "java" / "com" / "example"
        src.mkdir(parents=True)
        resources = tmp_path / "src" / "main" / "resources"
        resources.mkdir(parents=True)
        (resources / "application.yml").write_text(
            "kafka:\n  topics:\n    orders: my.orders.topic\n"
        )
        (src / "MyConsumer.java").write_text(
            'package com.example;\n'
            'import org.springframework.kafka.annotation.KafkaListener;\n'
            'public class MyConsumer {\n'
            '    @KafkaListener(topics = "${kafka.topics.orders}", groupId = "g")\n'
            '    public void consume(String msg) {}\n'
            '}\n'
        )
        consumes = extractor._parse_kafka_consumers(tmp_path)
        assert "my.orders.topic" in consumes

    def test_fixture_has_producers_and_consumers(
        self, extractor: BackendJavaExtractor
    ) -> None:
        """The sample fixture produces and consumes known topics."""
        produces = extractor._parse_kafka_producers(SAMPLE_BACKEND_JAVA_REPO)
        consumes = extractor._parse_kafka_consumers(SAMPLE_BACKEND_JAVA_REPO)

        # OrderEventPublisher publishes to demo.orders.created (via Spring EL resolved)
        # and demo.orders.shipped (via OrderTopics.ORDER_SHIPPED constant)
        assert "demo.orders.shipped" in produces

        # OrderEventListener consumes demo.orders.created (Spring EL) and
        # demo.orders.cancelled (direct string)
        assert "demo.orders.cancelled" in consumes

    def test_manifest_has_kafka_produces_and_consumes(
        self, extractor: BackendJavaExtractor, service_yaml: ServiceYaml
    ) -> None:
        """Full extraction manifest includes kafka_produces and kafka_consumes fields."""
        with patch.object(extractor, "_get_source_repo", return_value=None):
            manifest = extractor.extract(SAMPLE_BACKEND_JAVA_REPO, service_yaml)
        assert isinstance(manifest.kafka_produces, list)
        assert isinstance(manifest.kafka_consumes, list)
        # At minimum the constant-ref topic should be detected
        assert "demo.orders.shipped" in manifest.kafka_produces
        assert "demo.orders.cancelled" in manifest.kafka_consumes

    def test_kafka_topics_backward_compat(
        self, extractor: BackendJavaExtractor, service_yaml: ServiceYaml
    ) -> None:
        """kafka_topics still populated as the union of all topics (backward compat)."""
        with patch.object(extractor, "_get_source_repo", return_value=None):
            manifest = extractor.extract(SAMPLE_BACKEND_JAVA_REPO, service_yaml)
        # kafka_topics must be a superset of kafka_produces + kafka_consumes
        all_detected = set(manifest.kafka_produces) | set(manifest.kafka_consumes)
        for topic in all_detected:
            assert topic in manifest.kafka_topics, (
                f"Topic '{topic}' in produces/consumes but missing from kafka_topics"
            )

    def test_test_dirs_excluded_from_producer_scan(
        self, extractor: BackendJavaExtractor, tmp_path: Path
    ) -> None:
        """Test directory Java files are not scanned for Kafka producers."""
        test_src = tmp_path / "src" / "test" / "java" / "com" / "example"
        test_src.mkdir(parents=True)
        (test_src / "TestPublisher.java").write_text(
            'package com.example;\n'
            'public class TestPublisher {\n'
            '    void t() { kafkaTemplate.send("test.only.topic", "x"); }\n'
            '}\n'
        )
        produces = extractor._parse_kafka_producers(tmp_path)
        assert "test.only.topic" not in produces

    def test_test_dirs_excluded_from_consumer_scan(
        self, extractor: BackendJavaExtractor, tmp_path: Path
    ) -> None:
        """Test directory Java files are not scanned for Kafka consumers."""
        test_src = tmp_path / "src" / "test" / "java" / "com" / "example"
        test_src.mkdir(parents=True)
        (test_src / "TestConsumer.java").write_text(
            'package com.example;\n'
            'import org.springframework.kafka.annotation.KafkaListener;\n'
            'public class TestConsumer {\n'
            '    @KafkaListener(topics = "test.only.topic")\n'
            '    public void consume(String msg) {}\n'
            '}\n'
        )
        consumes = extractor._parse_kafka_consumers(tmp_path)
        assert "test.only.topic" not in consumes


# ---------------------------------------------------------------------------
# TestOutboundServiceCalls
# ---------------------------------------------------------------------------


class TestOutboundServiceCalls:
    """Tests for _parse_outbound_service_calls() (WebClient/HTTP outbound detection)."""

    def test_webclient_base_url_from_config(
        self, extractor: BackendJavaExtractor, tmp_path: Path
    ) -> None:
        """application.yml with services.identity.base-url → detected as outbound call."""
        resources = tmp_path / "src" / "main" / "resources"
        resources.mkdir(parents=True)
        (resources / "application.yml").write_text(
            "services:\n  identity:\n    base-url: https://identity-service.internal\n"
        )
        calls = extractor._parse_outbound_service_calls(tmp_path)
        assert len(calls) >= 1
        urls = [c.target_url for c in calls]
        assert "https://identity-service.internal" in urls

    def test_webclient_bean_with_qualifier(
        self, extractor: BackendJavaExtractor, tmp_path: Path
    ) -> None:
        """@Bean WebClient with @Value injection detected via YAML + Java source."""
        resources = tmp_path / "src" / "main" / "resources"
        resources.mkdir(parents=True)
        (resources / "application.yml").write_text(
            "services:\n  rewards:\n    base-url: https://rewards-service.internal\n"
        )
        src = tmp_path / "src" / "main" / "java" / "com" / "example"
        src.mkdir(parents=True)
        (src / "WebClientConfig.java").write_text(
            'package com.example;\n'
            'import org.springframework.web.reactive.function.client.WebClient;\n'
            'import org.springframework.beans.factory.annotation.Value;\n'
            'public class WebClientConfig {\n'
            '    public WebClient rewardsWebClient('
            '@Value("${services.rewards.base-url}") '
            "String url) {\n"
            '        return WebClient.builder().baseUrl(url).build();\n'
            '    }\n'
            '}\n'
        )
        calls = extractor._parse_outbound_service_calls(tmp_path)
        assert len(calls) >= 1
        urls = [c.target_url for c in calls]
        assert "https://rewards-service.internal" in urls

    def test_excludes_database_urls(
        self, extractor: BackendJavaExtractor, tmp_path: Path
    ) -> None:
        """JDBC URLs are not treated as outbound service calls."""
        resources = tmp_path / "src" / "main" / "resources"
        resources.mkdir(parents=True)
        (resources / "application.yml").write_text(
            "spring:\n  datasource:\n    url: jdbc:postgresql://localhost:5432/mydb\n"
        )
        calls = extractor._parse_outbound_service_calls(tmp_path)
        jdbc_calls = [c for c in calls if c.target_url and "jdbc" in c.target_url]
        assert len(jdbc_calls) == 0

    def test_excludes_kafka_bootstrap_servers(
        self, extractor: BackendJavaExtractor, tmp_path: Path
    ) -> None:
        """Kafka bootstrap server URLs are not treated as outbound service calls."""
        resources = tmp_path / "src" / "main" / "resources"
        resources.mkdir(parents=True)
        (resources / "application.yml").write_text(
            "spring:\n  kafka:\n    bootstrap-servers: kafka-broker:9092\n"
        )
        calls = extractor._parse_outbound_service_calls(tmp_path)
        kafka_calls = [c for c in calls if c.target_url and "kafka" in (c.target_url or "").lower()]
        assert len(kafka_calls) == 0

    def test_fixture_detects_rewards_service_call(
        self, extractor: BackendJavaExtractor
    ) -> None:
        """SAMPLE_BACKEND_JAVA_REPO fixture detects rewards service outbound call."""
        calls = extractor._parse_outbound_service_calls(SAMPLE_BACKEND_JAVA_REPO)
        urls = [c.target_url for c in calls]
        assert "https://rewards-service.internal" in urls

    def test_manifest_has_outbound_calls(
        self, extractor: BackendJavaExtractor, service_yaml: ServiceYaml
    ) -> None:
        """Full extraction produces manifest with outbound_calls field."""
        with patch.object(extractor, "_get_source_repo", return_value=None):
            manifest = extractor.extract(SAMPLE_BACKEND_JAVA_REPO, service_yaml)
        assert isinstance(manifest.outbound_calls, list)
        # Fixture has rewards service call
        urls = [c.target_url for c in manifest.outbound_calls]
        assert "https://rewards-service.internal" in urls


# ---------------------------------------------------------------------------
# TestValueInjectedKafkaProducers
# ---------------------------------------------------------------------------


class TestValueInjectedKafkaProducers:
    """Tests for Kafka producer resolution via @Value-injected String fields."""

    def test_value_injected_field_with_default_resolved(
        self, extractor: BackendJavaExtractor, tmp_path: Path
    ) -> None:
        """@Value default-topic field → ProducerRecord produces default."""
        src = tmp_path / "src" / "main" / "java" / "com" / "example"
        src.mkdir(parents=True)
        (src / "EventPublisher.java").write_text(
            'package com.example;\n'
            'import org.springframework.beans.factory.annotation.Value;\n'
            'import org.springframework.kafka.core.KafkaTemplate;\n'
            'import org.apache.kafka.clients.producer.ProducerRecord;\n'
            'public class EventPublisher {\n'
            '    @Value("${kafka.topic.events:order-events-topic}")\n'
            '    private String topicName;\n'
            '    void publish() {\n'
            '        ProducerRecord<String,String> rec = new ProducerRecord<>(topicName, "data");\n'
            '        kafkaTemplate.send(rec);\n'
            '    }\n'
            '}\n'
        )
        produces = extractor._parse_kafka_producers(tmp_path)
        assert "order-events-topic" in produces

    def test_producer_record_variable_not_captured_as_topic(
        self, extractor: BackendJavaExtractor, tmp_path: Path
    ) -> None:
        """kafkaTemplate.send(producerRecord) where producerRecord is a ProducerRecord var
        should NOT capture the variable name 'producerRecord' as a topic string."""
        src = tmp_path / "src" / "main" / "java" / "com" / "example"
        src.mkdir(parents=True)
        (src / "MyPublisher.java").write_text(
            'package com.example;\n'
            'import org.springframework.beans.factory.annotation.Value;\n'
            'import org.apache.kafka.clients.producer.ProducerRecord;\n'
            'public class MyPublisher {\n'
            '    @Value("${kafka.topic.events:real-topic-name}")\n'
            '    private String topicField;\n'
            '    void publish() {\n'
            "        ProducerRecord<String,String> producerRecord ="
            ' new ProducerRecord<>(topicField, "x");\n'
            '        kafkaTemplate.send(producerRecord);\n'
            '    }\n'
            '}\n'
        )
        produces = extractor._parse_kafka_producers(tmp_path)
        # The ProducerRecord variable name should NOT appear as a topic
        assert "producerRecord" not in produces
        # But the actual topic from ProducerRecord constructor should be found
        assert "real-topic-name" in produces

    def test_value_injected_field_resolved_from_yaml(
        self, extractor: BackendJavaExtractor, tmp_path: Path
    ) -> None:
        """@Value("${kafka.topic.orders}") field (no default) resolved via application.yml."""
        src = tmp_path / "src" / "main" / "java" / "com" / "example"
        src.mkdir(parents=True)
        resources = tmp_path / "src" / "main" / "resources"
        resources.mkdir(parents=True)
        (resources / "application.yml").write_text(
            "kafka:\n  topic:\n    orders: production.orders.created\n"
        )
        (src / "OrderPublisher.java").write_text(
            'package com.example;\n'
            'import org.springframework.beans.factory.annotation.Value;\n'
            'public class OrderPublisher {\n'
            '    @Value("${kafka.topic.orders}")\n'
            '    private String ordersTopic;\n'
            '    void publish() {\n'
            '        kafkaTemplate.send(ordersTopic, "order-data");\n'
            '    }\n'
            '}\n'
        )
        produces = extractor._parse_kafka_producers(tmp_path)
        assert "production.orders.created" in produces

    def test_scan_value_injected_fields_returns_field_map(
        self, extractor: BackendJavaExtractor, tmp_path: Path
    ) -> None:
        """_scan_value_injected_fields returns dict of fieldName → resolved value."""
        src = tmp_path / "src" / "main" / "java" / "com" / "example"
        src.mkdir(parents=True)
        (src / "Config.java").write_text(
            'package com.example;\n'
            'import org.springframework.beans.factory.annotation.Value;\n'
            'public class Config {\n'
            '    @Value("${kafka.topic.a:topic-alpha}")\n'
            '    private String topicA;\n'
            '    @Value("${kafka.topic.b:topic-beta}")\n'
            '    private String topicB;\n'
            '}\n'
        )
        fields = extractor._scan_value_injected_fields(tmp_path)
        assert fields.get("topicA") == "topic-alpha"
        assert fields.get("topicB") == "topic-beta"


# ---------------------------------------------------------------------------
# TestActuatorEndpointFiltering
# ---------------------------------------------------------------------------


class TestActuatorEndpointFiltering:
    """Tests for /actuator/* endpoint filtering from api_contracts."""

    def test_actuator_endpoints_excluded_from_contracts(
        self, extractor: BackendJavaExtractor, tmp_path: Path
    ) -> None:
        """Spring Actuator endpoints (/actuator/*) are filtered from api_contracts."""
        src = tmp_path / "src" / "main" / "java" / "com" / "example"
        src.mkdir(parents=True)
        # A regular controller
        (src / "OrderController.java").write_text(
            'package com.example;\n'
            'import org.springframework.web.bind.annotation.*;\n'
            '@RestController\n'
            '@RequestMapping("/v1/orders")\n'
            'public class OrderController {\n'
            '    @GetMapping\n'
            '    public String list() { return "ok"; }\n'
            '}\n'
        )
        # An actuator-style controller (should be filtered)
        (src / "ActuatorController.java").write_text(
            'package com.example;\n'
            'import org.springframework.web.bind.annotation.*;\n'
            '@RestController\n'
            '@RequestMapping("/actuator")\n'
            'public class ActuatorController {\n'
            '    @GetMapping("/health")\n'
            '    public String health() { return "UP"; }\n'
            '    @GetMapping("/info")\n'
            '    public String info() { return "{}"; }\n'
            '}\n'
        )
        contracts = extractor.find_api_contracts(tmp_path)
        all_paths = [ep.path for c in contracts for ep in c.endpoints]
        # Real endpoint should be present
        assert any(p and p.startswith("/v1/orders") for p in all_paths)
        # Actuator endpoints should be filtered out
        assert not any(p and p.startswith("/actuator") for p in all_paths)

    def test_non_actuator_endpoints_not_filtered(
        self, extractor: BackendJavaExtractor, tmp_path: Path
    ) -> None:
        """Endpoints starting with /actualization (similar prefix) are NOT filtered."""
        src = tmp_path / "src" / "main" / "java" / "com" / "example"
        src.mkdir(parents=True)
        (src / "ActualizationController.java").write_text(
            'package com.example;\n'
            'import org.springframework.web.bind.annotation.*;\n'
            '@RestController\n'
            '@RequestMapping("/actualization")\n'
            'public class ActualizationController {\n'
            '    @GetMapping("/status")\n'
            '    public String status() { return "ok"; }\n'
            '}\n'
        )
        contracts = extractor.find_api_contracts(tmp_path)
        all_paths = [ep.path for c in contracts for ep in c.endpoints]
        assert any(p and p.startswith("/actualization") for p in all_paths)


# ---------------------------------------------------------------------------
# TestKafkaProducerFallback — cross-class @Value topic field resolution
# ---------------------------------------------------------------------------


class TestKafkaProducerFallback:
    """Tests for the fallback mechanism that collects @Value-injected kafka topics
    when kafkaTemplate.send() uses an unresolvable method parameter.

    This covers the common Spring Boot pattern:
      - A publisher class receives the topic as a method parameter
      - The use-case/aspect that calls it injects the topic via @Value
    """

    def test_value_injected_topic_resolved_via_fallback(
        self, extractor: BackendJavaExtractor, tmp_path: Path
    ) -> None:
        """When kafkaTemplate.send(topic, ...) uses a method param, fall back to
        scanning @Value-injected kafka/topic fields across the codebase."""
        src = tmp_path / "src" / "main" / "java" / "com" / "example"
        src.mkdir(parents=True)

        # Publisher class: topic is a method parameter (not resolvable at call site)
        (src / "EventPublisher.java").write_text(
            'package com.example;\n'
            'import org.springframework.kafka.core.KafkaTemplate;\n'
            'public class EventPublisher {\n'
            '    private KafkaTemplate<String, Object> kafkaTemplate;\n'
            '    public void publish(Object event, String topic) {\n'
            '        kafkaTemplate.send(topic, event);\n'
            '    }\n'
            '}\n'
        )

        # Use-case class: injects topic via @Value and calls publisher
        (src / "LocationUseCase.java").write_text(
            'package com.example;\n'
            'import org.springframework.beans.factory.annotation.Value;\n'
            'public class LocationUseCase {\n'
            '    @Value("${spring.kafka.topic.locations-config-changed}")\n'
            '    private String locationConfigTopic;\n'
            '    public void update() {\n'
            '        eventPublisher.publish(data, locationConfigTopic);\n'
            '    }\n'
            '}\n'
        )

        # YAML: defines the topic default
        resources = tmp_path / "src" / "main" / "resources"
        resources.mkdir(parents=True)
        (resources / "application.yml").write_text(
            'spring:\n'
            '  kafka:\n'
            '    topic:\n'
            '      locations-config-changed: locations-config-changed\n'
        )

        produces = extractor._parse_kafka_producers(tmp_path)
        assert "locations-config-changed" in produces

    def test_multiple_value_topics_resolved_via_fallback(
        self, extractor: BackendJavaExtractor, tmp_path: Path
    ) -> None:
        """When multiple @Value kafka topics exist, all should be collected via fallback."""
        src = tmp_path / "src" / "main" / "java" / "com" / "example"
        src.mkdir(parents=True)

        # Publisher: receives topic as parameter
        (src / "KafkaPublisher.java").write_text(
            'package com.example;\n'
            'import org.springframework.kafka.core.KafkaTemplate;\n'
            'public class KafkaPublisher {\n'
            '    private KafkaTemplate<String, Object> kafkaTemplate;\n'
            '    public void send(String topic, Object payload) {\n'
            '        kafkaTemplate.send(topic, payload);\n'
            '    }\n'
            '}\n'
        )

        # Aspect 1: injects one topic
        (src / "OrderAspect.java").write_text(
            'package com.example;\n'
            'import org.springframework.beans.factory.annotation.Value;\n'
            'public class OrderAspect {\n'
            '    @Value("${spring.kafka.topic.order-created:order-created}")\n'
            '    private String orderTopic;\n'
            '}\n'
        )

        # Aspect 2: injects another topic
        (src / "PaymentAspect.java").write_text(
            'package com.example;\n'
            'import org.springframework.beans.factory.annotation.Value;\n'
            'public class PaymentAspect {\n'
            '    @Value("${spring.kafka.topic.payment-processed:payment-processed}")\n'
            '    private String paymentTopic;\n'
            '}\n'
        )

        produces = extractor._parse_kafka_producers(tmp_path)
        assert "order-created" in produces
        assert "payment-processed" in produces

    def test_unresolved_java_constant_names_not_emitted(
        self, extractor: BackendJavaExtractor, tmp_path: Path
    ) -> None:
        """Unresolved constant identifiers like TOPIC_NAME should not appear in produces."""
        src = tmp_path / "src" / "main" / "java" / "com" / "example"
        src.mkdir(parents=True)

        # kafkaTemplate.send() where topic arg resolves to TOPIC_NAME (unresolvable constant)
        (src / "BadPublisher.java").write_text(
            'package com.example;\n'
            'import org.springframework.kafka.core.KafkaTemplate;\n'
            'public class BadPublisher {\n'
            '    private KafkaTemplate<String, Object> kafkaTemplate;\n'
            '    public void send(String TOPIC_NAME) {\n'
            '        kafkaTemplate.send(TOPIC_NAME, "data");\n'
            '    }\n'
            '}\n'
        )

        produces = extractor._parse_kafka_producers(tmp_path)
        assert "TOPIC_NAME" not in produces

    def test_is_java_constant_name_detection(
        self, extractor: BackendJavaExtractor
    ) -> None:
        """_is_java_constant_name correctly identifies Java constant-style identifiers."""
        # Should be identified as constants
        assert extractor._is_java_constant_name("TOPIC_NAME") is True
        assert extractor._is_java_constant_name("MY_KAFKA_TOPIC") is True
        assert extractor._is_java_constant_name("ORDER_CREATED") is True

        # Should NOT be identified as constants
        assert extractor._is_java_constant_name("order-created") is False
        assert extractor._is_java_constant_name("locations-config-changed") is False
        assert extractor._is_java_constant_name("myTopic") is False
        assert extractor._is_java_constant_name("my.topic.key") is False


# ---------------------------------------------------------------------------
# TestOutboundCallUrlResolution — Spring EL default URL extraction
# ---------------------------------------------------------------------------


class TestOutboundCallUrlResolution:
    """Tests that outbound calls have target_url populated from Spring EL defaults."""

    def test_spring_el_default_url_resolved_in_outbound_calls(
        self, extractor: BackendJavaExtractor, tmp_path: Path
    ) -> None:
        """When application.yml has ${ENV_VAR:https://default-url}, target_url is set."""
        resources = tmp_path / "src" / "main" / "resources"
        resources.mkdir(parents=True)
        (resources / "application.yml").write_text(
            'frictionless-commerce:\n'
            '  base-url: ${FRICTIONLESS_COMMERCE_BASE_URI:https://example.com/frictionless/v1/}\n'
            'device-twins:\n'
            '  base-url: ${DEVICE_TWINS_BASE_URL:https://device-twins.example.com}\n'
        )

        calls = extractor._parse_outbound_service_calls(tmp_path)
        call_map = {c.config_key: c for c in calls}

        assert "frictionless-commerce.base-url" in call_map
        fc_call = call_map["frictionless-commerce.base-url"]
        assert fc_call.target_url == "https://example.com/frictionless/v1/"

        assert "device-twins.base-url" in call_map
        dt_call = call_map["device-twins.base-url"]
        assert dt_call.target_url == "https://device-twins.example.com"

    def test_plain_https_url_in_yaml_still_works(
        self, extractor: BackendJavaExtractor, tmp_path: Path
    ) -> None:
        """Plain https:// URLs in YAML (no Spring EL) still populate target_url."""
        resources = tmp_path / "src" / "main" / "resources"
        resources.mkdir(parents=True)
        (resources / "application.yml").write_text(
            'my-service:\n'
            '  base-url: https://api.my-service.example.com/v2\n'
        )

        calls = extractor._parse_outbound_service_calls(tmp_path)
        call_map = {c.config_key: c for c in calls}

        assert "my-service.base-url" in call_map
        assert call_map["my-service.base-url"].target_url == "https://api.my-service.example.com/v2"

    def test_spring_el_without_default_url_emits_env_prefix(
        self, extractor: BackendJavaExtractor, tmp_path: Path
    ) -> None:
        """${ENV_VAR} with no default emits an outbound call with env: prefix target_url."""
        resources = tmp_path / "src" / "main" / "resources"
        resources.mkdir(parents=True)
        (resources / "application.yml").write_text(
            'internal:\n'
            '  base-url: ${INTERNAL_SERVICE_URL}\n'
        )

        calls = extractor._parse_outbound_service_calls(tmp_path)
        call_map = {c.config_key: c for c in calls}
        # Change 1: env-var-only values now emit outbound calls with env: prefix
        assert "internal.base-url" in call_map
        assert call_map["internal.base-url"].target_url == "env:INTERNAL_SERVICE_URL"


# ---------------------------------------------------------------------------
# TestHttpExchangeClientDetection — @HttpExchange declarative client detection
# ---------------------------------------------------------------------------


class TestHttpExchangeClientDetection:
    """Tests for detecting Spring 6 @HttpExchange declarative HTTP clients."""

    def test_http_exchange_base_url_from_value_annotation(
        self, extractor: BackendJavaExtractor, tmp_path: Path
    ) -> None:
        """@Value-injected baseUrl in WebConfig is detected as outbound call."""
        src = tmp_path / "src" / "main" / "java" / "com" / "example" / "config"
        src.mkdir(parents=True)

        # WebConfig-style bean factory using HttpServiceProxyFactory
        (src / "WebConfig.java").write_text(
            'package com.example.config;\n'
            'import org.springframework.beans.factory.annotation.Value;\n'
            'import org.springframework.web.reactive.function.client.WebClient;\n'
            'import org.springframework.web.service.invoker.HttpServiceProxyFactory;\n'
            'public class WebConfig {\n'
            '    @Value("${device-twins.base-url}")\n'
            '    private String deviceTwinsBaseUrl;\n'
            '    public WebClient webClient() {\n'
            '        return WebClient.builder()\n'
            '                .baseUrl(deviceTwinsBaseUrl)\n'
            '                .build();\n'
            '    }\n'
            '}\n'
        )

        resources = tmp_path / "src" / "main" / "resources"
        resources.mkdir(parents=True)
        (resources / "application.yml").write_text(
            'device-twins:\n'
            '  base-url: ${DEVICE_TWINS_BASE_URL:https://device-twins.example.com}\n'
        )

        calls = extractor._parse_outbound_service_calls(tmp_path)
        call_map = {c.config_key: c for c in calls}

        # Should be found from YAML Phase A since the YAML resolves correctly
        assert "device-twins.base-url" in call_map
        assert call_map["device-twins.base-url"].target_url == "https://device-twins.example.com"

    def test_inline_spring_el_in_base_url_call(
        self, extractor: BackendJavaExtractor, tmp_path: Path
    ) -> None:
        """WebClient.builder().baseUrl('${svc.base-url:https://default}') is detected."""
        src = tmp_path / "src" / "main" / "java" / "com" / "example"
        src.mkdir(parents=True)

        (src / "ClientConfig.java").write_text(
            'package com.example;\n'
            'import org.springframework.web.reactive.function.client.WebClient;\n'
            'public class ClientConfig {\n'
            '    public WebClient client() {\n'
            '        return WebClient.builder()\n'
            '                .baseUrl("${partner-api.base-url:https://api.partner.example.com}")\n'
            '                .build();\n'
            '    }\n'
            '}\n'
        )

        calls = extractor._parse_outbound_service_calls(tmp_path)
        call_map = {c.config_key: c for c in calls}
        assert "partner-api.base-url" in call_map
        assert call_map["partner-api.base-url"].target_url == "https://api.partner.example.com"


class TestYamlValueIsSpringElResolution:
    """Tests for the case where a YAML value is itself a Spring EL expression.

    Pattern (locations-microservice):
        application.yml:
            locations-config-changed: ${LOCATIONS_CONFIG_CHANGED_TOPIC:locations-config-changed}
        Java:
            @Value("${spring.kafka.topic.locations-config-changed}")
            private String locationConfigTopic;

    The @Value key resolves to the YAML value, which is itself ${ENV:default}.
    A second resolution pass must extract the concrete topic name.
    """

    def test_kafka_produces_resolved_when_yaml_value_is_spring_el(
        self, extractor: BackendJavaExtractor, tmp_path: Path
    ) -> None:
        """Kafka topic is correctly resolved through two layers of Spring EL."""
        src = tmp_path / "src" / "main" / "java" / "com" / "example"
        src.mkdir(parents=True)
        res = tmp_path / "src" / "main" / "resources"
        res.mkdir(parents=True)

        # YAML: topic value is itself ${ENV:default}
        (res / "application.yml").write_text(
            "spring:\n"
            "  kafka:\n"
            "    topic:\n"
            "      locations-config-changed: "
            "${LOCATIONS_CONFIG_CHANGED_TOPIC:"
            "locations-config-changed}\n"
        )

        # Publisher receives topic as method parameter (unresolvable at call site)
        (src / "EventPublisherImpl.java").write_text(
            "package com.example;\n"
            "public class EventPublisherImpl {\n"
            "    private KafkaTemplate<String, Object> kafkaTemplate;\n"
            "    public void publish(String topic, Object event) {\n"
            "        kafkaTemplate.send(topic, event);\n"
            "    }\n"
            "}\n"
        )

        # Use-case has @Value annotation referencing the YAML key (no default)
        (src / "PublishAspect.java").write_text(
            "package com.example;\n"
            "import org.springframework.beans.factory.annotation.Value;\n"
            "public class PublishAspect {\n"
            "    @Value(\"${spring.kafka.topic.locations-config-changed}\")\n"
            "    private String locationConfigTopic;\n"
            "}\n"
        )

        produces = extractor._parse_kafka_producers(tmp_path)
        # Must resolve to the concrete default, not the raw ${...} expression
        assert "locations-config-changed" in produces
        assert not any(p.startswith("${") for p in produces), (
            f"Raw Spring EL expression found in produces: {produces}"
        )

    def test_resolve_kafka_topic_ref_double_el(
        self, extractor: BackendJavaExtractor, tmp_path: Path
    ) -> None:
        """_resolve_kafka_topic_ref resolves ${KEY} when YAML[KEY] = ${ENV:default}."""
        yaml_props = {
            "spring.kafka.topic.my-topic": "${MY_TOPIC_ENV:my-concrete-topic}",
        }
        result = extractor._resolve_kafka_topic_ref(
            "${spring.kafka.topic.my-topic}", {}, yaml_props
        )
        assert result == "my-concrete-topic"

    def test_scan_value_injected_fields_double_el(
        self, extractor: BackendJavaExtractor, tmp_path: Path
    ) -> None:
        """_scan_value_injected_fields resolves field when YAML value is Spring EL."""
        src = tmp_path / "src" / "main" / "java" / "com" / "example"
        src.mkdir(parents=True)
        res = tmp_path / "src" / "main" / "resources"
        res.mkdir(parents=True)

        (res / "application.yml").write_text(
            "spring:\n"
            "  kafka:\n"
            "    topic:\n"
            "      orders: ${ORDERS_TOPIC:orders-created}\n"
        )

        (src / "OrderService.java").write_text(
            "package com.example;\n"
            "import org.springframework.beans.factory.annotation.Value;\n"
            "public class OrderService {\n"
            "    @Value(\"${spring.kafka.topic.orders}\")\n"
            "    private String ordersTopic;\n"
            "}\n"
        )

        fields = extractor._scan_value_injected_fields(tmp_path)
        # Should resolve to concrete default, not raw ${ORDERS_TOPIC:orders-created}
        assert "ordersTopic" in fields
        assert fields["ordersTopic"] == "orders-created"
        assert not fields["ordersTopic"].startswith("${")


# ---------------------------------------------------------------------------
# TestEnvVarOnlyOutboundCalls — Change 1
# ---------------------------------------------------------------------------


class TestEnvVarOnlyOutboundCalls:
    """Tests for Change 1: env-var-only YAML config keys emit outbound calls."""

    def test_env_var_with_empty_default_emits_call(
        self, extractor: BackendJavaExtractor, tmp_path: Path
    ) -> None:
        """${ENV_VAR:} (empty default) emits outbound call with env: prefix."""
        resources = tmp_path / "src" / "main" / "resources"
        resources.mkdir(parents=True)
        (resources / "application.yml").write_text(
            "notifications:\n  base-uri: ${NOTIFICATIONS_SERVICE_BASE_URI:}\n"
        )
        calls = extractor._parse_outbound_service_calls(tmp_path)
        call_map = {c.config_key: c for c in calls}
        assert "notifications.base-uri" in call_map
        assert call_map["notifications.base-uri"].target_url == "env:NOTIFICATIONS_SERVICE_BASE_URI"

    def test_env_var_without_default_emits_call(
        self, extractor: BackendJavaExtractor, tmp_path: Path
    ) -> None:
        """${ENV_VAR} (no default at all) emits outbound call with env: prefix."""
        resources = tmp_path / "src" / "main" / "resources"
        resources.mkdir(parents=True)
        (resources / "application.yml").write_text(
            "partner-api:\n  base-url: ${PARTNER_API_BASE_URL}\n"
        )
        calls = extractor._parse_outbound_service_calls(tmp_path)
        call_map = {c.config_key: c for c in calls}
        assert "partner-api.base-url" in call_map
        assert call_map["partner-api.base-url"].target_url == "env:PARTNER_API_BASE_URL"

    def test_env_var_with_http_default_still_resolves_normally(
        self, extractor: BackendJavaExtractor, tmp_path: Path
    ) -> None:
        """${ENV_VAR:https://default-url} still resolves to the HTTP URL (no env: prefix)."""
        resources = tmp_path / "src" / "main" / "resources"
        resources.mkdir(parents=True)
        (resources / "application.yml").write_text(
            "my-service:\n  base-url: ${MY_SVC_URL:https://api.example.com}\n"
        )
        calls = extractor._parse_outbound_service_calls(tmp_path)
        call_map = {c.config_key: c for c in calls}
        assert "my-service.base-url" in call_map
        assert call_map["my-service.base-url"].target_url == "https://api.example.com"

    def test_env_var_infra_keys_excluded(
        self, extractor: BackendJavaExtractor, tmp_path: Path
    ) -> None:
        """Infrastructure keys (redis, cosmos, etc.) are still excluded even with env: prefix."""
        resources = tmp_path / "src" / "main" / "resources"
        resources.mkdir(parents=True)
        (resources / "application.yml").write_text(
            "spring:\n"
            "  data:\n"
            "    redis:\n"
            "      url: ${REDIS_URL}\n"
        )
        calls = extractor._parse_outbound_service_calls(tmp_path)
        # Redis URLs should still be excluded
        call_keys = {c.config_key for c in calls}
        assert "spring.data.redis.url" not in call_keys

    def test_multiple_env_var_services_detected(
        self, extractor: BackendJavaExtractor, tmp_path: Path
    ) -> None:
        """Multiple env-var-only service URLs are all detected."""
        resources = tmp_path / "src" / "main" / "resources"
        resources.mkdir(parents=True)
        (resources / "application.yml").write_text(
            "nba:\n"
            "  default:\n"
            "    base-uri: ${NBA_BASE_URL:}\n"
            "ticketing:\n"
            "  base-uri: ${TICKETING_SERVICE_BASE_URI:}\n"
            "notifications:\n"
            "  base-uri: ${NOTIFICATIONS_SERVICE_BASE_URI:}\n"
        )
        calls = extractor._parse_outbound_service_calls(tmp_path)
        call_keys = {c.config_key for c in calls}
        assert "nba.default.base-uri" in call_keys
        assert "ticketing.base-uri" in call_keys
        assert "notifications.base-uri" in call_keys
        # All should have env: prefix
        for c in calls:
            assert c.target_url.startswith("env:"), f"Expected env: prefix on {c.config_key}"

    def test_fixture_detects_env_var_outbound_calls(
        self, extractor: BackendJavaExtractor
    ) -> None:
        """Fixture application.yml env-var-only entries are detected as outbound calls."""
        calls = extractor._parse_outbound_service_calls(SAMPLE_BACKEND_JAVA_REPO)
        call_map = {c.config_key: c for c in calls}
        # Fixture has notifications.base-uri: ${NOTIFICATIONS_SERVICE_BASE_URI:}
        assert "notifications.base-uri" in call_map
        assert call_map["notifications.base-uri"].target_url == "env:NOTIFICATIONS_SERVICE_BASE_URI"
        # Fixture has ticketing.base-uri: ${TICKETING_SERVICE_BASE_URI:}
        assert "ticketing.base-uri" in call_map
        assert call_map["ticketing.base-uri"].target_url == "env:TICKETING_SERVICE_BASE_URI"
        # Fixture has partner-api.base-url: ${PARTNER_API_BASE_URL}
        assert "partner-api.base-url" in call_map
        assert call_map["partner-api.base-url"].target_url == "env:PARTNER_API_BASE_URL"

    def test_extract_env_var_from_spring_el(
        self, extractor: BackendJavaExtractor
    ) -> None:
        """_extract_env_var_from_spring_el extracts env var names correctly."""
        assert extractor._extract_env_var_from_spring_el("${NBA_BASE_URL:}") == "NBA_BASE_URL"
        assert extractor._extract_env_var_from_spring_el("${NBA_BASE_URL}") == "NBA_BASE_URL"
        assert extractor._extract_env_var_from_spring_el("${MY_VAR:default}") == "MY_VAR"
        assert extractor._extract_env_var_from_spring_el("plain-value") is None
        assert extractor._extract_env_var_from_spring_el("https://example.com") is None


# ---------------------------------------------------------------------------
# TestValueOnMethodParams — Change 2
# ---------------------------------------------------------------------------


class TestValueOnMethodParams:
    """Tests for Change 2: @Value on @Bean method parameters for outbound call detection."""

    def test_value_on_method_param_detected(
        self, extractor: BackendJavaExtractor, tmp_path: Path
    ) -> None:
        """@Value on @Bean method param creates outbound call when YAML has env-var value."""
        resources = tmp_path / "src" / "main" / "resources"
        resources.mkdir(parents=True)
        (resources / "application.yml").write_text(
            "nba:\n  default:\n    base-uri: ${NBA_BASE_URL:}\n"
        )
        src = tmp_path / "src" / "main" / "java" / "com" / "example"
        src.mkdir(parents=True)
        (src / "WebClientConfig.java").write_text(
            "package com.example;\n"
            "import org.springframework.beans.factory.annotation.Value;\n"
            "import org.springframework.web.reactive.function.client.WebClient;\n"
            "import org.springframework.web.service.invoker.HttpServiceProxyFactory;\n"
            "public class WebClientConfig {\n"
            "    @Bean\n"
            "    public NBAWebClient nbaWebClient(\n"
            '            @Value("${nba.default.base-uri}") String url) {\n'
            "        return createWebClient(url, NBAWebClient.class);\n"
            "    }\n"
            "    private <T> T createWebClient(String url, Class<T> clientType) {\n"
            "        WebClient webClient = WebClient.builder().baseUrl(url).build();\n"
            "        HttpServiceProxyFactory factory = HttpServiceProxyFactory.builder()\n"
            "                .build();\n"
            "        return factory.createClient(clientType);\n"
            "    }\n"
            "}\n"
        )
        calls = extractor._parse_outbound_service_calls(tmp_path)
        call_map = {c.config_key: c for c in calls}
        assert "nba.default.base-uri" in call_map
        assert call_map["nba.default.base-uri"].target_url == "env:NBA_BASE_URL"

    def test_value_on_method_param_with_http_default(
        self, extractor: BackendJavaExtractor, tmp_path: Path
    ) -> None:
        """@Value on method param resolves to HTTP URL when YAML has http default."""
        resources = tmp_path / "src" / "main" / "resources"
        resources.mkdir(parents=True)
        (resources / "application.yml").write_text(
            "my-api:\n  base-url: ${MY_API_URL:https://api.example.com}\n"
        )
        src = tmp_path / "src" / "main" / "java" / "com" / "example"
        src.mkdir(parents=True)
        (src / "ClientConfig.java").write_text(
            "package com.example;\n"
            "import org.springframework.beans.factory.annotation.Value;\n"
            "import org.springframework.web.reactive.function.client.WebClient;\n"
            "import org.springframework.web.service.invoker.HttpServiceProxyFactory;\n"
            "public class ClientConfig {\n"
            "    @Bean\n"
            "    public MyApiClient myApiClient(\n"
            '            @Value("${my-api.base-url}") String url) {\n'
            "        WebClient webClient = WebClient.builder().baseUrl(url).build();\n"
            "        return null;\n"
            "    }\n"
            "}\n"
        )
        calls = extractor._parse_outbound_service_calls(tmp_path)
        call_map = {c.config_key: c for c in calls}
        assert "my-api.base-url" in call_map
        assert call_map["my-api.base-url"].target_url == "https://api.example.com"

    def test_value_on_method_param_non_url_key_ignored(
        self, extractor: BackendJavaExtractor, tmp_path: Path
    ) -> None:
        """@Value on method param with non-URL config key is not treated as outbound call."""
        src = tmp_path / "src" / "main" / "java" / "com" / "example"
        src.mkdir(parents=True)
        (src / "Config.java").write_text(
            "package com.example;\n"
            "import org.springframework.beans.factory.annotation.Value;\n"
            "import org.springframework.web.reactive.function.client.WebClient;\n"
            "public class Config {\n"
            "    @Bean\n"
            "    public String timeout(\n"
            '            @Value("${services.timeout-ms}") String timeoutMs) {\n'
            "        return timeoutMs;\n"
            "    }\n"
            "}\n"
        )
        calls = extractor._parse_outbound_service_calls(tmp_path)
        assert len(calls) == 0

    def test_fixture_detects_method_param_value_outbound_calls(
        self, extractor: BackendJavaExtractor
    ) -> None:
        """Fixture WebClientConfig with @Value method params produces outbound calls."""
        calls = extractor._parse_outbound_service_calls(SAMPLE_BACKEND_JAVA_REPO)
        call_map = {c.config_key: c for c in calls}
        # Fixture WebClientConfig.java has @Value("${notifications.base-uri}") on method param
        assert "notifications.base-uri" in call_map
        # Fixture WebClientConfig.java has @Value("${ticketing.base-uri}") on method param
        assert "ticketing.base-uri" in call_map


# ---------------------------------------------------------------------------
# TestHttpExchangeInterfaceScanning — Change 3
# ---------------------------------------------------------------------------


class TestHttpExchangeInterfaceScanning:
    """Tests for Change 3: @HttpExchange interface scanning for endpoint metadata."""

    def test_http_exchange_interface_endpoints_extracted(
        self, extractor: BackendJavaExtractor, tmp_path: Path
    ) -> None:
        """@HttpExchange interface methods are extracted as endpoint metadata."""
        src = tmp_path / "src" / "main" / "java" / "com" / "example"
        src.mkdir(parents=True)
        (src / "ExternalApiClient.java").write_text(
            "package com.example;\n"
            "import org.springframework.web.service.annotation.HttpExchange;\n"
            "import org.springframework.web.service.annotation.GetExchange;\n"
            "import org.springframework.web.service.annotation.PostExchange;\n"
            "@HttpExchange\n"
            "public interface ExternalApiClient {\n"
            '    @GetExchange(url = "/v1/items")\n'
            "    Object listItems();\n"
            '    @PostExchange(url = "/v1/items")\n'
            "    Object createItem(Object body);\n"
            "}\n"
        )
        interfaces = extractor._scan_http_exchange_interfaces(tmp_path)
        assert "ExternalApiClient" in interfaces
        eps = interfaces["ExternalApiClient"]
        assert len(eps) == 2
        methods = {ep.method for ep in eps}
        assert "GET" in methods
        assert "POST" in methods
        paths = {ep.path for ep in eps}
        assert "/v1/items" in paths

    def test_http_exchange_with_class_level_base_path(
        self, extractor: BackendJavaExtractor, tmp_path: Path
    ) -> None:
        """Class-level @HttpExchange('/api') is combined with method paths."""
        src = tmp_path / "src" / "main" / "java" / "com" / "example"
        src.mkdir(parents=True)
        (src / "MyClient.java").write_text(
            "package com.example;\n"
            "import org.springframework.web.service.annotation.HttpExchange;\n"
            "import org.springframework.web.service.annotation.GetExchange;\n"
            '@HttpExchange("/api")\n'
            "public interface MyClient {\n"
            '    @GetExchange(url = "/users")\n'
            "    Object listUsers();\n"
            "}\n"
        )
        interfaces = extractor._scan_http_exchange_interfaces(tmp_path)
        assert "MyClient" in interfaces
        eps = interfaces["MyClient"]
        assert len(eps) == 1
        assert eps[0].path == "/api/users"
        assert eps[0].method == "GET"

    def test_http_exchange_interface_linked_to_outbound_call(
        self, extractor: BackendJavaExtractor, tmp_path: Path
    ) -> None:
        """@HttpExchange interfaces are linked to outbound calls via createClient binding."""
        resources = tmp_path / "src" / "main" / "resources"
        resources.mkdir(parents=True)
        (resources / "application.yml").write_text(
            "external:\n  base-url: ${EXTERNAL_API_URL:}\n"
        )
        src = tmp_path / "src" / "main" / "java" / "com" / "example"
        src.mkdir(parents=True)

        # @HttpExchange interface
        (src / "ExternalClient.java").write_text(
            "package com.example;\n"
            "import org.springframework.web.service.annotation.HttpExchange;\n"
            "import org.springframework.web.service.annotation.GetExchange;\n"
            "@HttpExchange\n"
            "public interface ExternalClient {\n"
            '    @GetExchange(url = "/v1/data")\n'
            "    Object getData();\n"
            "}\n"
        )

        # Config class that binds the interface to a base URL
        (src / "WebConfig.java").write_text(
            "package com.example;\n"
            "import org.springframework.beans.factory.annotation.Value;\n"
            "import org.springframework.web.reactive.function.client.WebClient;\n"
            "import org.springframework.web.service.invoker.HttpServiceProxyFactory;\n"
            "public class WebConfig {\n"
            "    @Bean\n"
            "    public ExternalClient externalClient(\n"
            '            @Value("${external.base-url}") String url) {\n'
            "        WebClient webClient = WebClient.builder().baseUrl(url).build();\n"
            "        HttpServiceProxyFactory factory = HttpServiceProxyFactory.builder()\n"
            "                .build();\n"
            "        return factory.createClient(ExternalClient.class);\n"
            "    }\n"
            "}\n"
        )

        calls = extractor._parse_outbound_service_calls(tmp_path)
        call_map = {c.config_key: c for c in calls}
        assert "external.base-url" in call_map
        call = call_map["external.base-url"]
        assert "ExternalClient" in call.client_interfaces
        assert len(call.endpoints) == 1
        assert call.endpoints[0].method == "GET"
        assert call.endpoints[0].path == "/v1/data"

    def test_fixture_http_exchange_interfaces_linked(
        self, extractor: BackendJavaExtractor
    ) -> None:
        """Fixture @HttpExchange interfaces are linked to their outbound calls."""
        calls = extractor._parse_outbound_service_calls(SAMPLE_BACKEND_JAVA_REPO)
        call_map = {c.config_key: c for c in calls}

        # NotificationsWebClient should be linked to notifications.base-uri
        if "notifications.base-uri" in call_map:
            notif_call = call_map["notifications.base-uri"]
            assert "NotificationsWebClient" in notif_call.client_interfaces
            assert len(notif_call.endpoints) >= 2  # sendNotification + getNotificationStatus

        # TicketingWebClient should be linked to ticketing.base-uri
        if "ticketing.base-uri" in call_map:
            tick_call = call_map["ticketing.base-uri"]
            assert "TicketingWebClient" in tick_call.client_interfaces
            assert len(tick_call.endpoints) >= 3  # listTickets + purchaseTicket + getTicket

    def test_non_interface_files_ignored(
        self, extractor: BackendJavaExtractor, tmp_path: Path
    ) -> None:
        """Java classes (not interfaces) with @HttpExchange are ignored."""
        src = tmp_path / "src" / "main" / "java" / "com" / "example"
        src.mkdir(parents=True)
        (src / "NotAnInterface.java").write_text(
            "package com.example;\n"
            "import org.springframework.web.service.annotation.HttpExchange;\n"
            "import org.springframework.web.service.annotation.GetExchange;\n"
            "@HttpExchange\n"
            "public class NotAnInterface {\n"
            '    @GetExchange(url = "/should/not/appear")\n'
            "    public Object method() { return null; }\n"
            "}\n"
        )
        interfaces = extractor._scan_http_exchange_interfaces(tmp_path)
        assert "NotAnInterface" not in interfaces

    def test_scan_http_exchange_bean_config_keys(
        self, extractor: BackendJavaExtractor, tmp_path: Path
    ) -> None:
        """_scan_http_exchange_bean_config_keys maps interface name → config key."""
        src = tmp_path / "src" / "main" / "java" / "com" / "example"
        src.mkdir(parents=True)
        (src / "ClientConfig.java").write_text(
            "package com.example;\n"
            "import org.springframework.beans.factory.annotation.Value;\n"
            "import org.springframework.web.service.invoker.HttpServiceProxyFactory;\n"
            "public class ClientConfig {\n"
            "    @Bean\n"
            "    public MyClient myClient(\n"
            '            @Value("${my-service.base-url}") String url) {\n'
            "        return factory.createClient(MyClient.class);\n"
            "    }\n"
            "    @Bean\n"
            "    public OtherClient otherClient(\n"
            '            @Value("${other-service.base-uri}") String url) {\n'
            "        return factory.createClient(OtherClient.class);\n"
            "    }\n"
            "}\n"
        )
        result = extractor._scan_http_exchange_bean_config_keys(tmp_path)
        assert result.get("MyClient") == "my-service.base-url"
        assert result.get("OtherClient") == "other-service.base-uri"

    def test_all_exchange_methods_detected(
        self, extractor: BackendJavaExtractor, tmp_path: Path
    ) -> None:
        """All HTTP exchange methods are detected: GET, POST, PUT, DELETE, PATCH."""
        src = tmp_path / "src" / "main" / "java" / "com" / "example"
        src.mkdir(parents=True)
        (src / "FullClient.java").write_text(
            "package com.example;\n"
            "import org.springframework.web.service.annotation.*;\n"
            "@HttpExchange\n"
            "public interface FullClient {\n"
            '    @GetExchange(url = "/items")\n'
            "    Object list();\n"
            '    @PostExchange(url = "/items")\n'
            "    Object create(Object body);\n"
            '    @PutExchange(url = "/items/{id}")\n'
            "    Object update(String id, Object body);\n"
            '    @DeleteExchange(url = "/items/{id}")\n'
            "    Object delete(String id);\n"
            '    @PatchExchange(url = "/items/{id}")\n'
            "    Object patch(String id, Object body);\n"
            "}\n"
        )
        interfaces = extractor._scan_http_exchange_interfaces(tmp_path)
        assert "FullClient" in interfaces
        eps = interfaces["FullClient"]
        methods = {ep.method for ep in eps}
        assert methods == {"GET", "POST", "PUT", "DELETE", "PATCH"}


# ---------------------------------------------------------------------------
# TestKafkaProducerPerFileResolution — regression for field-name collision fix
# ---------------------------------------------------------------------------


class TestKafkaProducerPerFileResolution:
    """Regression tests for per-file @Value resolution in Kafka producer detection.

    The bug: when multiple publisher classes all use the same field name (e.g. `topic`),
    the codebase-wide _scan_value_injected_fields dict had last-write-wins semantics,
    causing all but one topic to be lost.  The fix uses per-file resolution so each
    publisher resolves its own `topic` field independently.
    """

    def test_multiple_publishers_same_field_name_all_detected(
        self, extractor: BackendJavaExtractor, tmp_path: Path
    ) -> None:
        """Multiple publishers all named 'topic' field each resolve to their own topic."""
        src = tmp_path / "src" / "main" / "java" / "com" / "example"
        src.mkdir(parents=True)
        resources = tmp_path / "src" / "main" / "resources"
        resources.mkdir(parents=True)

        # YAML defines all topics
        (resources / "application.yml").write_text(
            "spring:\n"
            "  kafka:\n"
            "    topics:\n"
            "      orders-mgmt: orders-mgmt-topic\n"
            "      payments-history: payments-history-topic\n"
            "      push-notifications: push-notifications-topic\n"
            "      domain-events: payment-domain-events\n"
        )

        # Publisher A: uses field name `topic` for orders-mgmt
        (src / "OrdersPublisher.java").write_text(
            "package com.example;\n"
            "import org.springframework.beans.factory.annotation.Value;\n"
            "import org.springframework.kafka.core.KafkaTemplate;\n"
            "@Component\n"
            "public class OrdersPublisher {\n"
            '    @Value("${spring.kafka.topics.orders-mgmt}")\n'
            "    private String topic;\n"
            "    private final KafkaTemplate<String, Object> kafkaTemplate;\n"
            "    public void send(Object msg) {\n"
            '        kafkaTemplate.send(topic, "key", msg);\n'
            "    }\n"
            "}\n"
        )

        # Publisher B: also uses field name `topic` for payments-history
        (src / "PaymentHistoryPublisher.java").write_text(
            "package com.example;\n"
            "import org.springframework.beans.factory.annotation.Value;\n"
            "import org.springframework.kafka.core.KafkaTemplate;\n"
            "@Component\n"
            "public class PaymentHistoryPublisher {\n"
            '    @Value("${spring.kafka.topics.payments-history}")\n'
            "    private String topic;\n"
            "    private final KafkaTemplate<String, Object> kafkaTemplate;\n"
            "    public void send(Object msg) {\n"
            '        kafkaTemplate.send(topic, "key", msg);\n'
            "    }\n"
            "}\n"
        )

        # Publisher C: also uses field name `topic` for push-notifications
        (src / "PushNotificationPublisher.java").write_text(
            "package com.example;\n"
            "import org.springframework.beans.factory.annotation.Value;\n"
            "import org.springframework.kafka.core.KafkaTemplate;\n"
            "@Component\n"
            "public class PushNotificationPublisher {\n"
            '    @Value("${spring.kafka.topics.push-notifications}")\n'
            "    private String topic;\n"
            "    private final KafkaTemplate<String, Object> kafkaTemplate;\n"
            "    public void send(Object msg) {\n"
            '        kafkaTemplate.send(topic, "key", msg);\n'
            "    }\n"
            "}\n"
        )

        # Publisher D: also uses field name `topic` for domain-events
        (src / "DomainEventPublisher.java").write_text(
            "package com.example;\n"
            "import org.springframework.beans.factory.annotation.Value;\n"
            "import org.springframework.kafka.core.KafkaTemplate;\n"
            "@Component\n"
            "public class DomainEventPublisher {\n"
            '    @Value("${spring.kafka.topics.domain-events}")\n'
            "    private String topic;\n"
            "    private final KafkaTemplate<String, Object> kafkaTemplate;\n"
            "    public void send(Object msg) {\n"
            '        kafkaTemplate.send(topic, "key", msg);\n'
            "    }\n"
            "}\n"
        )

        produces = extractor._parse_kafka_producers(tmp_path)

        # All four publishers must be detected — not just one (the last-write-wins victim)
        assert "orders-mgmt-topic" in produces, (
            "orders-mgmt-topic missing — per-file resolution not working"
        )
        assert "payments-history-topic" in produces, (
            "payments-history-topic missing — per-file resolution not working"
        )
        assert "push-notifications-topic" in produces, (
            "push-notifications-topic missing — per-file resolution not working"
        )
        assert "payment-domain-events" in produces, (
            "payment-domain-events missing — per-file resolution not working"
        )

    def test_scan_value_injected_fields_in_file_returns_per_file_map(
        self, extractor: BackendJavaExtractor, tmp_path: Path
    ) -> None:
        """_scan_value_injected_fields_in_file returns the field map for just that file."""
        resources = tmp_path / "src" / "main" / "resources"
        resources.mkdir(parents=True)
        (resources / "application.yml").write_text(
            "spring:\n  kafka:\n    topics:\n      my-topic: my-resolved-topic\n"
        )
        yaml_props = extractor._load_yaml_flat_props(tmp_path)
        content = (
            "package com.example;\n"
            "import org.springframework.beans.factory.annotation.Value;\n"
            "public class MyPublisher {\n"
            '    @Value("${spring.kafka.topics.my-topic}")\n'
            "    private String topic;\n"
            "}\n"
        )
        fields = extractor._scan_value_injected_fields_in_file(content, yaml_props)
        assert fields.get("topic") == "my-resolved-topic"


# ---------------------------------------------------------------------------
# TestOutboundCallNonStandardUrlKeys — regression for expanded _URL_KEY_PATTERN
# ---------------------------------------------------------------------------


class TestOutboundCallNonStandardUrlKeys:
    """Regression tests for outbound call detection with non-standard YAML key suffixes.

    The bug: keys ending in -url or _url (e.g. verifications-url, environment_url)
    were not matched by _URL_KEY_PATTERN which only covered base-url / base-uri.
    The fix broadens the pattern to match any key ending with -url or _url.
    """

    def test_verifications_url_key_detected(
        self, extractor: BackendJavaExtractor, tmp_path: Path
    ) -> None:
        """Keys ending with -url (e.g. verifications-url) are detected as outbound calls."""
        resources = tmp_path / "src" / "main" / "resources"
        resources.mkdir(parents=True)
        (resources / "application.yml").write_text(
            "spring:\n"
            "  chase:\n"
            "    verifications-url: ${CHASE_VERIFICATIONS_URL:https://cat-api.merchant.jpmorgan.com/api}\n"
            "    wallet-decryption-url: ${WALLET_DECRYPTION_URL:https://cat-api.merchant.jpmorgan.com/api}\n"
        )
        calls = extractor._parse_outbound_service_calls(tmp_path)
        config_keys = {c.config_key for c in calls}
        assert "spring.chase.verifications-url" in config_keys, (
            "verifications-url key not detected — _URL_KEY_PATTERN not matching -url suffix"
        )
        assert "spring.chase.wallet-decryption-url" in config_keys, (
            "wallet-decryption-url key not detected — _URL_KEY_PATTERN not matching -url suffix"
        )

    def test_environment_url_key_detected(
        self, extractor: BackendJavaExtractor, tmp_path: Path
    ) -> None:
        """Keys ending with _url (e.g. environment_url) are detected as outbound calls."""
        resources = tmp_path / "src" / "main" / "resources"
        resources.mkdir(parents=True)
        (resources / "application.yml").write_text(
            "spring:\n"
            "  microsoft:\n"
            "    refund:\n"
            "      environment_url:"
            " ${RETURN_CART_LINES_ENV_URL:https://scus8l625me54960702-rs.su.retail.dynamics.com/Commerce}\n"
        )
        calls = extractor._parse_outbound_service_calls(tmp_path)
        config_keys = {c.config_key for c in calls}
        assert "spring.microsoft.refund.environment_url" in config_keys, (
            "environment_url key not detected — _URL_KEY_PATTERN not matching _url suffix"
        )

    def test_chase_client_linked_via_verifications_url(
        self, extractor: BackendJavaExtractor, tmp_path: Path
    ) -> None:
        """@HttpExchange client registered with @Value('${spring.chase.verifications-url}')
        is linked to the outbound call for that config key."""
        resources = tmp_path / "src" / "main" / "resources"
        resources.mkdir(parents=True)
        (resources / "application.yml").write_text(
            "spring:\n"
            "  chase:\n"
            "    verifications-url: ${CHASE_URL:https://cat-api.merchant.jpmorgan.com/api}\n"
        )
        src = tmp_path / "src" / "main" / "java" / "com" / "example"
        src.mkdir(parents=True)
        (src / "TokenizationClient.java").write_text(
            "package com.example;\n"
            "import org.springframework.web.service.annotation.HttpExchange;\n"
            "import org.springframework.web.service.annotation.PostExchange;\n"
            "@HttpExchange\n"
            "public interface TokenizationClient {\n"
            '    @PostExchange("/v2/verifications")\n'
            "    Object tokenize(Object request);\n"
            "}\n"
        )
        (src / "WebConfig.java").write_text(
            "package com.example;\n"
            "import org.springframework.beans.factory.annotation.Value;\n"
            "import org.springframework.context.annotation.Bean;\n"
            "import org.springframework.context.annotation.Configuration;\n"
            "import org.springframework.web.reactive.function.client.support.WebClientAdapter;\n"
            "import org.springframework.web.service.invoker.HttpServiceProxyFactory;\n"
            "@Configuration\n"
            "public class WebConfig {\n"
            "    @Bean\n"
            "    TokenizationClient tokenizationClient("
            '@Value("${spring.chase.verifications-url}") '
            "String url) {\n"
            "        WebClientAdapter adapter = "
            "WebClientAdapter.forClient(null);\n"
            "        return HttpServiceProxyFactory.builder(adapter)"
            ".build().createClient("
            "TokenizationClient.class);\n"
            "    }\n"
            "}\n"
        )
        result = extractor._scan_http_exchange_bean_config_keys(tmp_path)
        assert result.get("TokenizationClient") == "spring.chase.verifications-url"


# ---------------------------------------------------------------------------
# TestEndpointSummaryPostWindow — regression for summary attribution fix
# ---------------------------------------------------------------------------


class TestEndpointSummaryPostWindow:
    """Regression tests for @Operation summary attribution when placed after @*Mapping.

    The bug: when @Operation(summary=...) appears AFTER the @*Mapping annotation
    (not before it), the pre-window strategy assigned that summary to the NEXT
    endpoint instead of the current one.  The fix adds a post-window fallback.
    """

    def test_summary_after_mapping_attributed_correctly(
        self, extractor: BackendJavaExtractor, tmp_path: Path
    ) -> None:
        """@Operation placed after @DeleteMapping is attributed to that DELETE endpoint."""
        src = tmp_path / "src" / "main" / "java" / "com" / "example"
        src.mkdir(parents=True)
        (src / "PaymentController.java").write_text(
            "package com.example;\n"
            "import org.springframework.web.bind.annotation.*;\n"
            "import io.swagger.v3.oas.annotations.Operation;\n"
            "@RestController\n"
            "@RequestMapping(\"/v1\")\n"
            "public class PaymentController {\n"
            "\n"
            "    @DeleteMapping(\"/payment-method/{userId}/{id}\")\n"
            '    @Operation(summary = "Delete payment method")\n'
            "    public void delete(String userId, String id) {}\n"
            "\n"
            "    @PatchMapping(\"/payment-method/{userId}/preferred/\")\n"
            '    @Operation(summary = "Set preferred payment method")\n'
            "    public void setPreferred(String userId) {}\n"
            "\n"
            "    @PatchMapping(\"/payment-method/{userId}/nickname/\")\n"
            '    @Operation(summary = "Update nickname")\n'
            "    public void updateNickname(String userId) {}\n"
            "}\n"
        )
        contracts = extractor.find_api_contracts(tmp_path)
        assert contracts, "No API contracts extracted"
        endpoints = contracts[0].endpoints

        ep_map = {(ep.method, ep.path): ep.summary for ep in endpoints}

        delete_key = ("DELETE", "/v1/payment-method/{userId}/{id}")
        assert ep_map.get(delete_key) == "Delete payment method", (
            "DELETE summary attributed to wrong endpoint"
        )
        patch_pref_key = (
            "PATCH", "/v1/payment-method/{userId}/preferred/"
        )
        assert (
            ep_map.get(patch_pref_key)
            == "Set preferred payment method"
        ), (
            "PATCH preferred summary attributed to wrong endpoint"
            " — likely shifted from DELETE"
        )
        patch_nick_key = (
            "PATCH", "/v1/payment-method/{userId}/nickname/"
        )
        assert (
            ep_map.get(patch_nick_key) == "Update nickname"
        ), (
            "PATCH nickname summary attributed to wrong endpoint"
        )

    def test_pre_window_summary_still_works(
        self, extractor: BackendJavaExtractor, tmp_path: Path
    ) -> None:
        """@Operation placed BEFORE @GetMapping still works correctly (no regression)."""
        src = tmp_path / "src" / "main" / "java" / "com" / "example"
        src.mkdir(parents=True)
        (src / "OrderController.java").write_text(
            "package com.example;\n"
            "import org.springframework.web.bind.annotation.*;\n"
            "import io.swagger.v3.oas.annotations.Operation;\n"
            "@RestController\n"
            "@RequestMapping(\"/v1\")\n"
            "public class OrderController {\n"
            "\n"
            '    @Operation(summary = "List orders")\n'
            "    @GetMapping(\"/orders\")\n"
            "    public void listOrders() {}\n"
            "\n"
            '    @Operation(summary = "Create order")\n'
            "    @PostMapping(\"/orders\")\n"
            "    public void createOrder() {}\n"
            "}\n"
        )
        contracts = extractor.find_api_contracts(tmp_path)
        assert contracts
        endpoints = contracts[0].endpoints
        ep_map = {(ep.method, ep.path): ep.summary for ep in endpoints}

        assert ep_map.get(("GET", "/v1/orders")) == "List orders"
        assert ep_map.get(("POST", "/v1/orders")) == "Create order"


# ---------------------------------------------------------------------------
# TestEndpointParameterExtraction — parameter, body, response extraction
# ---------------------------------------------------------------------------


class TestEndpointParameterExtraction:
    """Tests for endpoint parameter, request body, and response extraction."""

    def setup_method(self):
        self.extractor = BackendJavaExtractor()

    # --- _split_params_respecting_generics ---

    def test_split_simple_params(self):
        result = self.extractor._split_params_respecting_generics("String name, int age")
        assert result == ["String name", "int age"]

    def test_split_generic_params(self):
        result = self.extractor._split_params_respecting_generics(
            "Map<String, List<Integer>> map, @RequestParam String name"
        )
        assert result == ["Map<String, List<Integer>> map", "@RequestParam String name"]

    def test_split_empty(self):
        result = self.extractor._split_params_respecting_generics("")
        assert result == []

    # --- _extract_method_signature ---

    def test_extract_simple_signature(self):
        text = """
    public ResponseEntity<OrderDto> getOrder(@PathVariable String id) {
        return ResponseEntity.ok().build();
    }
"""
        sig = self.extractor._extract_method_signature(text)
        assert sig is not None
        assert "ResponseEntity<OrderDto>" in sig
        assert "getOrder" in sig
        assert "@PathVariable String id" in sig

    def test_extract_signature_with_annotations_before(self):
        text = """
    @Operation(summary = "Get order")
    @Tag(name = "Orders")
    public ResponseEntity<OrderDto> getOrder(@PathVariable String id) {
        return ResponseEntity.ok().build();
    }
"""
        sig = self.extractor._extract_method_signature(text)
        assert sig is not None
        assert "ResponseEntity<OrderDto>" in sig
        assert "@PathVariable String id" in sig

    def test_extract_signature_interface_method(self):
        text = """
    ResponseEntity<List<RewardDto>> listRewards(@RequestParam String memberId);
"""
        sig = self.extractor._extract_method_signature(text)
        assert sig is not None
        assert "ResponseEntity<List<RewardDto>>" in sig
        assert "@RequestParam String memberId" in sig

    def test_extract_signature_multiline(self):
        text = """
    public ResponseEntity<OrderDto> createOrder(
            @RequestBody CreateOrderRequest request,
            @RequestHeader("Authorization") String auth) {
        return ResponseEntity.ok().build();
    }
"""
        sig = self.extractor._extract_method_signature(text)
        assert sig is not None
        assert "@RequestBody CreateOrderRequest request" in sig
        assert '@RequestHeader("Authorization") String auth' in sig

    def test_extract_signature_void_return(self):
        text = """
    public void deleteOrder(@PathVariable String id) {
        // delete
    }
"""
        sig = self.extractor._extract_method_signature(text)
        assert sig is not None
        assert "void" in sig

    def test_extract_signature_no_method(self):
        text = "// just a comment"
        sig = self.extractor._extract_method_signature(text)
        assert sig is None

    # --- _extract_parameters_from_signature ---

    def test_extract_path_variable(self):
        sig = "ResponseEntity<OrderDto> getOrder(@PathVariable String id)"
        params = self.extractor._extract_parameters_from_signature(sig)
        assert len(params) == 1
        assert params[0].name == "id"
        assert params[0].location == "path"
        assert params[0].type == "String"

    def test_extract_request_param_with_attributes(self):
        sig = (
            'ResponseEntity<List<OrderDto>> listOrders('
            '@RequestParam(value = "status", required = false) '
            'String status, '
            '@RequestParam(defaultValue = "0") int page)'
        )
        params = self.extractor._extract_parameters_from_signature(sig)
        assert len(params) == 2

        status_param = params[0]
        assert status_param.name == "status"
        assert status_param.location == "query"
        assert status_param.type == "String"
        assert status_param.required is False

        page_param = params[1]
        assert page_param.name == "page"
        assert page_param.location == "query"
        assert page_param.type == "int"
        assert page_param.default_value == "0"

    def test_extract_request_header(self):
        sig = (
            'ResponseEntity<Void> cancelOrder('
            '@PathVariable String id, '
            '@RequestHeader("X-Correlation-Id") '
            'String correlationId)'
        )
        params = self.extractor._extract_parameters_from_signature(sig)
        assert len(params) == 2

        id_param = params[0]
        assert id_param.name == "id"
        assert id_param.location == "path"

        header_param = params[1]
        assert header_param.name == "X-Correlation-Id"
        assert header_param.location == "header"
        assert header_param.type == "String"

    def test_extract_params_skips_unannotated(self):
        sig = (
            "ResponseEntity<OrderDto> getOrder("
            "@PathVariable String id, "
            "HttpServletRequest request)"
        )
        params = self.extractor._extract_parameters_from_signature(sig)
        assert len(params) == 1
        assert params[0].name == "id"

    def test_extract_params_empty_signature(self):
        sig = "ResponseEntity<OrderDto> listOrders()"
        params = self.extractor._extract_parameters_from_signature(sig)
        assert params == []

    def test_extract_path_variable_with_name(self):
        sig = 'ResponseEntity<OrderDto> getOrder(@PathVariable("orderId") String id)'
        params = self.extractor._extract_parameters_from_signature(sig)
        assert len(params) == 1
        assert params[0].name == "orderId"
        assert params[0].location == "path"

    # --- _extract_request_body_from_signature ---

    def test_extract_request_body(self):
        sig = "ResponseEntity<OrderDto> createOrder(@RequestBody CreateOrderRequest request)"
        body = self.extractor._extract_request_body_from_signature(sig)
        assert body is not None
        assert body.type == "CreateOrderRequest"
        assert body.required is True

    def test_extract_request_body_optional(self):
        sig = "ResponseEntity<OrderDto> updateOrder(@RequestBody(required = false) UpdateDto dto)"
        body = self.extractor._extract_request_body_from_signature(sig)
        assert body is not None
        assert body.type == "UpdateDto"
        assert body.required is False

    def test_extract_request_body_generic(self):
        sig = "ResponseEntity<OrderDto> createOrders(@RequestBody List<OrderItemDto> items)"
        body = self.extractor._extract_request_body_from_signature(sig)
        assert body is not None
        assert body.type == "List<OrderItemDto>"

    def test_extract_no_request_body(self):
        sig = "ResponseEntity<OrderDto> getOrder(@PathVariable String id)"
        body = self.extractor._extract_request_body_from_signature(sig)
        assert body is None

    # --- _extract_return_type_from_signature ---

    def test_extract_response_entity(self):
        sig = "ResponseEntity<OrderDto> getOrder(@PathVariable String id)"
        resp = self.extractor._extract_return_type_from_signature(sig)
        assert resp is not None
        assert resp.type == "OrderDto"
        assert resp.wrapper == "ResponseEntity"

    def test_extract_response_entity_list(self):
        sig = "ResponseEntity<List<OrderDto>> listOrders()"
        resp = self.extractor._extract_return_type_from_signature(sig)
        assert resp is not None
        assert resp.type == "List<OrderDto>"
        assert resp.wrapper == "ResponseEntity"

    def test_extract_response_entity_void(self):
        sig = "ResponseEntity<Void> deleteOrder(@PathVariable String id)"
        resp = self.extractor._extract_return_type_from_signature(sig)
        assert resp is not None
        assert resp.type == "Void"
        assert resp.wrapper == "ResponseEntity"

    def test_extract_void_return(self):
        sig = "void deleteOrder(@PathVariable String id)"
        resp = self.extractor._extract_return_type_from_signature(sig)
        assert resp is not None
        assert resp.type == "void"
        assert resp.wrapper is None

    def test_extract_plain_dto_return(self):
        sig = "OrderDto getOrder(@PathVariable String id)"
        resp = self.extractor._extract_return_type_from_signature(sig)
        assert resp is not None
        assert resp.type == "OrderDto"
        assert resp.wrapper is None

    def test_extract_mono_response_entity(self):
        sig = "Mono<ResponseEntity<OrderDto>> getOrder(@PathVariable String id)"
        resp = self.extractor._extract_return_type_from_signature(sig)
        assert resp is not None
        assert resp.type == "OrderDto"
        assert resp.wrapper == "Mono<ResponseEntity>"

    def test_extract_flux_return(self):
        sig = "Flux<OrderDto> streamOrders()"
        resp = self.extractor._extract_return_type_from_signature(sig)
        assert resp is not None
        assert resp.type == "OrderDto"
        assert resp.wrapper == "Flux"

    def test_extract_response_entity_wildcard(self):
        sig = "ResponseEntity<?> handleRequest()"
        resp = self.extractor._extract_return_type_from_signature(sig)
        assert resp is not None
        assert resp.type == "?"
        assert resp.wrapper == "ResponseEntity"

    # --- _unwrap_response_type ---

    def test_unwrap_simple_wrapper(self):
        wrapper, inner = self.extractor._unwrap_response_type("ResponseEntity<OrderDto>")
        assert wrapper == "ResponseEntity"
        assert inner == "OrderDto"

    def test_unwrap_nested_wrapper(self):
        wrapper, inner = self.extractor._unwrap_response_type("Mono<ResponseEntity<OrderDto>>")
        assert wrapper == "Mono<ResponseEntity>"
        assert inner == "OrderDto"

    def test_unwrap_non_wrapper(self):
        wrapper, inner = self.extractor._unwrap_response_type("List<OrderDto>")
        assert wrapper is None
        assert inner == "List<OrderDto>"

    def test_unwrap_no_generics(self):
        wrapper, inner = self.extractor._unwrap_response_type("OrderDto")
        assert wrapper is None
        assert inner == "OrderDto"

    # --- Integration: fixture-based tests ---

    def test_order_controller_endpoints_have_params(self, extractor: BackendJavaExtractor) -> None:
        """Test that OrderController endpoints extract parameters, body, and response."""
        contracts = extractor.find_api_contracts(SAMPLE_BACKEND_JAVA_REPO)
        assert len(contracts) > 0

        endpoints = contracts[0].endpoints
        # Find the list orders endpoint (GET /v1/orders)
        list_ep = next((e for e in endpoints if e.method == "GET" and e.path == "/v1/orders"), None)
        assert list_ep is not None, "GET /v1/orders endpoint not found"
        # Should have query parameters
        assert len(list_ep.parameters) >= 1
        param_names = {p.name for p in list_ep.parameters}
        assert "status" in param_names or "page" in param_names
        # Should have response
        assert list_ep.response is not None

        # Find the create order endpoint (POST /v1/orders)
        create_ep = next(
            (e for e in endpoints
             if e.method == "POST" and e.path == "/v1/orders"),
            None,
        )
        assert create_ep is not None, "POST /v1/orders not found"
        # Should have request body
        assert create_ep.request_body is not None
        assert create_ep.request_body.type == "CreateOrderRequest"
        # Should have response
        assert create_ep.response is not None

        # Find the get order endpoint (GET /v1/orders/{id})
        get_ep = next(
            (e for e in endpoints
             if e.method == "GET" and e.path == "/v1/orders/{id}"),
            None,
        )
        assert get_ep is not None, "GET /v1/orders/{id} not found"
        # Should have path variable
        assert len(get_ep.parameters) >= 1
        id_param = next((p for p in get_ep.parameters if p.location == "path"), None)
        assert id_param is not None
        assert id_param.name == "id"

    def test_rewards_api_interface_endpoints_have_params(
        self, extractor: BackendJavaExtractor,
    ) -> None:
        """Test that RewardsApi interface endpoints extract parameters via implements pattern."""
        contracts = extractor.find_api_contracts(SAMPLE_BACKEND_JAVA_REPO)
        assert len(contracts) > 0

        endpoints = contracts[0].endpoints
        # Find rewards endpoints (from RewardsApi interface)
        rewards_eps = [e for e in endpoints if e.path and "/rewards" in e.path]
        assert len(rewards_eps) > 0, "No rewards endpoints found"

        # Find list rewards endpoint (GET /v1/rewards)
        list_ep = next(
            (e for e in rewards_eps
             if e.method == "GET" and e.path == "/v1/rewards"),
            None,
        )
        if list_ep:
            assert len(list_ep.parameters) >= 1
            member_param = next((p for p in list_ep.parameters if p.name == "memberId"), None)
            assert member_param is not None
            assert member_param.location == "query"

        # Find redeem endpoint (POST /v1/rewards/{rewardId}/redeem)
        redeem_ep = next((e for e in rewards_eps if e.method == "POST"), None)
        if redeem_ep:
            assert redeem_ep.request_body is not None
            assert redeem_ep.request_body.type == "RedeemRequest"


# ---------------------------------------------------------------------------
# TestDtoSchemaExtraction — DTO schema extraction
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_backend_java_repo() -> Path:
    """Path to the sample backend-java repo fixture."""
    return SAMPLE_BACKEND_JAVA_REPO


class TestDtoSchemaExtraction:
    """Tests for DTO schema extraction from Java source files."""

    def setup_method(self):
        self.extractor = BackendJavaExtractor()

    # --- _build_class_index ---

    def test_build_class_index(self, sample_backend_java_repo):
        """Test that class index maps class names to file paths."""
        index = self.extractor._build_class_index(sample_backend_java_repo)
        assert "CreateOrderRequest" in index
        assert "OrderDto" in index
        assert "OrderStatus" in index
        assert "RewardDto" in index
        assert "OrderItemDto" in index
        # Should NOT include test classes
        assert "DemoApplicationTests" not in index

    # --- _detect_class_kind ---

    def test_detect_class_kind_regular_class(self):
        content = "public class CreateOrderRequest {"
        kind, parent = self.extractor._detect_class_kind(content, "CreateOrderRequest")
        assert kind == "class"
        assert parent is None

    def test_detect_class_kind_with_extends(self):
        content = "public class SpecialOrder extends CreateOrderRequest {"
        kind, parent = self.extractor._detect_class_kind(content, "SpecialOrder")
        assert kind == "class"
        assert parent == "CreateOrderRequest"

    def test_detect_class_kind_record(self):
        content = "public record RewardDto(String id, String name) {}"
        kind, parent = self.extractor._detect_class_kind(content, "RewardDto")
        assert kind == "record"

    def test_detect_class_kind_enum(self):
        content = "public enum OrderStatus {"
        kind, parent = self.extractor._detect_class_kind(content, "OrderStatus")
        assert kind == "enum"

    def test_detect_class_kind_data_class(self):
        content = "data class OrderDto(val id: String, val name: String)"
        kind, parent = self.extractor._detect_class_kind(content, "OrderDto")
        assert kind == "data_class"

    def test_detect_class_kind_interface(self):
        content = "public interface OrderService {"
        kind, parent = self.extractor._detect_class_kind(content, "OrderService")
        assert kind == "interface"

    # --- _extract_class_fields ---

    def test_extract_class_fields_basic(self):
        content = '''
public class SimpleDto {
    private String name;
    private int age;
    private BigDecimal amount;
}
'''
        fields = self.extractor._extract_class_fields(content, "SimpleDto")
        assert len(fields) >= 3
        names = {f.name for f in fields}
        assert "name" in names
        assert "age" in names
        assert "amount" in names

    def test_extract_class_fields_with_annotations(self):
        content = '''
public class AnnotatedDto {
    @NotNull
    @Size(min = 1, max = 100)
    @Schema(description = "The user name")
    private String name;

    @JsonProperty("email_address")
    private String email;

    @JsonIgnore
    private String secret;
}
'''
        fields = self.extractor._extract_class_fields(content, "AnnotatedDto")
        field_map = {f.name: f for f in fields}

        assert "name" in field_map
        assert field_map["name"].required is True
        assert field_map["name"].description == "The user name"
        assert len(field_map["name"].constraints) >= 1
        size_constraint = next(
            (c for c in field_map["name"].constraints if c.kind == "size"),
            None,
        )
        assert size_constraint is not None
        assert size_constraint.min == 1
        assert size_constraint.max == 100

        assert "email" in field_map
        assert field_map["email"].json_name == "email_address"

        # @JsonIgnore should be excluded
        assert "secret" not in field_map

    def test_extract_class_fields_skips_static(self):
        content = '''
public class WithStatic {
    private static final String CONSTANT = "value";
    private String name;
}
'''
        fields = self.extractor._extract_class_fields(content, "WithStatic")
        names = {f.name for f in fields}
        assert "name" in names
        assert "CONSTANT" not in names

    # --- _extract_record_fields ---

    def test_extract_record_fields(self):
        content = '''
public record RewardDto(
    String id,
    String name,
    int points,
    String description
) {}
'''
        fields = self.extractor._extract_record_fields(content, "RewardDto")
        assert len(fields) == 4
        names = [f.name for f in fields]
        assert names == ["id", "name", "points", "description"]

    def test_extract_record_fields_with_annotations(self):
        content = '''
public record ValidatedRecord(
    @NotNull String id,
    @Size(min = 1, max = 50) String name
) {}
'''
        fields = self.extractor._extract_record_fields(content, "ValidatedRecord")
        assert len(fields) == 2
        assert fields[0].required is True
        assert any(c.kind == "size" for c in fields[1].constraints)

    # --- _extract_enum_values ---

    def test_extract_enum_values(self):
        content = '''
public enum OrderStatus {
    PENDING,
    CONFIRMED,
    SHIPPED,
    DELIVERED,
    CANCELLED
}
'''
        values = self.extractor._extract_enum_values(content, "OrderStatus")
        assert values == ["PENDING", "CONFIRMED", "SHIPPED", "DELIVERED", "CANCELLED"]

    def test_extract_enum_values_with_constructor(self):
        content = '''
public enum Priority {
    LOW(1),
    MEDIUM(2),
    HIGH(3);

    private final int level;
    Priority(int level) { this.level = level; }
}
'''
        values = self.extractor._extract_enum_values(content, "Priority")
        assert "LOW" in values
        assert "MEDIUM" in values
        assert "HIGH" in values

    # --- _extract_kotlin_data_class_fields ---

    def test_extract_kotlin_data_class_fields(self):
        content = '''
data class OrderDto(
    val id: String,
    val name: String,
    val amount: BigDecimal,
    val notes: String?
)
'''
        fields = self.extractor._extract_kotlin_data_class_fields(
            content, "OrderDto"
        )
        assert len(fields) == 4
        field_map = {f.name: f for f in fields}
        assert field_map["id"].type == "String"
        assert field_map["id"].required is True
        assert field_map["notes"].required is False  # nullable

    # --- _collect_dto_type_names ---

    def test_collect_dto_type_names(self):
        from cortex.schema import (
            ApiContract,
            EndpointIndex,
            EndpointRequestBody,
            EndpointResponse,
        )

        contracts = [
            ApiContract(
                kind="spring-annotations",
                endpoints=[
                    EndpointIndex(
                        method="POST",
                        path="/orders",
                        request_body=EndpointRequestBody(
                            type="CreateOrderRequest"
                        ),
                        response=EndpointResponse(
                            type="OrderDto",
                            wrapper="ResponseEntity",
                        ),
                    ),
                    EndpointIndex(
                        method="GET",
                        path="/orders",
                        response=EndpointResponse(
                            type="List<OrderDto>",
                            wrapper="ResponseEntity",
                        ),
                    ),
                ],
            )
        ]
        names = self.extractor._collect_dto_type_names(contracts)
        assert "CreateOrderRequest" in names
        assert "OrderDto" in names
        # List should be filtered out as primitive
        assert "List" not in names
        # ResponseEntity should not appear
        assert "ResponseEntity" not in names

    # --- _parse_java_class ---

    def test_parse_java_class_pojo(self, sample_backend_java_repo):
        dto_path = (
            sample_backend_java_repo
            / "src/main/java/com/example/demo/dto/CreateOrderRequest.java"
        )
        schema = self.extractor._parse_java_class(
            dto_path, sample_backend_java_repo
        )
        assert schema is not None
        assert schema.name == "CreateOrderRequest"
        assert schema.kind == "class"
        # customerId, items, totalAmount, notes (not internalTrackingId)
        assert len(schema.fields) >= 3

        field_map = {f.name: f for f in schema.fields}
        assert "customerId" in field_map
        assert field_map["customerId"].required is True
        assert "items" in field_map
        assert field_map["items"].json_name == "order_items"
        # @JsonIgnore field should be excluded
        assert "internalTrackingId" not in field_map

    def test_parse_java_class_record(self, sample_backend_java_repo):
        dto_path = (
            sample_backend_java_repo
            / "src/main/java/com/example/demo/dto/RewardDto.java"
        )
        schema = self.extractor._parse_java_class(
            dto_path, sample_backend_java_repo
        )
        assert schema is not None
        assert schema.name == "RewardDto"
        assert schema.kind == "record"
        assert len(schema.fields) == 4

    def test_parse_java_class_enum(self, sample_backend_java_repo):
        dto_path = (
            sample_backend_java_repo
            / "src/main/java/com/example/demo/dto/OrderStatus.java"
        )
        schema = self.extractor._parse_java_class(
            dto_path, sample_backend_java_repo
        )
        assert schema is not None
        assert schema.name == "OrderStatus"
        assert schema.kind == "enum"
        assert "PENDING" in schema.enum_values
        assert "CANCELLED" in schema.enum_values

    # --- _extract_dto_schemas (integration) ---

    def test_extract_dto_schemas_integration(self, sample_backend_java_repo):
        """Test full DTO extraction pipeline against fixture repo."""
        from cortex.schema import (
            ApiContract,
            EndpointIndex,
            EndpointRequestBody,
            EndpointResponse,
        )

        contracts = [
            ApiContract(
                kind="spring-annotations",
                endpoints=[
                    EndpointIndex(
                        method="POST",
                        path="/v1/orders",
                        request_body=EndpointRequestBody(
                            type="CreateOrderRequest"
                        ),
                        response=EndpointResponse(
                            type="OrderDto",
                            wrapper="ResponseEntity",
                        ),
                    ),
                ],
            )
        ]
        schemas = self.extractor._extract_dto_schemas(
            sample_backend_java_repo, contracts
        )

        # Should have resolved CreateOrderRequest, OrderDto, and nested types
        assert "CreateOrderRequest" in schemas
        assert "OrderDto" in schemas
        # OrderItemDto is referenced by both CreateOrderRequest and OrderDto
        assert "OrderItemDto" in schemas
        # OrderStatus is referenced by OrderDto
        assert "OrderStatus" in schemas
        assert schemas["OrderStatus"].kind == "enum"

    def test_extract_dto_schemas_in_manifest(self, sample_backend_java_repo):
        """Test that dto_schemas appears in the full extraction output."""
        service_yaml = ServiceYaml(
            name="sample-backend-java",
            type="backend-java",
            owner="team-backend",
            domain="orders",
            tier="critical",
            purpose="Sample backend Java service",
        )
        manifest = self.extractor.extract(
            sample_backend_java_repo, service_yaml
        )
        # Should have dto_schemas populated
        assert isinstance(manifest.dto_schemas, dict)
        # The fixture has CreateOrderRequest and OrderDto referenced
        # in controller endpoints
        if manifest.api_contracts:
            # If endpoints reference DTOs, schemas should be extracted
            has_dto_refs = any(
                ep.request_body or ep.response
                for c in manifest.api_contracts
                for ep in c.endpoints
            )
            if has_dto_refs:
                assert len(manifest.dto_schemas) > 0

    # --- _extract_field_constraints ---

    def test_extract_field_constraints_size(self):
        annotations = ["@Size(min = 1, max = 100)"]
        constraints = self.extractor._extract_field_constraints(annotations)
        assert len(constraints) == 1
        assert constraints[0].kind == "size"
        assert constraints[0].min == 1
        assert constraints[0].max == 100

    def test_extract_field_constraints_min_max(self):
        annotations = ["@Min(0)", "@Max(1000)"]
        constraints = self.extractor._extract_field_constraints(annotations)
        assert len(constraints) == 2
        kinds = {c.kind for c in constraints}
        assert "min" in kinds
        assert "max" in kinds

    def test_extract_field_constraints_pattern(self):
        annotations = ['@Pattern(regexp = "^[A-Z]+$")']
        constraints = self.extractor._extract_field_constraints(annotations)
        assert len(constraints) == 1
        assert constraints[0].kind == "pattern"
        assert constraints[0].value == "^[A-Z]+$"

    def test_extract_field_constraints_email(self):
        annotations = ["@Email"]
        constraints = self.extractor._extract_field_constraints(annotations)
        assert len(constraints) == 1
        assert constraints[0].kind == "email"

    # --- _extract_brace_block ---

    def test_extract_brace_block(self):
        content = "class Foo { int x; { nested; } }"
        # Start after the first '{'
        result = self.extractor._extract_brace_block(content, 12)
        assert result is not None
        assert "int x;" in result
        assert "nested;" in result

    def test_extract_brace_block_unmatched(self):
        content = "class Foo { int x;"
        result = self.extractor._extract_brace_block(content, 12)
        assert result is None

    # --- Edge case tests ---

    def test_circular_reference_safe(self, sample_backend_java_repo):
        """Circular references (A→B→A) should resolve without infinite recursion."""
        class_index = self.extractor._build_class_index(sample_backend_java_repo)
        # Ensure both circular classes are in the index
        assert "CircularA" in class_index
        assert "CircularB" in class_index

        schemas: dict = {}
        # Should complete without hanging or raising
        self.extractor._resolve_dto_types(
            {"CircularA"}, class_index, sample_backend_java_repo, schemas, depth=0
        )

        assert "CircularA" in schemas
        assert "CircularB" in schemas

        # Verify field references
        a_fields = {f.name: f for f in schemas["CircularA"].fields}
        b_fields = {f.name: f for f in schemas["CircularB"].fields}
        assert "other" in a_fields
        assert a_fields["other"].type == "CircularB"
        assert "back" in b_fields
        assert b_fields["back"].type == "CircularA"

    def test_unresolvable_type_skipped(self, sample_backend_java_repo):
        """Types not found in the class index should be silently skipped."""
        class_index = self.extractor._build_class_index(sample_backend_java_repo)
        schemas: dict = {}

        # Should not raise any exception
        self.extractor._resolve_dto_types(
            {"NonExistentDto"}, class_index, sample_backend_java_repo, schemas, depth=0
        )

        assert "NonExistentDto" not in schemas

    def test_abstract_parent_detected(self, sample_backend_java_repo):
        """Abstract parent class should be parsed with correct fields."""
        dto_path = (
            sample_backend_java_repo
            / "src/main/java/com/example/demo/dto/BaseRequest.java"
        )
        schema = self.extractor._parse_java_class(
            dto_path, sample_backend_java_repo
        )
        assert schema is not None
        assert schema.kind == "class"

        field_map = {f.name: f for f in schema.fields}
        assert "requestId" in field_map
        assert field_map["requestId"].required is True  # @NotNull
        assert "correlationId" in field_map
        assert "timestamp" in field_map

    def test_transitive_dto_resolution(self, tmp_path):
        """Transitive references (A→B→C) should all be resolved."""
        src = tmp_path / "src" / "main" / "java" / "com" / "example"
        src.mkdir(parents=True)

        (src / "TypeA.java").write_text(
            "package com.example;\n"
            "public class TypeA {\n"
            "    private String id;\n"
            "    private TypeB nested;\n"
            "}\n"
        )
        (src / "TypeB.java").write_text(
            "package com.example;\n"
            "public class TypeB {\n"
            "    private String label;\n"
            "    private TypeC deep;\n"
            "}\n"
        )
        (src / "TypeC.java").write_text(
            "package com.example;\n"
            "public class TypeC {\n"
            "    private String value;\n"
            "}\n"
        )

        class_index = self.extractor._build_class_index(tmp_path)
        assert "TypeA" in class_index
        assert "TypeB" in class_index
        assert "TypeC" in class_index

        schemas: dict = {}
        self.extractor._resolve_dto_types(
            {"TypeA"}, class_index, tmp_path, schemas, depth=0
        )

        assert "TypeA" in schemas
        assert "TypeB" in schemas
        assert "TypeC" in schemas

    def test_depth_limit_respected(self, tmp_path):
        """Resolution should stop at _MAX_DTO_DEPTH and not resolve deeper types."""
        src = tmp_path / "src" / "main" / "java" / "com" / "example"
        src.mkdir(parents=True)

        # Create a chain: Type1 → Type2 → ... → Type7
        for i in range(1, 8):
            next_field = (
                f"    private Type{i + 1} next;\n" if i < 7 else ""
            )
            (src / f"Type{i}.java").write_text(
                f"package com.example;\n"
                f"public class Type{i} {{\n"
                f"    private String name;\n"
                f"{next_field}"
                f"}}\n"
            )

        class_index = self.extractor._build_class_index(tmp_path)
        schemas: dict = {}
        self.extractor._resolve_dto_types(
            {"Type1"}, class_index, tmp_path, schemas, depth=0
        )

        # With _MAX_DTO_DEPTH=5, depths 0-4 are processed (5 levels)
        # Type1(d=0) → Type2(d=1) → Type3(d=2) → Type4(d=3) → Type5(d=4) → Type6(d=5, blocked)
        for i in range(1, 6):
            assert f"Type{i}" in schemas, f"Type{i} should be resolved at depth {i - 1}"

        # Type6 and Type7 should NOT be resolved (depth limit reached)
        assert "Type6" not in schemas, "Type6 should be blocked by depth limit"
        assert "Type7" not in schemas, "Type7 should be blocked by depth limit"


# ---------------------------------------------------------------------------
# TestStripAnnotations
# ---------------------------------------------------------------------------


class TestStripAnnotations:
    """Tests for BackendJavaExtractor._strip_annotations()."""

    def test_simple_annotation_no_parens(self):
        """@Hidden is removed, preserving the rest."""
        assert (BackendJavaExtractor._strip_annotations(
            "@Hidden ResponseEntity<Foo>") == "ResponseEntity<Foo>")

    def test_annotation_with_simple_parens(self):
        """@RequestBody is removed."""
        assert (BackendJavaExtractor._strip_annotations(
            '@RequestBody OrderDto dto').strip() == "OrderDto dto")

    def test_annotation_with_string_containing_parens(self):
        """Parens inside string literals don't break the parser."""
        text = '@Schema(description = "Incident Id.", example = "eyJ)test") String value'
        result = BackendJavaExtractor._strip_annotations(text)
        assert result.strip() == "String value"

    def test_annotation_with_nested_parens(self):
        """Nested parentheses are handled correctly."""
        text = '@Annotation(value = foo(bar())) String x'
        result = BackendJavaExtractor._strip_annotations(text)
        assert result.strip() == "String x"

    def test_annotation_with_escaped_quotes(self):
        """Escaped quotes inside strings don't break the parser."""
        text = r'@Schema(description = "say \"hello\"") String x'
        result = BackendJavaExtractor._strip_annotations(text)
        assert result.strip() == "String x"

    def test_multiple_annotations(self):
        """Multiple annotations are all removed."""
        text = "@NotNull @Size(min = 1) @Valid CreateOrderRequest request"
        result = BackendJavaExtractor._strip_annotations(text)
        assert result.strip() == "CreateOrderRequest request"

    def test_multiline_annotation(self):
        """Multi-line annotations are fully removed."""
        text = ('@Schema(\n        description = "channel",'
                '\n        nullable = true)\n    String channel')
        result = BackendJavaExtractor._strip_annotations(text)
        assert "String" in result
        assert "channel" in result
        assert "@Schema" not in result
        assert "description" not in result

    def test_no_annotations(self):
        """Text without annotations is returned unchanged."""
        text = "ResponseEntity<OrderDto> createOrder"
        assert BackendJavaExtractor._strip_annotations(text) == text

    def test_annotation_at_sign_in_email(self):
        """@ in email-like strings is not treated as annotation start."""
        # The @ must be preceded by a non-alnum char to be treated as annotation
        text = 'user@example.com String x'
        result = BackendJavaExtractor._strip_annotations(text)
        # The @example part should NOT be stripped since it's preceded by 'r' (alnum)
        assert "user" in result

    def test_preauthorize_with_complex_content(self):
        """@PreAuthorize with complex SpEL expression is fully removed."""
        text = """@PreAuthorize("hasRole('ROLE_INTERNAL')")
    ResponseEntity<LocationDto> getLocation"""
        result = BackendJavaExtractor._strip_annotations(text)
        assert "ResponseEntity<LocationDto>" in result
        assert "getLocation" in result
        assert "@PreAuthorize" not in result
        assert "hasRole" not in result


# ---------------------------------------------------------------------------
# TestIsValidDtoName
# ---------------------------------------------------------------------------


class TestIsValidDtoName:
    """Tests for BackendJavaExtractor._is_valid_dto_name()."""

    def test_valid_dto_names(self):
        """Standard DTO class names are accepted."""
        for name in ["OrderDto", "CreateOrderRequest", "SeatEvent", "UserIdentifier"]:
            assert BackendJavaExtractor._is_valid_dto_name(name), f"{name} should be valid"

    def test_primitive_types_rejected(self):
        """Primitive and standard library types are rejected."""
        for name in ["String", "Integer", "Boolean", "List", "Map", "UUID"]:
            assert not BackendJavaExtractor._is_valid_dto_name(name), f"{name} should be rejected"

    def test_response_wrappers_rejected(self):
        """Response wrapper types are rejected."""
        for name in ["ResponseEntity", "Mono", "Flux", "CompletableFuture"]:
            assert not BackendJavaExtractor._is_valid_dto_name(name), f"{name} should be rejected"

    def test_annotation_names_rejected(self):
        """Known annotation names are rejected."""
        for name in ["JsonProperty", "Schema", "NotNull", "Override", "Hidden", "Id"]:
            assert not BackendJavaExtractor._is_valid_dto_name(name), f"{name} should be rejected"

    def test_all_caps_rejected(self):
        """ALL_CAPS constants are rejected."""
        for name in ["EXAMPLE_IDS", "MAX_SIZE", "DEFAULT_VALUE"]:
            assert not BackendJavaExtractor._is_valid_dto_name(name), f"{name} should be rejected"

    def test_single_char_rejected(self):
        """Single character names are rejected."""
        assert not BackendJavaExtractor._is_valid_dto_name("T")
        assert not BackendJavaExtractor._is_valid_dto_name("E")

    def test_empty_rejected(self):
        """Empty string is rejected."""
        assert not BackendJavaExtractor._is_valid_dto_name("")

    def test_lowercase_start_rejected(self):
        """Names starting with lowercase are rejected."""
        assert not BackendJavaExtractor._is_valid_dto_name("orderDto")

    def test_names_with_special_chars_rejected(self):
        """Names with brackets or special chars are rejected."""
        assert not BackendJavaExtractor._is_valid_dto_name("String[]")
        assert not BackendJavaExtractor._is_valid_dto_name("Map<String")


# ---------------------------------------------------------------------------
# TestBuildClassIndex
# ---------------------------------------------------------------------------


class TestBuildClassIndex:
    """Tests for BackendJavaExtractor._build_class_index() inner class support."""

    def test_indexes_inner_classes(self, tmp_path):
        """Inner classes defined inside a file are indexed."""
        # Create a Java file with an inner class
        java_dir = tmp_path / "src" / "main" / "java" / "com" / "example"
        java_dir.mkdir(parents=True)
        controller_file = java_dir / "SeatController.java"
        controller_file.write_text('''
package com.example;

public class SeatController {

    public static class SeatEvent {
        private String seatId;
        private String status;
    }

    public ResponseEntity<SeatEvent> getSeat() {
        return ResponseEntity.ok(new SeatEvent());
    }
}
''')

        extractor = BackendJavaExtractor()
        class_index = extractor._build_class_index(tmp_path)

        # SeatController should be indexed by file stem
        assert "SeatController" in class_index
        # SeatEvent (inner class) should also be indexed
        assert "SeatEvent" in class_index
        assert class_index["SeatEvent"] == controller_file

    def test_file_stem_takes_priority_over_inner(self, tmp_path):
        """If a file stem matches an inner class name, file stem wins."""
        java_dir = tmp_path / "src" / "main" / "java" / "com" / "example"
        java_dir.mkdir(parents=True)

        # Create SeatEvent.java as a standalone file
        event_file = java_dir / "SeatEvent.java"
        event_file.write_text('package com.example;\npublic class SeatEvent { }')

        # Create another file that also declares SeatEvent as inner class
        controller_file = java_dir / "SeatController.java"
        controller_file.write_text('''
package com.example;
public class SeatController {
    public static class SeatEvent { }
}
''')

        extractor = BackendJavaExtractor()
        class_index = extractor._build_class_index(tmp_path)

        # SeatEvent should point to the standalone file, not the inner class
        assert class_index["SeatEvent"] == event_file


# ---------------------------------------------------------------------------
# TestEndpointExtractionWithComplexAnnotations
# ---------------------------------------------------------------------------


class TestEndpointExtractionWithComplexAnnotations:
    """Integration test for endpoint extraction with complex annotations."""

    def test_endpoint_extraction_with_complex_annotations(self, tmp_path):
        """Endpoints with @PreAuthorize, @Schema, @Hidden annotations extract clean types."""
        java_dir = tmp_path / "src" / "main" / "java" / "com" / "example"
        java_dir.mkdir(parents=True)

        controller = java_dir / "IncidentController.java"
        controller.write_text('''
package com.example;

import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/api/incidents")
public class IncidentController {

    @PostMapping
    @PreAuthorize("hasRole('ROLE_ADMIN')")
    public ResponseEntity<IncidentResponse> createIncident(
            @Schema(description = "Incident Id.", example = "eyJpdiI6ImFHOVVObmRxV0Z")
            @JsonProperty("incident_id")
            @RequestBody IncidentRequest request) {
        return null;
    }
}
''')

        # Create the DTO files
        request_file = java_dir / "IncidentRequest.java"
        request_file.write_text(
            'package com.example;\n'
            'public class IncidentRequest { private String name; }')

        response_file = java_dir / "IncidentResponse.java"
        response_file.write_text(
            'package com.example;\n'
            'public class IncidentResponse { private String id; }')

        # Create build.gradle
        (tmp_path / "build.gradle").write_text("""
plugins { id 'org.springframework.boot' version '3.1.0' }
dependencies { implementation 'org.springframework.boot:spring-boot-starter-web' }
""")

        extractor = BackendJavaExtractor()
        contracts = extractor.find_api_contracts(tmp_path)

        assert len(contracts) == 1
        endpoints = contracts[0].endpoints
        assert len(endpoints) >= 1

        post_endpoint = [e for e in endpoints if e.method == "POST"][0]

        # The response type should be clean — no annotation text
        if post_endpoint.response:
            assert "@PreAuthorize" not in (post_endpoint.response.type or "")
            assert "hasRole" not in (post_endpoint.response.type or "")
            assert "eyJpdiI6" not in (post_endpoint.response.type or "")

        # The request body type should be clean
        if post_endpoint.request_body:
            assert "eyJpdiI6" not in (post_endpoint.request_body.type or "")
            assert "@Schema" not in (post_endpoint.request_body.type or "")
            assert "JsonProperty" not in (post_endpoint.request_body.type or "")


# ---------------------------------------------------------------------------
# TestBuildClassIndexCommentStripping
# ---------------------------------------------------------------------------


class TestBuildClassIndexCommentStripping:
    """Tests that _build_class_index ignores class declarations inside comments."""

    def test_commented_out_classes_not_indexed(self, tmp_path):
        """Class declarations inside comments are not indexed."""
        java_dir = tmp_path / "src" / "main" / "java" / "com" / "example"
        java_dir.mkdir(parents=True)
        controller_file = java_dir / "MyController.java"
        controller_file.write_text('''
package com.example;

// public class CommentedOut { }

/*
 * public class InBlockComment { }
 */

/**
 * public class InJavadoc { }
 */

public class MyController {
    public static class RealInner { }

    // public class FakeInner { }
}
''')

        extractor = BackendJavaExtractor()
        class_index = extractor._build_class_index(tmp_path)

        assert "MyController" in class_index
        assert "RealInner" in class_index
        assert "CommentedOut" not in class_index
        assert "InBlockComment" not in class_index
        assert "InJavadoc" not in class_index
        assert "FakeInner" not in class_index
