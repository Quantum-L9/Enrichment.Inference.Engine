"""
Data Model Contract Tests
Source: app/services/pg_models.py, migrations/versions/001_initial_schema.py
Markers: unit
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.contracts.conftest_contracts import DATA_DIR, load_json, load_yaml

MODEL_FILES = {
    "enrichment-result": DATA_DIR / "models" / "enrichment-result.schema.json",
    "convergence-run": DATA_DIR / "models" / "convergence-run.schema.json",
    "field-confidence-history": DATA_DIR / "models" / "field-confidence-history.schema.json",
    "schema-proposal": DATA_DIR / "models" / "schema-proposal.schema.json",
}

ENRICHMENT_RESULT_FIELDS = {
    "id": {"type": "string"},
    "state": {"type": "string"},
    "fields": {},
    "confidence": {},
    "inference_version": {"type": "string"},
    "processing_time_ms": {},
    "tokens_used": {},
    "quality_tier": {"type": "string"},
}

CONVERGENCE_RUN_FIELDS = {
    "id": {},
    "domain": {},
    "entity_id": {},
    "state": {},
    "current_pass": {},
    "max_passes": {},
}

FIELD_CONFIDENCE_HISTORY_FIELDS = {
    "id": {},
    "entity_id": {},
    "field_name": {},
    "field_value": {},
    "confidence": {},
    "source": {},
}

SCHEMA_PROPOSAL_FIELDS = {
    "id": {},
    "domain": {},
    "field_name": {},
    "field_type": {},
    "approval_status": {},
}

FIELD_GROUND_TRUTH = {
    "enrichment-result": ENRICHMENT_RESULT_FIELDS,
    "convergence-run": CONVERGENCE_RUN_FIELDS,
    "field-confidence-history": FIELD_CONFIDENCE_HISTORY_FIELDS,
    "schema-proposal": SCHEMA_PROPOSAL_FIELDS,
}


@pytest.mark.unit
@pytest.mark.parametrize("model_name,schema_path", MODEL_FILES.items())
def test_model_schema_is_valid_json(model_name: str, schema_path: Path) -> None:
    assert schema_path.exists(), f"Missing model schema: {schema_path}"
    data = load_json(schema_path)
    assert isinstance(data, dict)


@pytest.mark.unit
@pytest.mark.parametrize("model_name,schema_path", MODEL_FILES.items())
def test_model_has_schema_id(model_name: str, schema_path: Path) -> None:
    if not schema_path.exists():
        pytest.skip(f"Missing: {schema_path}")
    data = load_json(schema_path)
    assert "$schema" in data, f"{model_name}: missing $schema"
    assert "$id" in data, f"{model_name}: missing $id — Phase 3.2"


@pytest.mark.unit
@pytest.mark.parametrize("model_name,schema_path", MODEL_FILES.items())
def test_model_has_description(model_name: str, schema_path: Path) -> None:
    if not schema_path.exists():
        pytest.skip(f"Missing: {schema_path}")
    data = load_json(schema_path)
    assert data.get("description"), f"{model_name}: missing description — Phase 3.6 Rule 5"


@pytest.mark.unit
@pytest.mark.parametrize("model_name,schema_path", MODEL_FILES.items())
def test_model_has_examples(model_name: str, schema_path: Path) -> None:
    if not schema_path.exists():
        pytest.skip(f"Missing: {schema_path}")
    data = load_json(schema_path)
    assert data.get("examples"), f"{model_name}: missing examples — Phase 3.2"


@pytest.mark.unit
@pytest.mark.parametrize("model_name,schema_path", MODEL_FILES.items())
def test_model_additional_properties_false(model_name: str, schema_path: Path) -> None:
    if not schema_path.exists():
        pytest.skip(f"Missing: {schema_path}")
    data = load_json(schema_path)
    assert data.get("additionalProperties") is False, (
        f"{model_name}: additionalProperties must be false — Phase 3.2 strict mode"
    )


@pytest.mark.unit
@pytest.mark.parametrize("model_name,expected_fields", FIELD_GROUND_TRUTH.items())
def test_model_required_fields_present(model_name: str, expected_fields: dict) -> None:
    schema_path = MODEL_FILES[model_name]
    if not schema_path.exists():
        pytest.skip(f"Missing: {schema_path}")
    data = load_json(schema_path)
    props = data.get("properties", {})
    for field in expected_fields:
        assert field in props, (
            f"{model_name}: field '{field}' missing from properties. "
            "Source: app/services/pg_models.py"
        )


@pytest.mark.unit
@pytest.mark.parametrize("model_name,expected_fields", FIELD_GROUND_TRUTH.items())
def test_model_fields_have_descriptions(model_name: str, expected_fields: dict) -> None:
    schema_path = MODEL_FILES[model_name]
    if not schema_path.exists():
        pytest.skip(f"Missing: {schema_path}")
    data = load_json(schema_path)
    props = data.get("properties", {})
    for field in expected_fields:
        if field in props:
            assert props[field].get("description"), (
                f"{model_name}.{field}: missing description — Phase 3.6 Rule 5"
            )


@pytest.mark.unit
def test_graph_schema_has_node_labels() -> None:
    path = DATA_DIR / "graph-schema.yaml"
    if not path.exists():
        pytest.skip("graph-schema.yaml missing")
    schema = load_yaml(path)
    s = str(schema).lower()
    assert any(k in s for k in ("node_labels", "nodes", "node")), (
        "graph-schema.yaml must document node labels. Source: Neo4j CEG database."
    )


@pytest.mark.unit
def test_graph_schema_has_relationship_types() -> None:
    path = DATA_DIR / "graph-schema.yaml"
    if not path.exists():
        pytest.skip("graph-schema.yaml missing")
    schema = load_yaml(path)
    s = str(schema).lower()
    assert any(k in s for k in ("relationship", "edge", "connects")), (
        "graph-schema.yaml must document relationship types."
    )


@pytest.mark.unit
def test_graph_schema_inventory_matches_live_verification() -> None:
    """TODO-04: the documented inventory is exactly what the live run observed."""
    schema = load_yaml(DATA_DIR / "graph-schema.yaml")
    assert schema.get("verified_against_live_db") is True
    observed = schema["live_verification"]["observed"]
    labels: set[str] = set()
    relationship_types: set[str] = set()
    for tenant in schema["tenants"].values():
        labels |= {n["label"] for n in tenant["nodes"]}
        relationship_types |= {r["type"] for r in tenant["relationships"]}
        relationship_types |= {r["type"] for r in tenant["asserted_relationships"]}
    assert labels == set(observed["labels"])
    assert relationship_types == set(observed["relationship_types"])
    assert set(observed["node_properties"]) == labels


@pytest.mark.unit
def test_graph_schema_nodes_match_eie_tenant_sync_endpoints() -> None:
    """TODO-04: a node EIE can write is a sync endpoint its tenant spec declares.

    EIE selects the endpoint from its own domains/<tenant>/spec.yaml
    (app/engines/packet_router.py resolve_graph_sync_contract), so an
    endpoint added, removed or re-keyed there must be reflected here.
    """
    from app.engines.packet_router import _usable_sync_endpoints

    schema = load_yaml(DATA_DIR / "graph-schema.yaml")
    repo_root = DATA_DIR.parent.parent.parent
    for tenant_id, tenant in schema["tenants"].items():
        spec = load_yaml(repo_root / tenant["eie_spec"])
        declared = {(t, e, i) for e, i, t in _usable_sync_endpoints(spec)}
        documented = {(n["label"], n["entity_type"], n["idproperty"]) for n in tenant["nodes"]}
        assert documented == declared, tenant_id


@pytest.mark.unit
def test_migration_policy_exists_and_non_empty() -> None:
    path = DATA_DIR / "migrations" / "migration-policy.md"
    if not path.exists():
        pytest.skip("migration-policy.md missing")
    content = path.read_text()
    assert len(content) > 100, "migration-policy.md seems like a stub"
    assert "migration" in content.lower()


@pytest.mark.unit
def test_data_model_index_covers_all_models() -> None:
    index_path = DATA_DIR / "models" / "_index.yaml"
    if not index_path.exists():
        pytest.skip("_index.yaml missing")
    index = load_yaml(index_path)
    index_str = str(index)
    for model_name in MODEL_FILES:
        assert model_name in index_str or model_name.replace("-", "_") in index_str, (
            f"Model '{model_name}' not in data/models/_index.yaml"
        )
