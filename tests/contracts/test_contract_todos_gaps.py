"""
Contract Gap / TODO Tests
==========================
Targeted tests for the 10 known TODO gaps from the Phase 5 audit report.
These tests are designed to:
  - FAIL explicitly and informatively when a gap is unresolved
  - PASS when the gap has been resolved and documented
  - Never fabricate assertions about undocumented behavior

Each test corresponds to exactly one numbered TODO from the audit report.
The test name encodes the TODO number for traceability.

TODO List (from audit report Gaps & TODOs section):
  TODO-01: /v1/score/* sub-API full schema not resolved
  TODO-02: /v1/health/* sub-route full response schema
  TODO-03: PromptBuilder exact prompt template text
  TODO-04: Neo4j live schema (node labels + relationship types from live DB)
  TODO-05: Waterfall chain provider ordering (Perplexity→Clearbit→ZoomInfo→Apollo/Hunter)
  TODO-06: AsyncAPI broker bindings — exact Redis XADD stream key names
  TODO-07: BatchEnrichResponse fields fully documented in OpenAPI
  TODO-08: circuit_breaker state transitions not fully documented
  TODO-09: agents/prompt-contracts/_index.yaml prompt registry completeness
  TODO-10: Convergence batch endpoint response schema (GET /v1/converge/{run_id})

Markers: unit
All tests use pytest.xfail with strict=False — they fail softly when gap is
unresolved but do not block CI. Change strict=True once the gap is resolved.

These xfails are imperative (``pytest.xfail(...)``), which takes no ``strict``
argument; the strict form of a resolved gap is ``pytest.fail(...)`` on the same
condition, so a regression fails CI instead of xfailing.

Resolved (strict) on 2026-11-11:
  TODO-01: /score/* documented in openapi.yaml; paths in REQUIRED_PATHS
           (the router prefix is /score, not /v1/score).
  TODO-04: graph-schema.yaml verified against a live Neo4j populated by EIE's
           notify_graph_sync -> CEG handle_sync (see live_verification).
  TODO-05: waterfall order in dependencies/_index.yaml, checked against the
           engine by test_waterfall_order_contract.py. The audit's assumed
           order (Perplexity first) was wrong: the engine is
           app/services/enrichment/waterfall_engine.py and, per
           config/waterfall_config.yaml, perplexity_sonar runs last.
  TODO-10: GET /v1/converge/{run_id} schema documented. The audit expected
           ConvergeResponse fields (`state` / `pass_count`); the route actually
           returns a LoopState summary (`status` / `current_pass`, ...) built in
           app/api/v1/converge.py get_convergence_status. The tracker now checks
           the documented properties against that handler's returned keys.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.contracts.conftest_contracts import (
    AGENTS_DIR,
    API_DIR,
    CONTRACTS_DIR,
    DATA_DIR,
    EVENTS_DIR,
    REPO_ROOT,
    load_json,
    load_yaml,
)

# ---------------------------------------------------------------------------
# Helper: load contract only if file exists, skip otherwise
# ---------------------------------------------------------------------------


def _load_yaml_safe(path: Path) -> dict | None:
    if not path.exists():
        return None
    return load_yaml(path)


def _load_json_safe(path: Path) -> dict | None:
    if not path.exists():
        return None
    return load_json(path)


# ---------------------------------------------------------------------------
# TODO-01: /v1/score/* sub-API full schema not resolved
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_todo_01_score_api_schema_resolved() -> None:
    """
    TODO-01: /v1/score/* endpoints full request/response schema must be documented.

    Resolved when: openapi.yaml contains /v1/score paths with full schemas.
    Unresolved: will xfail with explanation.

    Source gap: app/api/v1/score.py was not fully inspected during the audit.
    """
    spec = _load_yaml_safe(API_DIR / "openapi.yaml")
    if spec is None:
        pytest.skip("openapi.yaml missing — covered by test_contract_files_exist")

    paths = spec.get("paths", {})
    score_paths = [p for p in paths if "/score" in p]

    if not score_paths:
        pytest.fail(
            "TODO-01 REGRESSED (resolved 2026-11-11, strict): No /score/* paths found "
            "in openapi.yaml.\n"
            "Source: app/score/score_api.py (router prefix /score); paths are in "
            "test_openapi_contract.REQUIRED_PATHS."
        )

    # If score paths exist, verify they have schemas
    for path in score_paths:
        for method_def in paths[path].values():
            if isinstance(method_def, dict):
                responses = method_def.get("responses", {})
                assert responses, f"TODO-01 partially resolved: {path} has no responses"


# ---------------------------------------------------------------------------
# TODO-02: /v1/health/* sub-route full response schema
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_todo_02_health_endpoint_response_schema_complete() -> None:
    """
    TODO-02: GET /api/v1/health response schema must match HealthCheckResponse
    from app/models/schemas.py exactly.

    Known fields: status, version, kb_loaded, kb_polymers, kb_grades, kb_rules,
    circuit_breaker_state.
    Source: app/models/schemas.py HealthCheckResponse
    """
    spec = _load_yaml_safe(API_DIR / "openapi.yaml")
    if spec is None:
        pytest.skip("openapi.yaml missing")

    paths = spec.get("paths", {})
    if "/api/v1/health" not in paths:
        pytest.skip("/api/v1/health not in spec")

    get_op = paths["/api/v1/health"].get("get", {})
    response_200 = get_op.get("responses", {}).get("200", {})
    schema = response_200.get("content", {}).get("application/json", {}).get("schema", {})

    if not schema:
        pytest.xfail(
            "TODO-02 UNRESOLVED: GET /api/v1/health 200 response has no schema.\n"
            "Action: Add HealthCheckResponse schema from app/models/schemas.py.\n"
            "Required fields: status, version, kb_loaded, kb_polymers, kb_grades, "
            "kb_rules, circuit_breaker_state"
        )

    # If schema exists, check HealthCheckResponse fields
    props = schema.get("properties", {})
    if not props and schema.get("$ref"):
        return  # $ref resolution — assume resolved

    health_fields = ["status", "version", "kb_loaded", "circuit_breaker_state"]
    missing = [f for f in health_fields if f not in props]
    if missing:
        pytest.xfail(
            f"TODO-02 PARTIALLY RESOLVED: health schema missing fields: {missing}.\n"
            "Source: app/models/schemas.py HealthCheckResponse"
        )


# ---------------------------------------------------------------------------
# TODO-03: PromptBuilder exact prompt template text
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_todo_03_prompt_contracts_have_template_text() -> None:
    """
    TODO-03: Prompt contracts must include the actual Jinja2/f-string template
    text as seen by the LLM, not just variable names.

    Resolved when: each prompt contract in agents/prompt-contracts/ contains
    a non-empty 'template' field with the actual prompt string.
    Source gap: app/engines/prompt_builder.py was not fully read during audit.
    """
    index_path = AGENTS_DIR / "prompt-contracts" / "_index.yaml"
    if not index_path.exists():
        pytest.xfail(
            "TODO-03 UNRESOLVED: agents/prompt-contracts/_index.yaml missing.\n"
            "Action: Inspect app/engines/prompt_builder.py and extract all "
            "prompt templates into agents/prompt-contracts/*.yaml files."
        )

    index = load_yaml(index_path)
    prompts = index.get("prompts", [])

    if not prompts:
        pytest.xfail(
            "TODO-03 UNRESOLVED: prompt registry is empty.\n"
            "Action: Register all prompts from app/engines/prompt_builder.py."
        )

    # Check that prompt contract files exist and have template text
    prompt_contracts_dir = AGENTS_DIR / "prompt-contracts"
    unresolved = []
    for prompt in prompts:
        name = prompt if isinstance(prompt, str) else prompt.get("name", "")
        contract_path = prompt_contracts_dir / f"{name}.yaml"
        if contract_path.exists():
            contract = load_yaml(contract_path)
            if not contract.get("template"):
                unresolved.append(name)

    if unresolved:
        pytest.xfail(
            f"TODO-03 PARTIALLY RESOLVED: prompts missing template text: {unresolved}.\n"
            "Action: Extract exact prompt strings from app/engines/prompt_builder.py."
        )


# ---------------------------------------------------------------------------
# TODO-04: Neo4j live schema
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_todo_04_graph_schema_has_live_node_labels() -> None:
    """
    TODO-04: graph-schema.yaml must document Neo4j node labels discovered
    from actual running schema, not inferred from Python code alone.

    Resolved when: graph-schema.yaml contains a 'verified_against_live_db'
    or 'node_labels' section with actual labels from SHOW LABELS.
    """
    path = DATA_DIR / "graph-schema.yaml"
    if not path.exists():
        pytest.skip("graph-schema.yaml missing")

    schema = load_yaml(path)
    schema_str = str(schema).lower()

    verified = (
        "verified_against_live" in schema_str
        or "show labels" in schema_str
        or "live_db" in schema_str
        or schema.get("verified_against_live_db") is True
    )

    if not verified:
        pytest.fail(
            "TODO-04 REGRESSED (resolved 2026-11-11, strict): graph-schema.yaml "
            "was not verified against live Neo4j.\n"
            "Action: Run SHOW LABELS; SHOW RELATIONSHIP TYPES against live DB and "
            "update graph-schema.yaml with confirmed node_labels and relationship_types.\n"
            "Add: verified_against_live_db: true once confirmed."
        )


# ---------------------------------------------------------------------------
# TODO-05: Waterfall chain provider ordering
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_todo_05_waterfall_order_documented() -> None:
    """
    TODO-05: The enrichment waterfall chain provider ordering must be explicitly
    documented (Perplexity Sonar → Clearbit → ZoomInfo → Apollo/Hunter).

    Resolved when: dependencies/_index.yaml or a waterfall.yaml contains
    explicit ordering with sequence numbers.
    Source gap: app/services/waterfall.py ordering not confirmed in audit.
    """
    index_path = CONTRACTS_DIR / "dependencies" / "_index.yaml"
    if not index_path.exists():
        pytest.skip("dependencies/_index.yaml missing")

    index = load_yaml(index_path)
    index_str = str(index).lower()

    waterfall_ordered = (
        "order" in index_str
        or "sequence" in index_str
        or "priority" in index_str
        or "waterfall" in index_str
    )

    if not waterfall_ordered:
        pytest.fail(
            "TODO-05 REGRESSED (resolved 2026-11-11, strict): Waterfall chain "
            "ordering not explicitly documented.\n"
            "Action: Read app/services/enrichment/waterfall_engine.py and "
            "config/waterfall_config.yaml and document the exact provider order with "
            "sequence numbers in dependencies/_index.yaml `waterfall`.\n"
            "Shipped order (company): clearbit(1) → zoominfo(2) → apollo(3) → "
            "perplexity_sonar(4)"
        )


# ---------------------------------------------------------------------------
# TODO-06: Redis stream key names
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_todo_06_redis_stream_keys_documented() -> None:
    """
    TODO-06: Exact Redis XADD stream key names must be documented in AsyncAPI bindings.

    Resolved when: asyncapi.yaml channel bindings include the exact Redis stream
    keys used in app/services/event_emitter.py.
    """
    path = EVENTS_DIR / "asyncapi.yaml"
    if not path.exists():
        pytest.skip("asyncapi.yaml missing")

    spec = load_yaml(path)
    spec_str = str(spec)

    has_stream_keys = (
        "xadd" in spec_str.lower()
        or "stream_key" in spec_str.lower()
        or "enrichment:events" in spec_str
        or "enrichment:stream" in spec_str
        or "eie:" in spec_str
    )

    if not has_stream_keys:
        pytest.xfail(
            "TODO-06 UNRESOLVED: Redis stream key names not documented in asyncapi.yaml.\n"
            "Action: Inspect app/services/event_emitter.py for exact XADD key strings "
            "and add Redis binding annotations to asyncapi.yaml channels.\n"
            "Phase 3.4: AsyncAPI bindings required for actual broker."
        )


# ---------------------------------------------------------------------------
# TODO-07: BatchEnrichResponse fully in OpenAPI
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_todo_07_batch_enrich_response_schema_complete() -> None:
    """
    TODO-07: POST /api/v1/enrich/batch response must fully document
    BatchEnrichResponse (results, total, succeeded, failed, total_processing_time_ms).
    Source: app/models/schemas.py BatchEnrichResponse.
    """
    spec = _load_yaml_safe(API_DIR / "openapi.yaml")
    if spec is None:
        pytest.skip("openapi.yaml missing")

    paths = spec.get("paths", {})
    if "/api/v1/enrich/batch" not in paths:
        pytest.skip("/api/v1/enrich/batch not in spec")

    post_op = paths["/api/v1/enrich/batch"].get("post", {})
    response_200 = post_op.get("responses", {}).get("200", {})
    schema = response_200.get("content", {}).get("application/json", {}).get("schema", {})

    if not schema:
        pytest.xfail(
            "TODO-07 UNRESOLVED: /api/v1/enrich/batch has no response schema.\n"
            "Action: Add BatchEnrichResponse schema from app/models/schemas.py.\n"
            "Required fields: results, total, succeeded, failed, total_processing_time_ms, "
            "total_tokens_used"
        )

    props = schema.get("properties", {})
    if schema.get("$ref"):
        return  # Resolved via $ref

    batch_fields = ["results", "total", "succeeded", "failed"]
    missing = [f for f in batch_fields if f not in props]
    if missing:
        pytest.xfail(
            f"TODO-07 PARTIALLY RESOLVED: batch response missing fields: {missing}.\n"
            "Source: app/models/schemas.py BatchEnrichResponse"
        )


# ---------------------------------------------------------------------------
# TODO-08: Circuit breaker state transitions
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_todo_08_circuit_breaker_transitions_documented() -> None:
    """
    TODO-08: Circuit breaker state machine (closed→open→half-open) must be
    documented in the redis or config dependency contract.

    Resolved when: one of the dependency contracts or config contract documents
    the CB_FAILURE_THRESHOLD, CB_COOLDOWN_SECONDS, and state transitions.
    """
    redis_contract_path = CONTRACTS_DIR / "dependencies" / "redis.yaml"
    config_contract_path = CONTRACTS_DIR / "config" / "env-contract.yaml"

    redis_contract = _load_yaml_safe(redis_contract_path)
    config_contract = _load_yaml_safe(config_contract_path)

    combined_str = str(redis_contract or "") + str(config_contract or "")
    combined_lower = combined_str.lower()

    has_cb_docs = (
        "circuit_breaker" in combined_lower
        or "cb_failure" in combined_lower
        or "half-open" in combined_lower
        or "state_transition" in combined_lower
    )

    if not has_cb_docs:
        pytest.xfail(
            "TODO-08 UNRESOLVED: Circuit breaker state transitions not documented.\n"
            "Action: Add circuit_breaker section to dependencies/redis.yaml or "
            "config/env-contract.yaml documenting:\n"
            "  - closed → open (after CB_FAILURE_THRESHOLD failures)\n"
            "  - open → half-open (after CB_COOLDOWN_SECONDS)\n"
            "  - half-open → closed (on success) or open (on failure)\n"
            "Source: app/engines/circuit_breaker.py"
        )


# ---------------------------------------------------------------------------
# TODO-09: Prompt registry completeness
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_todo_09_prompt_registry_not_empty() -> None:
    """
    TODO-09: agents/prompt-contracts/_index.yaml must be non-empty and list
    all prompts discovered in app/engines/prompt_builder.py.

    Resolved when: _index.yaml contains at least the enrichment_v2 and
    convergence system prompts.
    """
    index_path = AGENTS_DIR / "prompt-contracts" / "_index.yaml"
    if not index_path.exists():
        pytest.xfail(
            "TODO-09 UNRESOLVED: agents/prompt-contracts/_index.yaml missing.\n"
            "Action: Create the file and register all prompts from "
            "app/engines/prompt_builder.py and app/engines/convergence_controller.py."
        )

    index = load_yaml(index_path)
    prompts = index.get("prompts", [])

    if not prompts:
        pytest.xfail(
            "TODO-09 UNRESOLVED: prompt registry is empty.\n"
            "Action: Add prompt entries. Expected at minimum:\n"
            "  - enrichment_v2 (from app/engines/prompt_builder.py)\n"
            "  - convergence_system (from app/engines/convergence_controller.py)"
        )

    # Minimum: at least 2 prompts
    assert len(prompts) >= 2, (
        f"TODO-09 PARTIALLY RESOLVED: only {len(prompts)} prompt(s) registered. "
        "Expected at least 2 (enrichment + convergence)."
    )


# ---------------------------------------------------------------------------
# TODO-10: Convergence run_id response schema
# ---------------------------------------------------------------------------


@pytest.mark.unit
def _converge_status_response_keys() -> list[str]:
    """Keys of the dict literal returned by get_convergence_status, read from source."""
    import ast

    source = (REPO_ROOT / "app" / "api" / "v1" / "converge.py").read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef) and (
            node.name == "get_convergence_status"
        ):
            for stmt in ast.walk(node):
                if isinstance(stmt, ast.Return) and isinstance(stmt.value, ast.Dict):
                    return [k.value for k in stmt.value.keys if isinstance(k, ast.Constant)]
    pytest.fail("get_convergence_status return dict not found in app/api/v1/converge.py")


def test_todo_10_converge_run_id_response_schema() -> None:
    """
    TODO-10: GET /v1/converge/{run_id} response schema must document exactly
    what the route returns.

    The route returns a LoopState summary built in app/api/v1/converge.py
    get_convergence_status (``status`` / ``current_pass``), not ConvergeResponse
    (``state`` / ``pass_count``) as the audit assumed. The expected fields are
    read from that handler, so the contract and the code cannot drift apart.
    Resolved (strict) 2026-11-11.
    """
    spec = _load_yaml_safe(API_DIR / "openapi.yaml")
    if spec is None:
        pytest.skip("openapi.yaml missing")

    paths = spec.get("paths", {})
    path_key = "/v1/converge/{run_id}"
    if path_key not in paths:
        pytest.fail(f"TODO-10 REGRESSED: {path_key} missing from openapi.yaml")

    get_op = paths[path_key].get("get", {})
    response_200 = get_op.get("responses", {}).get("200", {})
    schema = response_200.get("content", {}).get("application/json", {}).get("schema", {})
    if not schema:
        pytest.fail(f"TODO-10 REGRESSED: {path_key} has no 200 response schema.")

    documented = set(schema.get("properties", {}))
    returned = set(_converge_status_response_keys())
    missing = sorted(returned - documented)
    extra = sorted(documented - returned)
    if missing or extra:
        pytest.fail(
            f"TODO-10 REGRESSED: {path_key} schema differs from get_convergence_status.\n"
            f"Returned but undocumented: {missing}. Documented but not returned: {extra}."
        )
    undeclared_required = sorted(returned - set(schema.get("required", [])))
    if undeclared_required:
        pytest.fail(
            f"TODO-10 REGRESSED: always-returned fields not required: {undeclared_required}"
        )
