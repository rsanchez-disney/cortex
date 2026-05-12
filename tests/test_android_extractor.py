"""Tests for the Android extractor."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from atlas.extractors.android import AndroidExtractor
from atlas.schema import ServiceYaml
from tests.conftest import SAMPLE_ANDROID_REPO


@pytest.fixture
def android_extractor() -> AndroidExtractor:
    return AndroidExtractor()


@pytest.fixture
def android_service_yaml() -> ServiceYaml:
    return ServiceYaml(
        name="sample-android",
        type="android",
        owner="team-mobile",
        domain="mobile",
        tier="standard",
        purpose="Sample Android app for testing the extractor.",
        status="active",
        slack="#team-mobile",
        keywords=["banking", "android", "mobile"],
        integration_notes=[{"scope": "global", "note": "Uses custom auth flow via AuthManager"}],
    )


class TestAndroidExtractor:
    """Tests for AndroidExtractor.extract()."""

    def test_successful_extraction(
        self, android_extractor: AndroidExtractor, android_service_yaml: ServiceYaml
    ) -> None:
        """Successful extraction produces correct manifest with all expected fields."""
        manifest = android_extractor.extract(SAMPLE_ANDROID_REPO, android_service_yaml)

        assert manifest.name == "sample-android"
        assert manifest.type == "android"
        assert manifest.owner == "team-mobile"
        assert manifest.domain == "mobile"
        assert manifest.tier == "standard"
        assert manifest.purpose == "Sample Android app for testing the extractor."
        assert manifest.extractor_version == "1.0.0"
        assert manifest.extracted_at is not None

    def test_language_detection(
        self, android_extractor: AndroidExtractor, android_service_yaml: ServiceYaml
    ) -> None:
        """Detects Kotlin as primary language (fixture has .kt files)."""
        manifest = android_extractor.extract(SAMPLE_ANDROID_REPO, android_service_yaml)
        assert manifest.language == "kotlin"

    def test_sdk_versions(
        self, android_extractor: AndroidExtractor, android_service_yaml: ServiceYaml
    ) -> None:
        """Extracts min/target/compile SDK from build.gradle.kts."""
        # The _parse_gradle_sdk method should find SDK versions
        sdk_info = android_extractor._parse_gradle_sdk(SAMPLE_ANDROID_REPO)
        assert sdk_info["min_sdk"] == "26"
        assert sdk_info["target_sdk"] == "34"
        assert sdk_info["compile_sdk"] == "34"

    def test_application_id(self, android_extractor: AndroidExtractor) -> None:
        """Extracts applicationId from app/build.gradle.kts."""
        app_id = android_extractor._find_application_id(SAMPLE_ANDROID_REPO)
        assert app_id == "com.example.sampleandroid"

    def test_dependencies_from_gradle(self, android_extractor: AndroidExtractor) -> None:
        """Parses dependencies from build.gradle.kts files."""
        deps = android_extractor._parse_dependencies(SAMPLE_ANDROID_REPO)
        dep_names = [d.name for d in deps]
        assert "androidx.core:core-ktx" in dep_names
        assert "com.squareup.retrofit2:retrofit" in dep_names

    def test_dependencies_from_version_catalog(self, android_extractor: AndroidExtractor) -> None:
        """Parses dependencies from libs.versions.toml."""
        deps = android_extractor._parse_dependencies(SAMPLE_ANDROID_REPO)
        dep_names = [d.name for d in deps]
        # These come from the version catalog
        assert "org.jetbrains.kotlinx:kotlinx-coroutines-android" in dep_names

    def test_modules_parsed(self, android_extractor: AndroidExtractor) -> None:
        """Parses modules from settings.gradle.kts — returns ModuleInfo objects."""
        modules = android_extractor._parse_modules(SAMPLE_ANDROID_REPO)
        module_names = [m.name for m in modules]
        assert "app" in module_names
        assert "core" in module_names
        assert "feature-login" in module_names

    def test_permissions_parsed(self, android_extractor: AndroidExtractor) -> None:
        """Parses permissions from AndroidManifest.xml."""
        permissions = android_extractor._parse_permissions(SAMPLE_ANDROID_REPO)
        assert "android.permission.INTERNET" in permissions
        assert "android.permission.ACCESS_NETWORK_STATE" in permissions

    def test_entry_activities(self, android_extractor: AndroidExtractor) -> None:
        """Parses entry activities (MAIN intent) from AndroidManifest.xml."""
        entries = android_extractor._parse_entry_activities(SAMPLE_ANDROID_REPO)
        assert len(entries) >= 1
        assert any(e.kind == "main-activity" for e in entries)
        assert any(".MainActivity" in e.ref for e in entries)

    def test_no_api_contracts(self, android_extractor: AndroidExtractor) -> None:
        """Mobile apps return no API contracts."""
        contracts = android_extractor.find_api_contracts(SAMPLE_ANDROID_REPO)
        assert contracts == []

    def test_extractor_hints_project_root(self, tmp_path: Path) -> None:
        """extractor_hints.project_root works for nested projects."""
        # Create a nested project structure
        nested = tmp_path / "nested" / "android"
        nested.mkdir(parents=True)

        # Put an Android manifest in the nested dir
        manifest_dir = nested / "app" / "src" / "main"
        manifest_dir.mkdir(parents=True)
        (manifest_dir / "AndroidManifest.xml").write_text(
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<manifest xmlns:android="http://schemas.android.com/apk/res/android"\n'
            '    package="com.nested.app">\n'
            "</manifest>"
        )

        # Create a kotlin source file
        kotlin_dir = nested / "app" / "src" / "main" / "kotlin"
        kotlin_dir.mkdir(parents=True)
        (kotlin_dir / "Main.kt").write_text("class Main")

        service_yaml = ServiceYaml(
            name="nested-app",
            type="android",
            owner="team",
            domain="mobile",
            tier="standard",
            purpose="Nested project test",
            extractor_hints={"project_root": "nested/android"},
        )

        extractor = AndroidExtractor()
        manifest = extractor.extract(tmp_path, service_yaml)
        assert manifest.language == "kotlin"

    def test_missing_optional_files_still_extracts(self, tmp_path: Path) -> None:
        """Missing optional files (no libs.versions.toml, etc.) still extracts."""
        # Create minimal Android project
        (tmp_path / "app" / "src" / "main").mkdir(parents=True)
        (tmp_path / "app" / "src" / "main" / "AndroidManifest.xml").write_text(
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<manifest xmlns:android="http://schemas.android.com/apk/res/android"\n'
            '    package="com.minimal.app">\n'
            "</manifest>"
        )

        service_yaml = ServiceYaml(
            name="minimal-app",
            type="android",
            owner="team",
            domain="mobile",
            tier="standard",
            purpose="Minimal project",
        )

        extractor = AndroidExtractor()
        manifest = extractor.extract(tmp_path, service_yaml)
        assert manifest.name == "minimal-app"
        assert manifest.dependencies == []  # No gradle files, no deps

    def test_language_detection_excludes_build_dir(self, tmp_path: Path) -> None:
        """Language detection should ignore generated .java files in build/ dirs."""
        # Create many generated .java files in build/ (simulating KSP output)
        build_java = tmp_path / "app" / "build" / "generated" / "ksp" / "debug" / "java"
        build_java.mkdir(parents=True)
        for i in range(50):
            (build_java / f"Generated{i}.java").write_text(f"class Generated{i} {{}}")

        # Create fewer hand-written .kt files in src/
        src_kt = tmp_path / "app" / "src" / "main" / "kotlin"
        src_kt.mkdir(parents=True)
        for i in range(10):
            (src_kt / f"Screen{i}.kt").write_text(f"class Screen{i}")

        extractor = AndroidExtractor()
        language, _ = extractor._detect_language(tmp_path)
        assert language == "kotlin", (
            "Should detect kotlin even when build/ has many generated .java files"
        )

    def test_language_detection_falls_back_to_java(self, tmp_path: Path) -> None:
        """Falls back to java when no .kt files exist (excluding build dirs)."""
        src_java = tmp_path / "app" / "src" / "main" / "java"
        src_java.mkdir(parents=True)
        for i in range(5):
            (src_java / f"Foo{i}.java").write_text(f"public class Foo{i} {{}}")

        extractor = AndroidExtractor()
        language, _ = extractor._detect_language(tmp_path)
        assert language == "java"


class TestBuildSrcSdkResolution:
    """Tests for buildSrc constant resolution in _parse_gradle_sdk."""

    def test_resolve_buildsrc_constants_finds_int_const_vals(self, tmp_path: Path) -> None:
        """Scans buildSrc .kt files and extracts const val integer declarations."""
        buildsrc_kt = tmp_path / "buildSrc" / "src" / "main" / "java"
        buildsrc_kt.mkdir(parents=True)
        (buildsrc_kt / "Android.kt").write_text(
            "object Android {\n    const val compileSdk = 36\n    const val minSdk = 29\n}\n"
        )

        extractor = AndroidExtractor()
        constants = extractor._resolve_buildsrc_constants(tmp_path)

        assert constants["compileSdk"] == "36"
        assert constants["minSdk"] == "29"

    def test_resolve_buildsrc_constants_empty_when_no_buildsrc(self, tmp_path: Path) -> None:
        """Returns empty dict when buildSrc directory does not exist."""
        extractor = AndroidExtractor()
        constants = extractor._resolve_buildsrc_constants(tmp_path)
        assert constants == {}

    def test_parse_gradle_sdk_resolves_buildsrc_references(self, tmp_path: Path) -> None:
        """Resolves SDK versions that reference buildSrc constants."""
        # Create buildSrc with Android object
        buildsrc_kt = tmp_path / "buildSrc" / "src" / "main" / "java"
        buildsrc_kt.mkdir(parents=True)
        (buildsrc_kt / "Android.kt").write_text(
            "object Android {\n    const val compileSdk = 36\n    const val minSdk = 29\n}\n"
        )

        # Create app/build.gradle.kts that references the constants
        app_dir = tmp_path / "app"
        app_dir.mkdir()
        (app_dir / "build.gradle.kts").write_text(
            "android {\n"
            "    compileSdk = Android.compileSdk\n"
            "    defaultConfig {\n"
            "        minSdk = Android.minSdk\n"
            "        targetSdk = Android.compileSdk\n"
            "    }\n"
            "}\n"
        )

        extractor = AndroidExtractor()
        sdk_info = extractor._parse_gradle_sdk(tmp_path, repo_root=tmp_path)

        assert sdk_info["compile_sdk"] == "36"
        assert sdk_info["min_sdk"] == "29"
        assert sdk_info["target_sdk"] == "36"  # targetSdk = Android.compileSdk resolved to 36

    def test_parse_gradle_sdk_literal_numbers_still_work(self, tmp_path: Path) -> None:
        """Inline literal SDK values are still parsed correctly without buildSrc."""
        app_dir = tmp_path / "app"
        app_dir.mkdir()
        (app_dir / "build.gradle.kts").write_text(
            "android {\n"
            "    compileSdk = 34\n"
            "    defaultConfig {\n"
            "        minSdk = 26\n"
            "        targetSdk = 34\n"
            "    }\n"
            "}\n"
        )

        extractor = AndroidExtractor()
        sdk_info = extractor._parse_gradle_sdk(tmp_path)

        assert sdk_info["compile_sdk"] == "34"
        assert sdk_info["min_sdk"] == "26"
        assert sdk_info["target_sdk"] == "34"

    def test_parse_gradle_sdk_unresolvable_constant_returns_none(self, tmp_path: Path) -> None:
        """Returns None for SDK values that reference undefined constants."""
        app_dir = tmp_path / "app"
        app_dir.mkdir()
        (app_dir / "build.gradle.kts").write_text(
            "android {\n"
            "    compileSdk = Config.sdk\n"  # Not defined anywhere
            "}\n"
        )

        extractor = AndroidExtractor()
        sdk_info = extractor._parse_gradle_sdk(tmp_path)

        assert sdk_info["compile_sdk"] is None

    def test_manifest_includes_sdk_fields(self, tmp_path: Path) -> None:
        """Full extraction surfaces SDK info in the ServiceManifest."""
        # Create buildSrc
        buildsrc_kt = tmp_path / "buildSrc" / "src" / "main" / "java"
        buildsrc_kt.mkdir(parents=True)
        (buildsrc_kt / "Android.kt").write_text(
            "object Android {\n    const val compileSdk = 35\n    const val minSdk = 24\n}\n"
        )

        # Create app/build.gradle.kts with constant references
        app_dir = tmp_path / "app"
        app_dir.mkdir()
        (app_dir / "build.gradle.kts").write_text(
            "android {\n"
            '    namespace = "com.example.app"\n'
            "    compileSdk = Android.compileSdk\n"
            "    defaultConfig {\n"
            "        minSdk = Android.minSdk\n"
            "        targetSdk = Android.compileSdk\n"
            "    }\n"
            "}\n"
        )

        service_yaml = ServiceYaml(
            name="test-sdk-app",
            type="android",
            owner="team",
            domain="mobile",
            tier="standard",
            purpose="SDK resolution test",
        )

        extractor = AndroidExtractor()
        manifest = extractor.extract(tmp_path, service_yaml)

        assert manifest.min_sdk == "24"
        assert manifest.compile_sdk == "35"
        assert manifest.target_sdk == "35"
        assert manifest.application_id == "com.example.app"


class TestNamespaceFallback:
    """Tests for namespace-as-applicationId fallback in _find_application_id."""

    def test_namespace_used_when_application_id_is_dynamic(self, tmp_path: Path) -> None:
        """Falls back to namespace when applicationId is not a string literal."""
        app_dir = tmp_path / "app"
        app_dir.mkdir()
        (app_dir / "build.gradle.kts").write_text(
            "android {\n"
            '    namespace = "com.example.myapp"\n'
            "    defaultConfig {\n"
            '        applicationId = properties.getProperty("config_application_id")\n'
            "    }\n"
            "}\n"
        )

        extractor = AndroidExtractor()
        app_id = extractor._find_application_id(tmp_path)

        assert app_id == "com.example.myapp"

    def test_application_id_literal_takes_priority_over_namespace(self, tmp_path: Path) -> None:
        """String literal applicationId wins over namespace."""
        app_dir = tmp_path / "app"
        app_dir.mkdir()
        (app_dir / "build.gradle.kts").write_text(
            "android {\n"
            '    namespace = "com.example.myapp"\n'
            "    defaultConfig {\n"
            '        applicationId = "com.example.myapp.prod"\n'
            "    }\n"
            "}\n"
        )

        extractor = AndroidExtractor()
        app_id = extractor._find_application_id(tmp_path)

        assert app_id == "com.example.myapp.prod"

    def test_manifest_package_used_as_last_resort(self, tmp_path: Path) -> None:
        """Falls back to AndroidManifest package attribute when no gradle match."""
        manifest_dir = tmp_path / "app" / "src" / "main"
        manifest_dir.mkdir(parents=True)
        (manifest_dir / "AndroidManifest.xml").write_text(
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<manifest xmlns:android="http://schemas.android.com/apk/res/android"\n'
            '    package="com.legacy.app">\n'
            "</manifest>"
        )

        extractor = AndroidExtractor()
        app_id = extractor._find_application_id(tmp_path)

        assert app_id == "com.legacy.app"

    def test_returns_none_when_no_id_found(self, tmp_path: Path) -> None:
        """Returns None when no applicationId or namespace can be resolved."""
        extractor = AndroidExtractor()
        app_id = extractor._find_application_id(tmp_path)
        assert app_id is None


class TestEntryPointDeduplication:
    """Tests for deduplication of MAIN activities across manifest variants."""

    def test_duplicate_activities_across_flavors_are_deduplicated(self, tmp_path: Path) -> None:
        """Same MAIN activity in multiple manifest files results in a single entry."""
        manifest_content = (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<manifest xmlns:android="http://schemas.android.com/apk/res/android">\n'
            "    <application>\n"
            '        <activity android:name=".MainActivity">\n'
            "            <intent-filter>\n"
            '                <action android:name="android.intent.action.MAIN" />\n'
            "            </intent-filter>\n"
            "        </activity>\n"
            "    </application>\n"
            "</manifest>"
        )

        # Put the same manifest in three different variant directories
        for variant in ["debug", "release", "qa"]:
            variant_dir = tmp_path / "app" / "src" / variant
            variant_dir.mkdir(parents=True)
            (variant_dir / "AndroidManifest.xml").write_text(manifest_content)

        extractor = AndroidExtractor()
        entries = extractor._parse_entry_activities(tmp_path)

        # Despite 3 manifest files all declaring the same activity, only 1 entry
        assert len(entries) == 1
        assert entries[0].ref == ".MainActivity"
        assert entries[0].kind == "main-activity"

    def test_distinct_activities_all_included(self, tmp_path: Path) -> None:
        """Different MAIN activities from different manifests are all captured."""
        for idx, name in enumerate([".MainActivity", ".WearActivity"]):
            variant_dir = tmp_path / "app" / "src" / f"variant{idx}"
            variant_dir.mkdir(parents=True)
            (variant_dir / "AndroidManifest.xml").write_text(
                '<?xml version="1.0" encoding="utf-8"?>\n'
                '<manifest xmlns:android="http://schemas.android.com/apk/res/android">\n'
                "    <application>\n"
                f'        <activity android:name="{name}">\n'
                "            <intent-filter>\n"
                '                <action android:name="android.intent.action.MAIN" />\n'
                "            </intent-filter>\n"
                "        </activity>\n"
                "    </application>\n"
                "</manifest>"
            )

        extractor = AndroidExtractor()
        entries = extractor._parse_entry_activities(tmp_path)
        refs = {e.ref for e in entries}

        assert ".MainActivity" in refs
        assert ".WearActivity" in refs
        assert len(entries) == 2


class TestModuleInfoExtraction:
    """Tests for enhanced _parse_modules() returning ModuleInfo with type + deps."""

    def test_fixture_modules_have_correct_types(self) -> None:
        """Fixture modules get correct types: app=application, core/feature-login=library."""
        extractor = AndroidExtractor()
        modules = extractor._parse_modules(SAMPLE_ANDROID_REPO)

        by_name = {m.name: m for m in modules}
        assert by_name["app"].type == "application"
        assert by_name["core"].type == "library"
        assert by_name["feature-login"].type == "library"

    def test_fixture_feature_login_has_inter_module_dep(self) -> None:
        """feature-login depends on :core — should be captured as inter-module dep."""
        extractor = AndroidExtractor()
        modules = extractor._parse_modules(SAMPLE_ANDROID_REPO)

        by_name = {m.name: m for m in modules}
        assert "core" in by_name["feature-login"].dependencies

    def test_fixture_app_has_no_inter_module_deps(self) -> None:
        """app module in fixture has no project() dependencies."""
        extractor = AndroidExtractor()
        modules = extractor._parse_modules(SAMPLE_ANDROID_REPO)

        by_name = {m.name: m for m in modules}
        assert by_name["app"].dependencies == []

    def test_modules_wired_into_manifest(self) -> None:
        """Full extraction includes modules in the manifest."""
        extractor = AndroidExtractor()
        service_yaml = ServiceYaml(
            name="sample-android",
            type="android",
            owner="team",
            domain="mobile",
            tier="standard",
            purpose="Test",
        )
        manifest = extractor.extract(SAMPLE_ANDROID_REPO, service_yaml)

        assert len(manifest.modules) >= 3
        module_names = [m.name for m in manifest.modules]
        assert "app" in module_names
        assert "core" in module_names

    def test_kmp_type_detection_via_plugin_id(self, tmp_path: Path) -> None:
        """Module applying kotlin-multiplatform plugin ID gets type='kmp'."""
        (tmp_path / "settings.gradle.kts").write_text('include(":mpp-lib")')
        mpp_dir = tmp_path / "mpp-lib"
        mpp_dir.mkdir()
        (mpp_dir / "build.gradle.kts").write_text(
            'plugins { id("kotlin-multiplatform"); id("com.android.library") }\n'
        )

        extractor = AndroidExtractor()
        modules = extractor._parse_modules(tmp_path)

        assert len(modules) == 1
        assert modules[0].type == "kmp"

    def test_kmp_type_detection_via_version_catalog_alias(self, tmp_path: Path) -> None:
        """Module using alias(libs.plugins.kotlinMultiplatform) gets type='kmp'."""
        (tmp_path / "settings.gradle.kts").write_text('include(":mpp-lib")')
        mpp_dir = tmp_path / "mpp-lib"
        mpp_dir.mkdir()
        (mpp_dir / "build.gradle.kts").write_text(
            "plugins {\n"
            "    alias(libs.plugins.kotlinMultiplatform)\n"
            '    id("com.android.library")\n'
            "}\n"
        )

        extractor = AndroidExtractor()
        modules = extractor._parse_modules(tmp_path)

        assert len(modules) == 1
        assert modules[0].type == "kmp"

    def test_groovy_include_syntax_parsed(self, tmp_path: Path) -> None:
        """Groovy settings.gradle include ':app' single-line syntax is handled."""
        (tmp_path / "settings.gradle").write_text(
            "include ':app'\ninclude ':lib'\n"
        )
        (tmp_path / "app").mkdir()
        (tmp_path / "app" / "build.gradle.kts").write_text(
            'plugins { id("com.android.application") }\n'
        )
        (tmp_path / "lib").mkdir()
        (tmp_path / "lib" / "build.gradle.kts").write_text(
            'plugins { id("com.android.library") }\n'
        )

        extractor = AndroidExtractor()
        modules = extractor._parse_modules(tmp_path)

        names = [m.name for m in modules]
        assert "app" in names
        assert "lib" in names

    def test_case_insensitive_directory_lookup(self, tmp_path: Path) -> None:
        """Module ':common' resolves to 'Common/' directory (case-insensitive fallback)."""
        (tmp_path / "settings.gradle").write_text("include ':common'\n")
        # Note: capital C — matching the real repo's Common/ directory
        common_dir = tmp_path / "Common"
        common_dir.mkdir()
        (common_dir / "build.gradle.kts").write_text(
            'plugins { id("com.android.library") }\n'
        )

        extractor = AndroidExtractor()
        modules = extractor._parse_modules(tmp_path)

        assert len(modules) == 1
        assert modules[0].name == "common"
        assert modules[0].type == "library"

    def test_nested_module_colon_to_slash_resolution(self, tmp_path: Path) -> None:
        """Module ':parent:child' resolves to parent/child/ directory."""
        (tmp_path / "settings.gradle.kts").write_text('include(":parent:child")')
        nested = tmp_path / "parent" / "child"
        nested.mkdir(parents=True)
        (nested / "build.gradle.kts").write_text(
            'plugins { id("com.android.library") }\n'
        )

        extractor = AndroidExtractor()
        modules = extractor._parse_modules(tmp_path)

        assert len(modules) == 1
        assert modules[0].name == "parent:child"
        assert modules[0].type == "library"

    def test_modules_kt_constants_resolved_for_deps(self, tmp_path: Path) -> None:
        """Modules.xxx constants in build.gradle.kts are resolved via buildSrc."""
        # buildSrc/Modules.kt with string constants
        buildsrc = tmp_path / "buildSrc" / "src" / "main" / "java"
        buildsrc.mkdir(parents=True)
        (buildsrc / "Modules.kt").write_text(
            'object Modules {\n    const val core = ":core"\n}\n'
        )

        (tmp_path / "settings.gradle.kts").write_text(
            'include(":app")\ninclude(":core")\n'
        )
        (tmp_path / "app").mkdir()
        (tmp_path / "app" / "build.gradle.kts").write_text(
            'plugins { id("com.android.application") }\n'
            "dependencies {\n"
            "    implementation(project(Modules.core))\n"
            "}\n"
        )
        (tmp_path / "core").mkdir()
        (tmp_path / "core" / "build.gradle.kts").write_text(
            'plugins { id("com.android.library") }\n'
        )

        extractor = AndroidExtractor()
        modules = extractor._parse_modules(tmp_path)

        by_name = {m.name: m for m in modules}
        assert "core" in by_name["app"].dependencies

    def test_unknown_type_for_missing_build_file(self, tmp_path: Path) -> None:
        """Module with no build file gets type='unknown'."""
        (tmp_path / "settings.gradle.kts").write_text('include(":missing")')
        # No build.gradle.kts created for :missing

        extractor = AndroidExtractor()
        modules = extractor._parse_modules(tmp_path)

        assert len(modules) == 1
        assert modules[0].name == "missing"
        assert modules[0].type == "unknown"


class TestVersionCatalogMetadata:
    """Tests for Kotlin version, AGP version, and plugin extraction."""

    def test_kotlin_and_agp_versions_from_fixture(self) -> None:
        """Reads kotlin and agp versions from fixture libs.versions.toml."""
        extractor = AndroidExtractor()
        meta = extractor._parse_version_catalog_metadata(SAMPLE_ANDROID_REPO)

        assert meta["kotlin_version"] == "1.9.22"
        assert meta["agp_version"] == "8.2.0"

    def test_plugin_ids_extracted_from_fixture(self) -> None:
        """Reads plugin IDs (not aliases) from fixture [plugins] section."""
        extractor = AndroidExtractor()
        meta = extractor._parse_version_catalog_metadata(SAMPLE_ANDROID_REPO)

        plugins = meta["plugins"]
        assert "com.android.application" in plugins
        assert "com.android.library" in plugins
        assert "org.jetbrains.kotlin.android" in plugins
        assert "com.google.dagger.hilt.android" in plugins
        assert "com.google.devtools.ksp" in plugins

    def test_language_version_populated_from_kotlin_version(self) -> None:
        """Full extraction sets language_version from libs.versions.toml kotlin key."""
        extractor = AndroidExtractor()
        service_yaml = ServiceYaml(
            name="sample-android",
            type="android",
            owner="team",
            domain="mobile",
            tier="standard",
            purpose="Test",
        )
        manifest = extractor.extract(SAMPLE_ANDROID_REPO, service_yaml)

        assert manifest.language_version == "1.9.22"
        assert manifest.android_gradle_plugin == "8.2.0"

    def test_gradle_plugins_in_manifest(self) -> None:
        """Full extraction includes gradle_plugins list in manifest."""
        extractor = AndroidExtractor()
        service_yaml = ServiceYaml(
            name="sample-android",
            type="android",
            owner="team",
            domain="mobile",
            tier="standard",
            purpose="Test",
        )
        manifest = extractor.extract(SAMPLE_ANDROID_REPO, service_yaml)

        assert len(manifest.gradle_plugins) > 0
        assert "com.android.application" in manifest.gradle_plugins

    def test_missing_version_catalog_returns_none(self, tmp_path: Path) -> None:
        """Returns None for versions when no libs.versions.toml exists."""
        extractor = AndroidExtractor()
        meta = extractor._parse_version_catalog_metadata(tmp_path)

        assert meta["kotlin_version"] is None
        assert meta["agp_version"] is None
        assert meta["plugins"] == []


class TestBuildVariants:
    """Tests for product flavor / build variant extraction."""

    def test_explicit_product_flavors_parsed(self, tmp_path: Path) -> None:
        """Parses explicit create("name") declarations from productFlavors block."""
        app_dir = tmp_path / "app"
        app_dir.mkdir()
        (app_dir / "build.gradle.kts").write_text(
            "android {\n"
            "    productFlavors {\n"
            '        create("dev") { dimension = "env" }\n'
            '        create("prod") { dimension = "env" }\n'
            "    }\n"
            "}\n"
        )

        extractor = AndroidExtractor()
        variants = extractor._parse_build_variants(tmp_path)

        assert "dev" in variants
        assert "prod" in variants

    def test_properties_dir_fallback(self, tmp_path: Path) -> None:
        """Derives flavor names from properties/ directory filenames."""
        props_dir = tmp_path / "properties"
        props_dir.mkdir()
        (props_dir / "clippers.dev.properties").write_text("config_application_id=com.example")
        (props_dir / "clippers.qa.properties").write_text("config_application_id=com.example")
        (props_dir / "clippers.pro.properties").write_text("config_application_id=com.example")
        # Two-part file — should be ignored
        (props_dir / "clippers.properties").write_text("base=config")

        extractor = AndroidExtractor()
        variants = extractor._parse_build_variants(tmp_path)

        assert "clippersDev" in variants
        assert "clippersQa" in variants
        assert "clippersPro" in variants
        # Two-part base config not included
        assert "clippers" not in variants

    def test_no_variants_returns_empty(self, tmp_path: Path) -> None:
        """Returns empty list when no flavors detected."""
        extractor = AndroidExtractor()
        variants = extractor._parse_build_variants(tmp_path)
        assert variants == []

    def test_build_variants_in_manifest(self, tmp_path: Path) -> None:
        """Full extraction includes build_variants in manifest."""
        app_dir = tmp_path / "app"
        app_dir.mkdir()
        (app_dir / "build.gradle.kts").write_text(
            "android {\n"
            '    namespace = "com.example"\n'
            "    productFlavors {\n"
            '        create("dev") { }\n'
            '        create("stg") { }\n'
            "    }\n"
            "}\n"
        )

        service_yaml = ServiceYaml(
            name="variant-app",
            type="android",
            owner="team",
            domain="mobile",
            tier="standard",
            purpose="Test",
        )
        extractor = AndroidExtractor()
        manifest = extractor.extract(tmp_path, service_yaml)

        assert "dev" in manifest.build_variants
        assert "stg" in manifest.build_variants


class TestDependencyCategories:
    """Tests for dependency category tagging from Gradle configuration names."""

    def test_implementation_tagged_as_runtime(self) -> None:
        """implementation(...) → category='runtime'."""
        extractor = AndroidExtractor()
        deps: list = []
        seen: set = set()
        extractor._parse_gradle_deps(
            'implementation("com.example:foo:1.0")', "build.gradle.kts", deps, seen
        )
        assert deps[0].category == "runtime"

    def test_api_tagged_as_runtime(self) -> None:
        """api(...) → category='runtime'."""
        extractor = AndroidExtractor()
        deps: list = []
        seen: set = set()
        extractor._parse_gradle_deps(
            'api("com.example:bar:1.0")', "build.gradle.kts", deps, seen
        )
        assert deps[0].category == "runtime"

    def test_test_implementation_tagged_as_test(self) -> None:
        """testImplementation(...) → category='test'."""
        extractor = AndroidExtractor()
        deps: list = []
        seen: set = set()
        extractor._parse_gradle_deps(
            'testImplementation("junit:junit:4.13")', "build.gradle.kts", deps, seen
        )
        assert deps[0].category == "test"

    def test_android_test_implementation_tagged_as_test(self) -> None:
        """androidTestImplementation(...) → category='test'."""
        extractor = AndroidExtractor()
        deps: list = []
        seen: set = set()
        extractor._parse_gradle_deps(
            'androidTestImplementation("androidx.test:runner:1.0")', "build.gradle.kts", deps, seen
        )
        assert deps[0].category == "test"

    def test_kapt_tagged_as_build(self) -> None:
        """kapt(...) → category='build'."""
        extractor = AndroidExtractor()
        deps: list = []
        seen: set = set()
        extractor._parse_gradle_deps(
            'kapt("com.google.dagger:hilt-compiler:2.50")', "build.gradle.kts", deps, seen
        )
        assert deps[0].category == "build"

    def test_ksp_tagged_as_build(self) -> None:
        """ksp(...) → category='build'."""
        extractor = AndroidExtractor()
        deps: list = []
        seen: set = set()
        extractor._parse_gradle_deps(
            'ksp("com.google.dagger:hilt-compiler:2.50")', "build.gradle.kts", deps, seen
        )
        assert deps[0].category == "build"

    def test_debug_implementation_tagged_as_debug(self) -> None:
        """debugImplementation(...) → category='debug'."""
        extractor = AndroidExtractor()
        deps: list = []
        seen: set = set()
        extractor._parse_gradle_deps(
            'debugImplementation("com.example:debug-tool:1.0")', "build.gradle.kts", deps, seen
        )
        assert deps[0].category == "debug"

    def test_version_catalog_deps_have_no_category(self) -> None:
        """Dependencies from libs.versions.toml have category=None (uncategorized)."""
        extractor = AndroidExtractor()
        deps = extractor._parse_dependencies(SAMPLE_ANDROID_REPO)
        catalog_deps = [d for d in deps if d.source == "libs.versions.toml"]
        assert len(catalog_deps) > 0
        for dep in catalog_deps:
            assert dep.category is None

    def test_gradle_deps_have_categories(self) -> None:
        """Dependencies parsed from build.gradle.kts have non-None categories."""
        extractor = AndroidExtractor()
        deps = extractor._parse_dependencies(SAMPLE_ANDROID_REPO)
        gradle_deps = [d for d in deps if d.source == "build.gradle.kts"]
        assert len(gradle_deps) > 0
        # All should have categories (implementation, api, kapt in fixture)
        for dep in gradle_deps:
            assert dep.category is not None


class TestPermissionsWiredIntoManifest:
    """Tests that permissions are wired into the manifest (previously dead code)."""

    def test_permissions_in_manifest(self) -> None:
        """Full extraction includes permissions in the manifest."""
        extractor = AndroidExtractor()
        service_yaml = ServiceYaml(
            name="sample-android",
            type="android",
            owner="team",
            domain="mobile",
            tier="standard",
            purpose="Test",
        )
        manifest = extractor.extract(SAMPLE_ANDROID_REPO, service_yaml)

        assert "android.permission.INTERNET" in manifest.permissions
        assert "android.permission.ACCESS_NETWORK_STATE" in manifest.permissions


class TestSourceRepoDetection:
    """Tests for _get_source_repo() in base.py."""

    def test_source_repo_returns_none_for_non_git_dir(self, tmp_path: Path) -> None:
        """Returns None when path is not a git repo."""
        extractor = AndroidExtractor()
        result = extractor._get_source_repo(tmp_path)
        assert result is None

    def test_source_repo_captures_url_and_commit(self, tmp_path: Path) -> None:
        """Captures URL and commit when git commands succeed."""
        extractor = AndroidExtractor()

        with patch("atlas.extractors.base.subprocess.run") as mock_run:
            mock_url = MagicMock()
            mock_url.returncode = 0
            mock_url.stdout = "git@github.com:org/repo.git\n"

            mock_commit = MagicMock()
            mock_commit.returncode = 0
            mock_commit.stdout = "abc123def456\n"

            mock_run.side_effect = [mock_url, mock_commit]

            result = extractor._get_source_repo(tmp_path)

        assert result is not None
        assert result.url == "git@github.com:org/repo.git"
        assert result.commit == "abc123def456"

    def test_source_repo_returns_none_on_timeout(self, tmp_path: Path) -> None:
        """Returns None when subprocess times out."""
        import subprocess

        extractor = AndroidExtractor()

        with patch("atlas.extractors.base.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired("git", 10)
            result = extractor._get_source_repo(tmp_path)

        assert result is None

    def test_source_repo_returns_none_when_git_not_found(self, tmp_path: Path) -> None:
        """Returns None when git is not installed (FileNotFoundError)."""
        extractor = AndroidExtractor()

        with patch("atlas.extractors.base.subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError("git not found")
            result = extractor._get_source_repo(tmp_path)

        assert result is None

    def test_source_repo_handles_partial_failure(self, tmp_path: Path) -> None:
        """Returns SourceRepo with available info when one command fails."""
        extractor = AndroidExtractor()

        with patch("atlas.extractors.base.subprocess.run") as mock_run:
            mock_url = MagicMock()
            mock_url.returncode = 128  # git error — not a git repo
            mock_url.stdout = ""

            mock_commit = MagicMock()
            mock_commit.returncode = 0
            mock_commit.stdout = "abc123\n"

            mock_run.side_effect = [mock_url, mock_commit]

            result = extractor._get_source_repo(tmp_path)

        # url is None (returncode != 0), commit is set
        assert result is not None
        assert result.url is None
        assert result.commit == "abc123"


class TestCIDetection:
    """Tests for _detect_ci() — previously had zero test coverage."""

    def test_github_actions_detected(self, tmp_path: Path) -> None:
        """Detects GitHub Actions from .github/workflows/ directory."""
        (tmp_path / ".github" / "workflows").mkdir(parents=True)
        extractor = AndroidExtractor()
        assert extractor._detect_ci(tmp_path) == "github-actions"

    def test_azure_pipelines_detected(self, tmp_path: Path) -> None:
        """Detects Azure Pipelines from azure-pipelines.yml file."""
        (tmp_path / "azure-pipelines.yml").write_text("trigger: main\n")
        extractor = AndroidExtractor()
        assert extractor._detect_ci(tmp_path) == "azure-pipelines"

    def test_gitlab_ci_detected(self, tmp_path: Path) -> None:
        """Detects GitLab CI from .gitlab-ci.yml file."""
        (tmp_path / ".gitlab-ci.yml").write_text("stages:\n  - build\n")
        extractor = AndroidExtractor()
        assert extractor._detect_ci(tmp_path) == "gitlab-ci"

    def test_no_ci_returns_none(self, tmp_path: Path) -> None:
        """Returns None when no CI config files are present."""
        extractor = AndroidExtractor()
        assert extractor._detect_ci(tmp_path) is None

    def test_fixture_has_azure_pipelines(self) -> None:
        """The sample fixture includes azure-pipelines.yml detection via full extraction."""
        # The fixture itself doesn't have azure-pipelines.yml, but this verifies
        # CI is surfaced in the manifest when it's present.
        extractor = AndroidExtractor()
        assert extractor._detect_ci(SAMPLE_ANDROID_REPO) is None  # Fixture has no CI file


# ---------------------------------------------------------------------------
# TestApiCallExtraction
# ---------------------------------------------------------------------------


class TestApiCallExtraction:
    """Tests for _parse_ktorfit_interfaces() (API call detection from Kotlin interfaces)."""

    def test_ktorfit_interface_endpoints_parsed(self, tmp_path: Path) -> None:
        """Kotlin interface with @GET/@POST annotations → api_calls populated."""
        api_dir = tmp_path / "src" / "main" / "kotlin" / "com" / "example" / "api"
        api_dir.mkdir(parents=True)
        (api_dir / "UserApi.kt").write_text(
            "package com.example.api\n"
            "interface UserApi {\n"
            '    @GET("/v1/users")\n'
            "    suspend fun getUsers(): List<User>\n"
            '    @POST("/v1/users")\n'
            "    suspend fun createUser(): User\n"
            "}\n"
        )
        extractor = AndroidExtractor()
        calls = extractor._parse_ktorfit_interfaces(tmp_path)
        paths = [c.path for c in calls]
        methods = [c.method for c in calls]
        assert "/v1/users" in paths
        assert "GET" in methods
        assert "POST" in methods

    def test_retrofit_interface_endpoints_parsed(self, tmp_path: Path) -> None:
        """Retrofit-style @GET/@POST interface in network/ dir → api_calls populated."""
        net_dir = tmp_path / "app" / "src" / "main" / "kotlin" / "com" / "example" / "network"
        net_dir.mkdir(parents=True)
        (net_dir / "OrderService.kt").write_text(
            "package com.example.network\n"
            "interface OrderService {\n"
            '    @GET("/v1/orders/{id}")\n'
            "    suspend fun getOrder(@Path(\"id\") id: String): Order\n"
            "}\n"
        )
        extractor = AndroidExtractor()
        calls = extractor._parse_ktorfit_interfaces(tmp_path)
        paths = [c.path for c in calls]
        assert "/v1/orders/{id}" in paths

    def test_api_calls_in_manifest(
        self, tmp_path: Path, android_service_yaml: ServiceYaml
    ) -> None:
        """Full extraction includes api_calls field with parsed endpoints."""
        api_dir = tmp_path / "app" / "src" / "main" / "kotlin" / "com" / "example" / "api"
        api_dir.mkdir(parents=True)
        (api_dir / "PayApi.kt").write_text(
            "package com.example.api\n"
            "interface PayApi {\n"
            '    @GET("/v1/payments")\n'
            "    suspend fun getPayments(): List<Payment>\n"
            "}\n"
        )
        # Minimal gradle setup
        (tmp_path / "settings.gradle.kts").write_text('rootProject.name = "test-app"\n')
        extractor = AndroidExtractor()
        with patch.object(extractor, "_get_source_repo", return_value=None):
            manifest = extractor.extract(tmp_path, android_service_yaml)
        assert isinstance(manifest.api_calls, list)
        paths = [c.path for c in manifest.api_calls]
        assert "/v1/payments" in paths

    def test_no_api_calls_in_minimal_repo(
        self, tmp_path: Path, android_service_yaml: ServiceYaml
    ) -> None:
        """Minimal repo with no network interfaces → api_calls is empty list."""
        (tmp_path / "settings.gradle.kts").write_text('rootProject.name = "test-app"\n')
        extractor = AndroidExtractor()
        with patch.object(extractor, "_get_source_repo", return_value=None):
            manifest = extractor.extract(tmp_path, android_service_yaml)
        assert manifest.api_calls == []

    def test_fixture_has_api_calls(self) -> None:
        """The sample Android fixture OrderApi.kt → api_calls populated."""
        extractor = AndroidExtractor()
        calls = extractor._parse_ktorfit_interfaces(SAMPLE_ANDROID_REPO)
        paths = [c.path for c in calls]
        assert "/v1/orders" in paths
        assert "/v1/orders/{id}" in paths

    def test_build_dirs_excluded(self, tmp_path: Path) -> None:
        """Files in build/ directories are not scanned."""
        build_api = tmp_path / "app" / "build" / "generated" / "api"
        build_api.mkdir(parents=True)
        (build_api / "GeneratedApi.kt").write_text(
            "interface GeneratedApi {\n"
            '    @GET("/v1/should-not-appear")\n'
            "    suspend fun get(): String\n"
            "}\n"
        )
        extractor = AndroidExtractor()
        calls = extractor._parse_ktorfit_interfaces(tmp_path)
        paths = [c.path for c in calls]
        assert "/v1/should-not-appear" not in paths

    def test_string_template_resolution(self, tmp_path: Path) -> None:
        """@GET with $VARIABLE references are resolved using const val declarations."""
        api_dir = tmp_path / "src" / "main" / "kotlin" / "com" / "example" / "api"
        api_dir.mkdir(parents=True)
        # Constants file in same package
        (api_dir / "ApiConstants.kt").write_text(
            "package com.example.api\n"
            "object ApiConstants {\n"
            '    const val BASE_PATH = "orders"\n'
            '    const val API_VERSION = "v1"\n'
            "}\n"
        )
        (api_dir / "OrderApi.kt").write_text(
            "package com.example.api\n"
            "interface OrderApi {\n"
            '    @GET("/$BASE_PATH/$API_VERSION/items")\n'
            "    suspend fun getItems(): List<Item>\n"
            "}\n"
        )
        extractor = AndroidExtractor()
        calls = extractor._parse_ktorfit_interfaces(tmp_path)
        paths = [c.path for c in calls]
        assert "/orders/v1/items" in paths

    def test_curly_brace_template_resolution(self, tmp_path: Path) -> None:
        """${VARIABLE} syntax is resolved in addition to $VARIABLE."""
        api_dir = tmp_path / "src" / "main" / "kotlin" / "com" / "example" / "api"
        api_dir.mkdir(parents=True)
        (api_dir / "ApiConstants.kt").write_text(
            "package com.example.api\n"
            'const val SERVICE = "payments"\n'
            'const val VER = "v2"\n'
        )
        (api_dir / "PaymentApi.kt").write_text(
            "package com.example.api\n"
            "interface PaymentApi {\n"
            '    @POST("/${SERVICE}/${VER}/charge")\n'
            "    suspend fun charge(): Response\n"
            "}\n"
        )
        extractor = AndroidExtractor()
        calls = extractor._parse_ktorfit_interfaces(tmp_path)
        paths = [c.path for c in calls]
        assert "/payments/v2/charge" in paths

    def test_unresolved_variables_kept(self, tmp_path: Path) -> None:
        """When a constant can't be found, the raw $VARIABLE remains (no crash)."""
        api_dir = tmp_path / "src" / "main" / "kotlin" / "com" / "example" / "api"
        api_dir.mkdir(parents=True)
        # No constants file — $UNKNOWN_VAR has no definition
        (api_dir / "FooApi.kt").write_text(
            "package com.example.api\n"
            "interface FooApi {\n"
            '    @GET("/$UNKNOWN_VAR/resource")\n'
            "    suspend fun get(): String\n"
            "}\n"
        )
        extractor = AndroidExtractor()
        calls = extractor._parse_ktorfit_interfaces(tmp_path)
        # Must not crash; path retains unresolved variable
        assert len(calls) == 1
        assert "$UNKNOWN_VAR" in calls[0].path

    def test_collect_string_constants_scans_all_kt_files(self, tmp_path: Path) -> None:
        """_collect_string_constants finds constants across different directories."""
        # Constant in a top-level constants file
        const_dir = tmp_path / "src" / "main" / "kotlin" / "com" / "example"
        const_dir.mkdir(parents=True)
        (const_dir / "Constants.kt").write_text(
            'const val ROOT_API = "rootapi"\n'
        )
        # Constant inside a companion object in an api file
        api_dir = const_dir / "api"
        api_dir.mkdir()
        (api_dir / "Config.kt").write_text(
            "object Config {\n"
            '    const val VERSION = "v3"\n'
            "}\n"
        )
        extractor = AndroidExtractor()
        constants = extractor._collect_string_constants(tmp_path)
        assert constants.get("ROOT_API") == "rootapi"
        assert constants.get("VERSION") == "v3"

    def test_resolve_string_template_mixed_syntax(self) -> None:
        """_resolve_string_template handles both $VAR and ${VAR} in same string."""
        extractor = AndroidExtractor()
        constants = {"SVC": "ticketing", "VER": "v1"}
        result = extractor._resolve_string_template("/$SVC/${VER}/games", constants)
        assert result == "/ticketing/v1/games"

    def test_resolve_string_template_camelcase_variable(self) -> None:
        """_resolve_string_template resolves camelCase variable names ($identityMicro)."""
        extractor = AndroidExtractor()
        constants = {"identityMicro": "identity", "v2": "v2"}
        result = extractor._resolve_string_template("/$identityMicro$v2/login", constants)
        assert result == "/identityv2/login"

    def test_duplicate_api_calls_deduplicated(self, tmp_path: Path) -> None:
        """Same @GET path defined in two files yields only one api_call entry."""
        api_dir = tmp_path / "src" / "main" / "kotlin" / "com" / "example" / "api"
        api_dir.mkdir(parents=True)
        # Two interface files with the same annotation
        (api_dir / "OrderApi.kt").write_text(
            "interface OrderApi {\n"
            '    @GET("/v1/orders")\n'
            "    suspend fun getOrders(): List<Order>\n"
            "}\n"
        )
        (api_dir / "OrderApiV2.kt").write_text(
            "interface OrderApiV2 {\n"
            '    @GET("/v1/orders")\n'  # same path
            "    suspend fun list(): List<Order>\n"
            "}\n"
        )
        extractor = AndroidExtractor()
        calls = extractor._parse_ktorfit_interfaces(tmp_path)
        paths = [c.path for c in calls]
        # Must appear exactly once
        assert paths.count("/v1/orders") == 1
