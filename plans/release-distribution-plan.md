# Platform Atlas — Release Distribution Plan

## Goal

Distribute the `atlas` CLI as a pre-built Python wheel so that Azure Pipelines can install and run it **without any access to the source repository** (memory-hub on GitHub).

---

## Architecture Overview

```
┌─────────────────────────┐         ┌──────────────────────────┐         ┌──────────────────────────┐
│  GitHub (private)        │         │  Azure Artifacts Feed    │         │  Azure Pipeline (agents) │
│  memory-hub repo         │────────▶│  (private Python feed)   │◀────────│  Extraction / Aggregate  │
│                          │  push   │                          │  pull   │                          │
│  Source code lives here  │  .whl   │  atlas-X.Y.Z.whl         │  pip    │  pip install atlas       │
│  GitHub Actions build &  │         │  (+ transitive deps      │  install│  No repo access needed   │
│  publish on git tags     │         │   from PyPI)             │         │                          │
└─────────────────────────┘         └──────────────────────────┘         └──────────────────────────┘
```

- **GitHub** — Source of truth. Source code never leaves here.
- **Azure Artifacts** — Acts as a private PyPI mirror. Receives the built wheel.
- **Azure Pipelines** — Only sees a published package. Zero knowledge of GitHub.

---

## One-Time Setup (Manual — Done Once by a Team Member)

### Step 1: Create an Azure Artifacts Python Feed

1. Go to your Azure DevOps organization: `https://dev.azure.com/YOUR_ORG`
2. Navigate to **Artifacts** → **Create Feed**
3. Settings:
   - **Name:** `platform-atlas` (or choose any name — update configs below to match)
   - **Visibility:** Private (organization-scoped)
   - **Upstream sources:** ✅ Enable (allows deps to be pulled from PyPI via the feed)
4. Click **Create**

> **Feed URL Pattern:**
> - Upload endpoint: `https://pkgs.dev.azure.com/YOUR_ORG/_packaging/platform-atlas/pypi/upload/`
> - Install index: `https://pkgs.dev.azure.com/YOUR_ORG/_packaging/platform-atlas/pypi/simple/`

---

### Step 2: Create an Azure DevOps PAT for Publishing

1. In Azure DevOps, click your profile icon → **Personal Access Tokens**
2. Click **New Token**
3. Settings:
   - **Name:** `github-atlas-publish`
   - **Expiration:** 1 year (set a calendar reminder to rotate)
   - **Scopes → Packaging:** ✅ Read & Write
4. Copy the PAT immediately — it will not be shown again

---

### Step 3: Create an Azure DevOps PAT for Azure Pipeline Agents

> If your pipeline agents already have implicit access to the Artifacts feed within the same org, you can skip this step and use the built-in `$(System.AccessToken)` instead.

1. Create another PAT:
   - **Name:** `pipeline-atlas-install`
   - **Scopes → Packaging:** ✅ Read
2. Store it in the `platform-atlas-secrets` Key Vault variable group (already referenced in the pipeline):
   - Variable name: `AZURE_ARTIFACTS_PAT`

---

### Step 4: Add GitHub Actions Secret

1. In GitHub, go to **memory-hub** repo → **Settings** → **Secrets and variables** → **Actions**
2. Click **New repository secret**
3. Name: `AZURE_ARTIFACTS_PAT`
4. Value: The PAT created in Step 2 (with Packaging Read & Write)
5. Click **Add secret**

---

## Files to Create / Modify

### New: `.github/workflows/release.yml` — Automated GitHub Actions Release

Triggered automatically when a git tag matching `v*` is pushed.

