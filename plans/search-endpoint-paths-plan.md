# Plan: Search Endpoint Paths and DTO Names in `_score_service()`

## Problem

The [`_score_service()`](mcp_server/server.py:697) function scores services against query tokens by searching: name, keywords, purpose, domain, gradle_plugins, and modules. It does **not** search endpoint paths or DTO names, causing services to be missed when the most relevant signal is in their API surface.

**Example:** Searching for "zipcode" fails to find `geoinfo-microservice`, which has endpoint `POST /v1/zipcode/validate-country` with DTOs `ValidateCountryRequest` and `ValidateCountryResponse`.

## Data Available in the Graph

Each service in the graph has an [`endpoints`](src/cortex/schema.py:297) list of [`EndpointIndex`](src/cortex/schema.py:166) objects. Each endpoint contains:

```
method: str | None          — "GET", "POST", etc.
path: str | None            — "/v1/zipcode/validate-country"
tags: list[str]             — ["zipcode"]
request_body.type: str      — "ValidateCountryRequest"
response.type: str          — "ValidateCountryResponse"
parameters[].name: str      — "staffMemberId"
```

Real example from [`cortex-output/graph/latest.json`](cortex-output/graph/latest.json:11078):
```json
{
  "method": "POST",
  "path": "/v1/zipcode/validate-country",
  "tags": ["zipcode"],
  "request_body": { "type": "ValidateCountryRequest", "required": true },
  "response": { "type": "ValidateCountryResponse", "wrapper": null }
}
```

## Implementation Plan

### 1. Add a `_tokenize_camel_case()` helper

DTO names like `ValidateCountryRequest` and path segments like `validate-country` need different tokenization than the existing [`_tokenize()`](mcp_server/server.py:691).

Create a new helper function `_tokenize_identifier()` that:
- Splits camelCase/PascalCase into words: `ValidateCountryRequest` → `{validate, country, request}`
- Splits on `/`, `-`, `_`, `.` for paths: `/v1/zipcode/validate-country` → `{v1, zipcode, validate, country}`
- Lowercases everything
- Removes stop words and single-character tokens (reuse existing `STOP_WORDS`)
- Filters out common noise tokens specific to this context: `v1`, `v2`, `api`, `request`, `response`, `dto`, `controller` (add these to a separate `ENDPOINT_NOISE_WORDS` set, not to the global `STOP_WORDS`)

```python
# Regex to split camelCase/PascalCase
_CAMEL_SPLIT_RE = re.compile(r'(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])')

def _tokenize_identifier(text: str) -> set[str]:
    """Tokenize an identifier: split camelCase, kebab-case, paths, remove noise."""
    # Split on /, -, _, . first
    parts = re.split(r'[/\-_.]', text)
    words: list[str] = []
    for part in parts:
        # Split camelCase within each part
        sub_parts = _CAMEL_SPLIT_RE.split(part)
        words.extend(sub_parts)
    lowered = {w.lower() for w in words if len(w) > 1}
    return lowered - STOP_WORDS - _ENDPOINT_NOISE_WORDS
```

### 2. Add two new scoring blocks to `_score_service()`

Add these after the existing module scoring block (line 749), before the `return` statement:

#### a. Endpoint paths — weight: 2.0x

Endpoint paths contain high-signal domain terms. A match on path tokens is as valuable as a keyword match.

```python
# Endpoint path match: high weight (2.0x)
path_tokens: set[str] = set()
for ep in svc.get("endpoints", []):
    ep_path = ep.get("path", "") or ""
    path_tokens |= _tokenize_identifier(ep_path)
path_overlap = query_tokens & path_tokens
if path_overlap:
    score += len(path_overlap) * 2.0
    matched_on.append("endpoint_paths")
```

#### b. Endpoint DTO names — weight: 1.5x

DTO names encode domain concepts. `ValidateCountryRequest` contains "validate" and "country" — useful for discovery. Weight slightly lower than paths since DTO names also contain structural noise (Request, Response, Dto).

```python
# Endpoint DTO name match: medium weight (1.5x)
dto_tokens: set[str] = set()
for ep in svc.get("endpoints", []):
    req_body = ep.get("request_body")
    if req_body and req_body.get("type"):
        dto_tokens |= _tokenize_identifier(req_body["type"])
    resp = ep.get("response")
    if resp and resp.get("type"):
        dto_tokens |= _tokenize_identifier(resp["type"])
dto_overlap = query_tokens & dto_tokens
if dto_overlap:
    score += len(dto_overlap) * 1.5
    matched_on.append("endpoint_dtos")
```

### 3. Weight Summary

| Field | Weight | Rationale |
|-------|--------|-----------|
| `name` | 3.0x | Existing — service name is strongest signal |
| `keywords` | 2.0x | Existing — human-curated terms |
| **`endpoint_paths`** | **2.0x** | **New — path segments are high-signal domain terms** |
| `purpose` | 1.5x | Existing — free-text description |
| `domain` | 1.5x | Existing — domain classification |
| `gradle_plugins` | 1.5x | Existing — technology stack |
| **`endpoint_dtos`** | **1.5x** | **New — DTO names encode domain concepts** |
| `modules` | 1.0x | Existing — module names |

### 4. Endpoint Noise Words

Define a small set of tokens to filter from endpoint/DTO tokenization that would cause false positives:

```python
_ENDPOINT_NOISE_WORDS = frozenset({
    "v1", "v2", "v3", "v4", "api",
    "request", "response", "dto",
    "controller", "handler",
})
```

