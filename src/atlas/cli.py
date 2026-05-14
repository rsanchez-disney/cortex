"""Platform Atlas CLI — extract, aggregate, report, run-local, mcp-server.

Entry point: `atlas = "atlas.cli:app"`
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

import structlog
import typer
import yaml

from atlas import __version__
from atlas.extractors import ExtractorError, get_extractor
from atlas.repo_cloner import inject_pat
from atlas.schema import ExtractionError, ServiceManifest
from atlas.storage import StorageBackend, StorageError
from atlas.validation import ValidationError, validate_service_yaml

logger = structlog.get_logger()

app = typer.Typer(
    name="atlas",
    help="Platform Atlas: structured architectural metadata extraction and serving.",
    no_args_is_help=True,
)

# Fields that are config-only (not ServiceYaml fields)
_CONFIG_ONLY_FIELDS = {"path", "url", "branch"}

# Required ServiceYaml fields that must be present in config entries
_REQUIRED_SERVICE_FIELDS = {"type", "owner", "domain", "tier", "purpose"}


@app.command()
def extract(
    repo_path: Path = typer.Option(..., help="Path to the repo to extract"),
    repo_name: str = typer.Option(..., help="Name of the repo (used as storage key)"),
    storage_backend: str = typer.Option("local", help="Storage backend: local or gcs"),
    storage_bucket: str = typer.Option(..., help="Storage bucket or directory path"),
    service_type: str = typer.Option(..., "--type", help="Service type (android, ios, etc.)"),
    owner: str = typer.Option(..., help="Owning team"),
    domain: str = typer.Option(..., help="Business domain"),
    tier: str = typer.Option(..., help="Service tier (critical, standard, experimental, deprecated)"),
    purpose: str = typer.Option(..., help="Short description of the service purpose"),
    status: str = typer.Option("active", help="Service status (active, deprecated, archived)"),
    slack: Optional[str] = typer.Option(None, help="Slack channel (e.g. #team-mobile)"),
    runbook: Optional[str] = typer.Option(None, help="Runbook URL"),
    jira_component: Optional[str] = typer.Option(None, help="Jira component name"),
    keywords: Optional[List[str]] = typer.Option(None, help="Keywords (repeat for multiple)"),
) -> None:
    """Extract metadata from a single repo and write manifest to storage."""
    storage = StorageBackend.from_config(storage_backend, storage_bucket)

    try:
        # Build service metadata dict from CLI args
        service_data: dict = {
            "name": repo_name,
            "type": service_type,
            "owner": owner,
            "domain": domain,
            "tier": tier,
            "purpose": purpose,
            "status": status,
        }
        if slack is not None:
            service_data["slack"] = slack
        if runbook is not None:
            service_data["runbook"] = runbook
        if jira_component is not None:
            service_data["jira_component"] = jira_component
        if keywords:
            service_data["keywords"] = keywords

        # 1. Validate service metadata
        service_yaml = validate_service_yaml(service_data)
        logger.info("service metadata validated", repo=repo_name, type=service_yaml.type)

        # 2. Get extractor
        extractor = get_extractor(service_yaml.type)

        # 3. Run extraction
        manifest = extractor.extract(repo_path, service_yaml)
        logger.info("extraction complete", repo=repo_name)

        # 4. Write manifest
        manifest_data = json.loads(manifest.model_dump_json())
        storage.write_json(f"services/{repo_name}/manifest.json", manifest_data)
        logger.info("manifest written", repo=repo_name, key=f"services/{repo_name}/manifest.json")

        typer.echo(f"OK: {repo_name} extracted successfully")

    except (ValidationError, ExtractorError) as e:
        # Write error file
        error = ExtractionError(
            repo=repo_name,
            timestamp=datetime.now(timezone.utc),
            error=str(e),
            phase="validation" if isinstance(e, ValidationError) else "extraction",
        )
        error_data = json.loads(error.model_dump_json())
        storage.write_json(f"services/{repo_name}/extraction-error.json", error_data)
        logger.error("extraction failed", repo=repo_name, error=str(e))
        typer.echo(f"FAIL: {repo_name} — {e}", err=True)
        raise typer.Exit(code=1)

    except Exception as e:
        error = ExtractionError(
            repo=repo_name,
            timestamp=datetime.now(timezone.utc),
            error=str(e),
            phase="extraction",
        )
        error_data = json.loads(error.model_dump_json())
        storage.write_json(f"services/{repo_name}/extraction-error.json", error_data)
        logger.error("unexpected error during extraction", repo=repo_name, error=str(e))
        typer.echo(f"FAIL: {repo_name} — {e}", err=True)
        raise typer.Exit(code=1)


@app.command()
def aggregate(
    storage_backend: str = typer.Option("local", help="Storage backend: local or gcs"),
    storage_bucket: str = typer.Option(..., help="Storage bucket or directory path"),
) -> None:
    """Aggregate all service manifests into a platform graph."""
    from atlas.aggregator import aggregate as run_aggregate

    storage = StorageBackend.from_config(storage_backend, storage_bucket)
    graph = run_aggregate(storage)

    # Write graph
    graph_data = json.loads(graph.model_dump_json())
    storage.write_json("graph/latest.json", graph_data)

    # Write timestamped snapshot
    timestamp = datetime.now(timezone.utc).isoformat().replace(":", "-")
    storage.write_json(f"graph/{timestamp}.json", graph_data)

    typer.echo(
        f"OK: Graph aggregated — {graph.metadata.service_count} services, "
        f"{len(graph.failed_extractions)} failures"
    )


@app.command()
def report(
    storage_backend: str = typer.Option("local", help="Storage backend: local or gcs"),
    storage_bucket: str = typer.Option(..., help="Storage bucket or directory path"),
) -> None:
    """Generate and display a pipeline run report."""
    storage = StorageBackend.from_config(storage_backend, storage_bucket)

    try:
        graph_data = storage.read_json("graph/latest.json")
    except StorageError:
        typer.echo("FAIL: No graph/latest.json found. Run 'atlas aggregate' first.", err=True)
        raise typer.Exit(code=1)

    services = graph_data.get("services", [])
    failures = graph_data.get("failed_extractions", [])
    metadata = graph_data.get("metadata", {})

    total = len(services) + len(failures)
    typer.echo("=" * 60)
    typer.echo("Platform Atlas — Run Report")
    typer.echo("=" * 60)
    typer.echo(f"  Total repos:             {total}")
    typer.echo(f"  Successful extractions:  {len(services)}")
    typer.echo(f"  Failed extractions:      {len(failures)}")
    typer.echo("")

    if failures:
        typer.echo("  Failed repos:")
        for f in failures:
            typer.echo(f"    - {f.get('repo', 'unknown')}: {f.get('error', 'unknown error')}")
        typer.echo("")

    # Domains summary
    domains: dict[str, int] = {}
    for svc in services:
        d = svc.get("domain", "unknown")
        domains[d] = domains.get(d, 0) + 1

    if domains:
        typer.echo("  Domains:")
        for domain, count in sorted(domains.items()):
            typer.echo(f"    - {domain}: {count} services")
        typer.echo("")

    typer.echo(f"  Graph timestamp: {metadata.get('timestamp', 'unknown')}")
    typer.echo(f"  Extractor version: {metadata.get('version', 'unknown')}")

    # Write run summary to storage
    run_summary = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "total_repos": total,
        "successful": len(services),
        "failed": len(failures),
        "domains": domains,
        "failures": failures,
    }
    timestamp = datetime.now(timezone.utc).isoformat().replace(":", "-")
    storage.write_json(f"runs/{timestamp}.json", run_summary)


@app.command(name="run-local")
def run_local(
    config: Optional[Path] = typer.Option(
        None, help="Path to repos config YAML (repos-local.yaml)"
    ),
    repo_path: Optional[Path] = typer.Option(
        None, help="Single repo path (alternative to --config)"
    ),
    repo_name: Optional[str] = typer.Option(None, help="Single repo name (used with --repo-path)"),
    output_dir: Path = typer.Option("./atlas-output", help="Output directory for local storage"),
) -> None:
    """Run the full extract->aggregate->report pipeline locally.

    Supports both local paths and remote URLs in the config YAML.
    For URL entries, set the AZURE_PAT environment variable.
    """
    from atlas.aggregator import aggregate as run_aggregate

    storage = StorageBackend.from_config("local", str(output_dir))

    # Determine repos to process
    repos: list[dict] = []
    if config:
        repos = _load_repos_config(config)
    elif repo_path and repo_name:
        repos = [{"name": repo_name, "path": str(repo_path)}]
    else:
        typer.echo("Error: Provide either --config or both --repo-path and --repo-name", err=True)
        raise typer.Exit(code=1)

    # Process each repo
    results: list[dict] = []
    for repo in repos:
        name = repo["name"]
        local_path = repo.get("path")
        url = repo.get("url")
        clone_dir = None

        try:
            if local_path:
                resolved_path = Path(local_path)
                if not resolved_path.is_absolute():
                    resolved_path = Path.cwd() / resolved_path
                if not resolved_path.exists():
                    raise FileNotFoundError(f"Local path does not exist: {resolved_path}")
                effective_path = resolved_path
            elif url:
                # Clone from URL using AZURE_PAT
                effective_path, clone_dir = _clone_repo(name, url, branch=repo.get("branch"))
            else:
                raise ValueError(f"Repo '{name}' must have either 'path' or 'url'")

            # Build service metadata dict from config entry (strip config-only fields)
            service_data = _extract_service_data(repo)

            # Validate service metadata
            service_yaml = validate_service_yaml(service_data)

            # Run extraction
            extractor = get_extractor(service_yaml.type)
            manifest = extractor.extract(effective_path, service_yaml)

            # Enrich manifest with swagger_url from config (if provided)
            if service_yaml.swagger_url:
                manifest = manifest.model_copy(update={"swagger_url": service_yaml.swagger_url})

            # Write manifest
            manifest_data = json.loads(manifest.model_dump_json())
            storage.write_json(f"services/{name}/manifest.json", manifest_data)

            typer.echo(f"  OK: {name}")
            results.append({"name": name, "status": "success"})

        except Exception as e:
            # Fail-soft: log error, continue with other repos
            error = ExtractionError(
                repo=name,
                timestamp=datetime.now(timezone.utc),
                error=str(e),
                phase="extraction",
            )
            error_data = json.loads(error.model_dump_json())
            storage.write_json(f"services/{name}/extraction-error.json", error_data)
            typer.echo(f"  FAIL: {name} — {e}", err=True)
            results.append({"name": name, "status": "failed", "error": str(e)})

        finally:
            # Clean up clone directory if we created one
            if clone_dir and Path(clone_dir).exists():
                shutil.rmtree(clone_dir, ignore_errors=True)

    # Aggregate
    typer.echo("")
    typer.echo("Aggregating...")
    graph = run_aggregate(storage)
    graph_data = json.loads(graph.model_dump_json())
    storage.write_json("graph/latest.json", graph_data)

    timestamp = datetime.now(timezone.utc).isoformat().replace(":", "-")
    storage.write_json(f"graph/{timestamp}.json", graph_data)

    # Summary
    success = sum(1 for r in results if r["status"] == "success")
    failed = sum(1 for r in results if r["status"] == "failed")
    typer.echo("")
    typer.echo(f"Done: {success} succeeded, {failed} failed out of {len(results)} repos")
    typer.echo(f"Output: {output_dir}")


def _extract_service_data(repo: dict) -> dict:
    """Extract ServiceYaml-relevant fields from a repo config entry.

    Strips config-only keys (path, url, branch) and returns the service metadata dict.
    The 'name' field is kept since ServiceYaml also requires it.
    """
    service_data = {}
    for key, value in repo.items():
        if key not in _CONFIG_ONLY_FIELDS:
            service_data[key] = value
    return service_data


def _load_repos_config(config_path: Path) -> list[dict]:
    """Load and validate a repos config YAML file."""
    if not config_path.exists():
        raise typer.BadParameter(f"Config file not found: {config_path}")

    with open(config_path) as f:
        data = yaml.safe_load(f)

    if not isinstance(data, dict) or "repos" not in data:
        raise typer.BadParameter(f"Config must have a 'repos' key: {config_path}")

    repos = data["repos"]
    for repo in repos:
        if "name" not in repo:
            raise typer.BadParameter(f"Each repo entry must have a 'name': {repo}")
        has_path = "path" in repo
        has_url = "url" in repo
        if not has_path and not has_url:
            raise typer.BadParameter(f"Repo '{repo['name']}' must have either 'path' or 'url'")
        if has_path and has_url:
            raise typer.BadParameter(f"Repo '{repo['name']}' cannot have both 'path' and 'url'")
        # Validate required service metadata fields are present
        missing = _REQUIRED_SERVICE_FIELDS - set(repo.keys())
        if missing:
            raise typer.BadParameter(
                f"Repo '{repo['name']}' is missing required service fields: {', '.join(sorted(missing))}"
            )

    return repos


def _clone_repo(name: str, url: str, branch: str | None = None) -> tuple[Path, str]:
    """Clone a repo from URL using AZURE_PAT.

    Args:
        name: Repo name (used for temp dir prefix and clone subdirectory).
        url: Remote URL to clone from.
        branch: Optional branch name. When set, passes ``--branch <branch>`` to
            ``git clone``. When ``None`` (default), clones the repo's default branch.

    Returns:
        Tuple of (repo_path, temp_dir_path) for cleanup.

    Raises:
        RuntimeError: if AZURE_PAT is not set or clone fails.
    """
    azure_pat = os.environ.get("AZURE_PAT")
    if not azure_pat:
        raise RuntimeError(
            f"AZURE_PAT environment variable required for cloning repo '{name}' from URL. "
            f"Set it or use a local 'path' instead."
        )

    clone_dir = tempfile.mkdtemp(prefix=f"atlas-clone-{name}-")
    clone_path = Path(clone_dir) / name

    auth_url = inject_pat(url, azure_pat)

    clone_cmd = ["git", "clone", "--depth", "1"]
    if branch:
        clone_cmd.extend(["--branch", branch])
    clone_cmd.extend([auth_url, str(clone_path)])

    try:
        result = subprocess.run(
            clone_cmd,
            capture_output=True,
            text=True,
            timeout=120,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
        if result.returncode != 0:
            raise RuntimeError(f"git clone failed for '{name}': {result.stderr.strip()}")
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"git clone timed out for '{name}'")

    return clone_path, clone_dir


@app.command(name="clone-repos")
def clone_repos_cmd(
    config: Path = typer.Option(
        "config/repos-real.yaml", help="Path to source repos config YAML"
    ),
    clone_dir: Path = typer.Option(
        ".repos", help="Directory to clone repos into"
    ),
    output_config: Path = typer.Option(
        "config/repos-local.yaml", help="Path to write generated local config"
    ),
) -> None:
    """Clone all remote repos locally and generate repos-local.yaml.

    Reads repo entries from the source config (default: repos-real.yaml),
    shallow-clones each unique URL into the clone directory, and writes a
    repos-local.yaml that points at the local clones.

    Requires the AZURE_PAT environment variable for Azure DevOps URLs.
    """
    from atlas.repo_cloner import clone_repos, generate_local_config

    # Load config
    repos = _load_repos_config(config)

    # Require AZURE_PAT
    azure_pat = os.environ.get("AZURE_PAT")
    if not azure_pat:
        typer.echo(
            "Error: AZURE_PAT environment variable is required for cloning remote repos.\n"
            "Set it with: export AZURE_PAT=your-pat-here",
            err=True,
        )
        raise typer.Exit(code=1)

    # Clone
    typer.echo(f"Cloning {len(repos)} repos into {clone_dir}/ ...")
    result = clone_repos(repos, clone_dir, azure_pat)

    # Print per-repo status
    for status in result.statuses:
        if status.status == "cloned":
            typer.echo(f"  CLONED:  {status.name} -> {status.path}")
        elif status.status == "skipped-duplicate":
            typer.echo(f"  DEDUP:   {status.name} -> {status.path}")
        elif status.status == "failed":
            typer.echo(f"  FAILED:  {status.name} — {status.error}", err=True)

    # Generate local config
    local_yaml = generate_local_config(repos, clone_dir, result.url_to_clone_name)
    output_config.parent.mkdir(parents=True, exist_ok=True)
    output_config.write_text(local_yaml)
    typer.echo(f"\nWrote {output_config}")

    # Summary
    typer.echo(
        f"\nDone: {result.cloned} cloned, {result.skipped} deduped, "
        f"{result.failed} failed out of {len(repos)} repos"
    )

    if result.failed > 0:
        raise typer.Exit(code=1)


@app.command(name="mcp-server")
def mcp_server(
    mode: str = typer.Option("stdio", help="Server mode: stdio or http"),
    storage_backend: str = typer.Option("local", help="Storage backend: local or gcs"),
    storage_bucket: str = typer.Option(..., help="Storage bucket or directory path"),
    host: str = typer.Option("0.0.0.0", help="HTTP server host (http mode only)"),
    port: int = typer.Option(8000, help="HTTP server port (http mode only)"),
) -> None:
    """Start the MCP server."""
    typer.echo(f"Starting MCP server in {mode} mode...", err=True)
    typer.echo(f"Storage: {storage_backend}://{storage_bucket}", err=True)

    # Import and start the server
    from mcp_server.server import create_server

    server = create_server(
        storage_backend=storage_backend,
        storage_bucket=storage_bucket,
    )

    if mode == "stdio":
        import asyncio
        import sys

        import structlog

        # In stdio mode stdout must carry only JSON-RPC frames.
        # Redirect structlog (and stdlib logging) to stderr so log lines
        # never corrupt the MCP transport.
        structlog.configure(
            logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        )

        asyncio.run(server.run_stdio())
    elif mode == "http":
        import asyncio

        asyncio.run(server.run_http(host=host, port=port))
    else:
        typer.echo(f"Unknown mode: {mode}. Use 'stdio' or 'http'.", err=True)
        raise typer.Exit(code=1)


@app.callback()
def main() -> None:
    """Platform Atlas — structured architectural metadata for AI agents."""
    pass


if __name__ == "__main__":
    app()