```yaml
name: Build and Publish to Azure Artifacts

on:
  push:
    tags:
      - 'v*'          # Triggers on v1.0.0, v1.1.0, v2.0.0, etc.

jobs:
  build-and-publish:
    name: Build wheel and publish to Azure Artifacts
    runs-on: ubuntu-latest

    steps:
      - name: Checkout source
        uses: actions/checkout@v4

      - name: Set up uv
        uses: astral-sh/setup-uv@v4
        with:
          version: "latest"

      - name: Set up Python 3.12
        run: uv python install 3.12

      - name: Install build dependencies
        run: uv sync --extra dev

      - name: Build wheel and sdist
        run: uv build
        # Produces: dist/atlas-X.Y.Z-py3-none-any.whl
        #           dist/atlas-X.Y.Z.tar.gz

      - name: Verify build artifact
        run: |
          ls -lh dist/
          echo "Built artifacts:"
          for f in dist/*; do echo "  $f"; done

      - name: Publish to Azure Artifacts
        env:
          AZURE_ARTIFACTS_PAT: ${{ secrets.AZURE_ARTIFACTS_PAT }}
          AZURE_ORG: YOUR_ORG          # ← Replace with your Azure DevOps org name
          FEED_NAME: platform-atlas    # ← Replace if you used a different feed name
        run: |
          pip install twine
          twine upload \
            --repository-url "https://pkgs.dev.azure.com/${AZURE_ORG}/_packaging/${FEED_NAME}/pypi/upload/" \
            --username az \
            --password "${AZURE_ARTIFACTS_PAT}" \
            --non-interactive \
            dist/*

      - name: Create GitHub Release
        uses: softprops/action-gh-release@v2
        with:
          files: dist/*
          generate_release_notes: true
```

---

### New: `Makefile` — Manual Build and Publish Targets

For local development and one-off manual releases.

```makefile
.PHONY: build publish clean test

AZURE_ORG    ?= YOUR_ORG          # Override: make publish AZURE_ORG=myorg
FEED_NAME    ?= platform-atlas    # Override: make publish FEED_NAME=myfeed

# Build the wheel and sdist
build:
	uv build
	@echo ""
	@echo "Built artifacts:"
	@ls -lh dist/

# Run tests before publishing
test:
	uv run pytest tests/ mcp_server/tests/ -v

# Publish to Azure Artifacts (requires AZURE_ARTIFACTS_PAT env var)
publish: test build
	@if [ -z "$$AZURE_ARTIFACTS_PAT" ]; then \
		echo "ERROR: AZURE_ARTIFACTS_PAT environment variable is not set."; \
		echo "Set it with: export AZURE_ARTIFACTS_PAT=your-pat-here"; \
		exit 1; \
	fi
	pip install --quiet twine
	twine upload \
		--repository-url "https://pkgs.dev.azure.com/$(AZURE_ORG)/_packaging/$(FEED_NAME)/pypi/upload/" \
		--username az \
		--password "$$AZURE_ARTIFACTS_PAT" \
		--non-interactive \
		dist/*
	@echo ""
	@echo "Published successfully to Azure Artifacts feed: $(FEED_NAME)"

# Build only (skip tests) — for quick iteration
build-only:
	uv build

# Clean build artifacts
clean:
	rm -rf dist/ build/ *.egg-info
```

**Usage:**
```bash
# Set the PAT
export AZURE_ARTIFACTS_PAT=your-pat-here

# Full release (runs tests → builds → publishes)
make publish AZURE_ORG=myorg FEED_NAME=platform-atlas

# Or build only (no publish)
make build
```

---

### Modified: `pipelines/azure-pipelines.yml` — Remove Source Checkout

Replace `checkout: self` + `uv sync` with `pip install atlas` from the feed.

**Before (current):**
```yaml
steps:
  - checkout: self

  - task: UsePythonVersion@0
    inputs:
      versionSpec: '3.12'
    displayName: Use Python 3.12

  - script: |
      pip install uv
      uv sync
    displayName: Install dependencies
```

**After (updated):**
```yaml
steps:
  - checkout: none    # No source repo access needed

  - task: UsePythonVersion@0
    inputs:
      versionSpec: '3.12'
    displayName: Use Python 3.12

  - script: |
      pip install atlas \
        --index-url "https://pkgs.dev.azure.com/$(AZURE_ORG)/_packaging/$(FEED_NAME)/pypi/simple/" \
        --extra-index-url "https://pypi.org/simple/" \
        --extra-index-url "https://pypi.org/simple/"
    displayName: Install atlas from Azure Artifacts
    env:
      PIP_USERNAME: az
      PIP_PASSWORD: $(AZURE_ARTIFACTS_PAT)   # From Key Vault variable group
```

Also add these to the pipeline `variables` section:
```yaml
variables:
  - group: platform-atlas-secrets    # Already exists — add AZURE_ARTIFACTS_PAT here
  - name: STORAGE_BACKEND
    value: gcs
  - name: STORAGE_BUCKET
    value: platform-atlas-prod
  - name: AZURE_ORG
    value: YOUR_ORG                  # ← Replace with your org
  - name: FEED_NAME
    value: platform-atlas            # ← Replace with your feed name
```