These are structural terms that appear in nearly every service and carry no discriminating signal.

### 5. Edge Cases

| Edge Case | Handling |
|-----------|----------|
| Service with no endpoints (mobile apps) | `svc.get("endpoints", [])` returns empty list — no tokens added, no score change |
| Endpoint with `null` path | `ep.get("path", "") or ""` handles None safely |
| Endpoint with no request_body or response | Guarded by `if req_body and req_body.get("type")` checks |
| Path parameters like `{staffMemberId}` | `_tokenize_identifier` will split on `{` and `}` via the regex, producing tokens like `staffmemberid`, `staff`, `member`, `id` — `id` is filtered by length, rest are valid |
| Version prefixes `v1`, `v2` | Filtered by `_ENDPOINT_NOISE_WORDS` |
| Generic DTO names like `BaseRequest` | `request` filtered by noise words; `base` is a valid but low-value token — acceptable |
| CamelCase with acronyms like `NBAWebClient` | `_CAMEL_SPLIT_RE` splits to `NBA`, `Web`, `Client` → lowered to `nba`, `web`, `client` |

### 6. Files to Modify

| File | Change |
|------|--------|
| [`mcp_server/server.py`](mcp_server/server.py) | Add `_CAMEL_SPLIT_RE`, `_ENDPOINT_NOISE_WORDS`, `_tokenize_identifier()`, and two new scoring blocks in `_score_service()` |
| [`mcp_server/tests/test_mcp_tools.py`](mcp_server/tests/test_mcp_tools.py) | Add tests for new scoring and tokenization |

### 7. Test Cases to Add

#### Unit tests for `_tokenize_identifier`

```python
class TestTokenizeIdentifier:
    def test_camel_case_splitting(self):
        tokens = _tokenize_identifier("ValidateCountryRequest")
        assert "validate" in tokens
        assert "country" in tokens
        assert "request" not in tokens  # filtered as noise

    def test_path_splitting(self):
        tokens = _tokenize_identifier("/v1/zipcode/validate-country")
        assert "zipcode" in tokens
        assert "validate" in tokens
        assert "country" in tokens
        assert "v1" not in tokens  # filtered as noise

    def test_empty_string(self):
        assert _tokenize_identifier("") == set()

    def test_acronym_handling(self):
        tokens = _tokenize_identifier("NBAWebClient")
        assert "nba" in tokens
        assert "web" in tokens
        assert "client" in tokens
```

#### Unit tests for `_score_service` with endpoint data

```python
def test_endpoint_path_match(self):
    svc = {
        "name": "geoinfo-microservice",
        "keywords": [],
        "purpose": "Geo info service",
        "domain": "geo-info",
        "endpoints": [
            {"method": "POST", "path": "/v1/zipcode/validate-country",
             "request_body": {"type": "ValidateCountryRequest"},
             "response": {"type": "ValidateCountryResponse"}}
        ],
    }
    score, matched = _score_service(svc, {"zipcode"})
    assert score > 0
    assert "endpoint_paths" in matched

def test_endpoint_dto_match(self):
    svc = {
        "name": "some-service",
        "keywords": [],
        "purpose": "Some service",
        "domain": "misc",
        "endpoints": [
            {"method": "POST", "path": "/api/orders",
             "request_body": {"type": "CreateOrderRequest"},
             "response": {"type": "OrderDto"}}
        ],
    }
    score, matched = _score_service(svc, {"order"})
    assert score > 0
    assert "endpoint_dtos" in matched

def test_no_endpoints_no_crash(self):
    svc = {
        "name": "mobile-app",
        "keywords": [],
        "purpose": "Mobile app",
        "domain": "mobile",
    }
    score, matched = _score_service(svc, {"zipcode"})
    # Should not crash, just no endpoint-related score
    assert "endpoint_paths" not in matched
    assert "endpoint_dtos" not in matched
```

#### Integration test for `find_relevant_services`

Add a service with endpoints to the test fixture graph and verify it is found:

```python
def test_find_service_by_endpoint_path(self, ...):
    """Service is found when query matches an endpoint path token."""
    # Add a service with endpoints to the fixture graph
    # Search for "zipcode"
    # Assert the service appears in candidates
```

### 8. Verification

After implementation, run:

```bash
# Existing tests must still pass
uv run pytest mcp_server/tests/test_mcp_tools.py -v

# Full test suite
uv run pytest tests/ mcp_server/tests/ -v

# Manual verification against real data
uv run cortex mcp-server --mode stdio --storage-backend local --storage-bucket ./cortex-output
# Then query: find_relevant_services("zipcode")
# Expected: geoinfo-microservice appears in results
```

### 9. Implementation Checklist

- [ ] Add `_CAMEL_SPLIT_RE` regex constant to `mcp_server/server.py`
- [ ] Add `_ENDPOINT_NOISE_WORDS` frozenset to `mcp_server/server.py`
- [ ] Add `_tokenize_identifier()` function to `mcp_server/server.py`
- [ ] Add endpoint path scoring block to `_score_service()` with weight 2.0x
- [ ] Add endpoint DTO name scoring block to `_score_service()` with weight 1.5x
- [ ] Add `TestTokenizeIdentifier` test class to `test_mcp_tools.py`
- [ ] Add endpoint path/DTO scoring tests to `TestScoreService` class
- [ ] Add integration test for `find_relevant_services` with endpoint-bearing service in fixture
- [ ] Import `_tokenize_identifier` in test file
- [ ] Run full test suite and verify all pass
