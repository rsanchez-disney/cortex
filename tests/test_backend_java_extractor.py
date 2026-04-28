"""Tests for the BackendJavaExtractor."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from atlas.extractors.backend_java import BackendJavaExtractor
from atlas.schema import ServiceYaml

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
            "dependencies {\n    implementation 'org.springframework.boot:spring-boot-starter-data-redis:3.1.0'\n}\n"
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

    def test_gradle_ext_vars_with_inline_content(self, extractor: BackendJavaExtractor, tmp_path: Path) -> None:
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

    def test_endpoints_in_fixture_have_clean_summaries(self, extractor: BackendJavaExtractor) -> None:
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
            "        MethodKafkaListenerEndpoint<String, String> ep = new MethodKafkaListenerEndpoint<>();\n"
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
        content = '@RequestMapping(path = "/v1/admin", produces = "application/json")\npublic class MyController {}'
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

    def test_try_extract_op_attr_missing_returns_none(self, extractor: BackendJavaExtractor) -> None:
        """_try_extract_op_attr returns None when attribute is absent."""
        text = '@Operation(description = "Retrieve account")'
        assert extractor._try_extract_op_attr(text, "summary") is None

    def test_description_endpoint_captured_in_fixture(self, extractor: BackendJavaExtractor) -> None:
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
            'azure-cosmos = { module = "com.azure.spring:spring-cloud-azure-starter-data-cosmos", version.ref = "cosmos" }\n'
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
            'mapstruct = { group = "org.mapstruct", name = "mapstruct", version.ref = "mapstruct" }\n'
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