> **Note:** If the pipeline agents are in the same Azure DevOps org as the feed, you can use `$(System.AccessToken)` as the password instead of a separate PAT, which avoids PAT rotation entirely:
> ```yaml
> env:
>   PIP_USERNAME: az
>   PIP_PASSWORD: $(System.AccessToken)
> ```

---

## Version Management Strategy

### Manual (Current — Recommended to Start)

1. Bump the version in `pyproject.toml`:
   ```toml
   [project]
   version = "1.1.0"   # ← Bump this
   ```
2. Commit and push:
   ```bash
   git add pyproject.toml
   git commit -m "chore: bump version to 1.1.0"
   git tag v1.1.0
   git push origin main --tags
   ```
3. GitHub Actions triggers automatically on the tag push.

### Automated (Future — Optional Upgrade)

Add `hatch-vcs` so the version is derived automatically from git tags, eliminating manual `pyproject.toml` edits:

```toml
# pyproject.toml
[project]
dynamic = ["version"]   # Remove static version

[build-system]
requires = ["hatchling", "hatch-vcs"]
build-backend = "hatchling.build"

[tool.hatch.version]
source = "vcs"          # Reads version from git tags
```

With this, just tag and push — no `pyproject.toml` edits needed:
```bash
git tag v1.1.0
git push origin main --tags
# GitHub Actions builds v1.1.0 automatically
```

---

## Release Workflow Summary

### Automated Path (Normal Releases)

```
1. Develop and merge to main
2. git tag v1.x.x && git push --tags
3. GitHub Actions triggers:
   ├── Checkout source
   ├── uv build (produces dist/atlas-1.x.x-py3-none-any.whl)
   ├── twine upload → Azure Artifacts feed
   └── GitHub Release created with wheel attached
4. Azure Pipelines (next nightly run):
   ├── checkout: none
   ├── pip install atlas (from Azure Artifacts feed)
   └── uv run atlas extract / aggregate / report
```

### Manual Path (Hotfix or First Release)

```bash
# 1. Build
export AZURE_ARTIFACTS_PAT=your-pat-here
make build AZURE_ORG=myorg FEED_NAME=platform-atlas

# 2. Inspect
ls dist/   # atlas-1.0.0-py3-none-any.whl

# 3. Publish
make publish AZURE_ORG=myorg FEED_NAME=platform-atlas
```

---

## Security Notes

| Secret | Where Stored | Who Needs It | Scope |
|--------|-------------|-------------|-------|
| `AZURE_ARTIFACTS_PAT` (publish) | GitHub Actions Secrets | GitHub Actions only | Packaging: Read & Write |
| `AZURE_ARTIFACTS_PAT` (install) | Azure Key Vault / variable group | Azure Pipeline agents | Packaging: Read only |
| `GIT_PAT` (target repo clone) | Azure Key Vault / variable group | Azure Pipeline agents | Code: Read |

- The GitHub repo remains private and inaccessible from Azure
- Azure Artifacts PAT for publishing has **write-only** scope — even if compromised, it cannot read source code
- Rotate PATs every 12 months (set calendar reminders)

---

## Validation Checklist (Post-Setup)

- [ ] Azure Artifacts feed created and accessible
- [ ] PAT for publishing stored in GitHub Actions secrets as `AZURE_ARTIFACTS_PAT`
- [ ] PAT for installing stored in Azure Key Vault variable group
- [ ] `make build` succeeds locally and produces `dist/atlas-*.whl`
- [ ] `make publish` successfully uploads the wheel to the feed
- [ ] `pip install atlas --index-url https://pkgs.dev.azure.com/...` succeeds on a test machine
- [ ] `atlas --version` or `atlas --help` works after `pip install`
- [ ] Tag `v1.0.0` pushed → GitHub Actions workflow triggers and succeeds
- [ ] Azure Pipeline (test run) installs from feed with `checkout: none` successfully
- [ ] Nightly pipeline runs without `checkout: self` and extractions succeed

---

## Files Changed Summary

| File | Action | Purpose |
|------|--------|---------|
| `.github/workflows/release.yml` | **Create** | Automated build + publish on git tags |
| `Makefile` | **Create** | Manual build + publish targets |
| `pipelines/azure-pipelines.yml` | **Modify** | Remove `checkout: self`, install from feed |
| `pyproject.toml` | **Optional** | Add `hatch-vcs` for automatic versioning |
| `plans/release-distribution-plan.md` | **Create** | This document |
