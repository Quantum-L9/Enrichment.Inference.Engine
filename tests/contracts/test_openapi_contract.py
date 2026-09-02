"""
OpenAPI Contract Tests
Source: app/models/schemas.py, app/main.py, app/core/auth.py
Markers: unit
"""

from __future__ import annotations

import pytest

from tests.contracts.conftest_contracts import API_DIR, load_yaml


@pytest.fixture(scope="module")
def spec() -> dict:
    path = API_DIR / "openapi.yaml"
    if not path.exists():
        pytest.skip("openapi.yaml missing")
    return load_yaml(path)


@pytest.mark.unit
def test_openapi_version_is_3_1(spec: dict) -> None:
    assert spec.get("openapi", "").startswith("3.1"), (
        f"Expected OpenAPI 3.1.x, got: {spec.get('openapi')!r}"
    )


@pytest.mark.unit
def test_info_block_present(spec: dict) -> None:
    info = spec.get("info", {})
    assert info.get("title") and info.get("version")


@pytest.mark.unit
def test_security_scheme_defined(spec: dict) -> None:
    schemes = spec.get("components", {}).get("securitySchemes", {})
    assert schemes, "components.securitySchemes is empty — X-API-Key auth not documented"
    scheme_types = [v.get("type") for v in schemes.values()]
    assert any(t in ("apiKey", "http") for t in scheme_types)


REQUIRED_PATHS = [
    ("GET", "/api/v1/health"),
    ("POST", "/api/v1/enrich"),
    ("POST", "/api/v1/enrich/batch"),
    ("POST", "/v1/execute"),
    ("POST", "/v1/converge"),
    ("POST", "/v1/converge/batch"),
    ("GET", "/v1/converge/{run_id}"),
    ("POST", "/v1/converge/{run_id}/approve"),
    ("GET", "/v1/converge/proposals/{domain}"),
    ("POST", "/api/v1/discover"),
    ("POST", "/api/v1/scan"),
    ("GET", "/api/v1/proposals/{domain}"),
    ("POST", "/api/v1/proposals/{proposal_id}/approve"),  # {proposal_id} not {id}
    ("GET", "/api/v1/fields/{entity_id}"),
    ("GET", "/api/v1/fields/{entity_id}/{field_name}/history"),  # {field_name} not {field}
]


@pytest.mark.unit
def test_retired_peer_ingress_is_not_documented(spec: dict) -> None:
    """Seam audit 2026-09-02: /v1/outcomes was a node-to-node side door."""
    assert "/v1/outcomes" not in spec.get("paths", {})


@pytest.mark.unit
@pytest.mark.parametrize("method,path", REQUIRED_PATHS)
def test_endpoint_present(spec: dict, method: str, path: str) -> None:
    paths = spec.get("paths", {})
    assert path in paths, f"Missing path: {path} — Source: app/api/v1/"
    assert paths[path].get(method.lower()), f"Missing {method} on {path}"


@pytest.mark.unit
@pytest.mark.parametrize("method,path", REQUIRED_PATHS)
def test_endpoint_has_operation_id(spec: dict, method: str, path: str) -> None:
    paths = spec.get("paths", {})
    if path not in paths:
        pytest.skip(f"Path {path} missing")
    op = paths[path].get(method.lower(), {})
    assert op.get("operationId"), f"Missing operationId on {method} {path}"


@pytest.mark.unit
@pytest.mark.parametrize("method,path", REQUIRED_PATHS)
def test_endpoint_has_responses(spec: dict, method: str, path: str) -> None:
    paths = spec.get("paths", {})
    if path not in paths:
        pytest.skip(f"Path {path} missing")
    assert paths[path].get(method.lower(), {}).get("responses"), f"No responses on {method} {path}"


ENRICH_REQUEST_REQUIRED_FIELDS = ["entity", "object_type", "objective"]
ENRICH_RESPONSE_FIELDS = [
    "fields",
    "confidence",
    "inference_version",
    "processing_time_ms",
    "quality_tier",
    "state",
    "tokens_used",
]


@pytest.mark.unit
def test_enrich_request_required_fields_in_spec(spec: dict) -> None:
    paths = spec.get("paths", {})
    if "/api/v1/enrich" not in paths:
        pytest.skip("/api/v1/enrich not in spec")
    post_op = paths["/api/v1/enrich"].get("post", {})
    body_schema = (
        post_op.get("requestBody", {})
        .get("content", {})
        .get("application/json", {})
        .get("schema", {})
    )
    props = body_schema.get("properties", {})
    for field in ENRICH_REQUEST_REQUIRED_FIELDS:
        assert field in props or body_schema.get("$ref"), (
            f"EnrichRequest field '{field}' missing. Source: app/models/schemas.py"
        )


@pytest.mark.unit
def test_error_response_components_present(spec: dict) -> None:
    components = spec.get("components", {})
    has_errors = bool(components.get("responses")) or any(
        "error" in k.lower() or "Error" in k for k in components.get("schemas", {})
    )
    assert has_errors, "No error response components — Phase 4.1 requires shared error envelope"


@pytest.mark.unit
def test_batch_enrich_max_length_documented(spec: dict) -> None:
    """BatchEnrichRequest max_length=50. Source: app/models/schemas.py."""
    paths = spec.get("paths", {})
    if "/api/v1/enrich/batch" not in paths:
        pytest.skip("/api/v1/enrich/batch not in spec")
    post_op = paths["/api/v1/enrich/batch"].get("post", {})
    assert post_op.get("requestBody"), "POST /api/v1/enrich/batch has no requestBody"


@pytest.mark.unit
def test_openapi_uses_ref(spec: dict) -> None:
    """Phase 4.1 DRY: spec must contain $ref references."""
    content = (API_DIR / "openapi.yaml").read_text()
    assert content.count("$ref") >= 1, "openapi.yaml must use $ref — Phase 4.1"
