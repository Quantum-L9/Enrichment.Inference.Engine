"""F-005 source attribution regressions; actual proposer and offline materializer.

L9_META:
  tier: 2
  domain: convergence
  authority: native-domain-remediation-v2-g27r
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from app.engines.convergence import schema_proposer as proposer
from app.models.field_confidence import FieldConfidence, FieldConfidenceMap, FieldSource
from app.services import domain_spec_workflow as workflow

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parents[2]
_SEEDS = ("0", "1", "2", "7", "42", "123", "999", "random")
_EXPECTED_BYTES = b"""name: attribution-fixture
version: 1.1.0-discovered
ontology:
  nodes:
  - label: Entity
    properties:
    - name: id
      type: string
    - name: capacity
      type: float
      source: inference
inference_rules: []
semantic_registry:
  enabled: false
"""
_EXPECTED_DIGEST = hashlib.sha256(_EXPECTED_BYTES).hexdigest()
_MESSAGE = "Ambiguous source attribution: capacity=['enrichment', 'inference']"
_EVIDENCE = (("capacity", ("enrichment", "inference")),)


def _base() -> dict[str, Any]:
    return {
        "name": "attribution-fixture",
        "version": "1.0.0",
        "ontology": {
            "nodes": [{"label": "Entity", "properties": [{"name": "id", "type": "string"}]}]
        },
        "inference_rules": [],
        "semantic_registry": {"enabled": False},
    }


def _batch(sources: list[str], shape: str = "flat") -> list[dict[str, Any]]:
    results = []
    for index, source in enumerate(sources):
        value = float(index + 1)
        entry = {"capacity": {"confidence": 0.9, "source": source}}
        if shape == "model":
            confidences: Any = FieldConfidenceMap(
                fields={
                    "capacity": FieldConfidence(
                        field_name="capacity",
                        value=value,
                        confidence=0.9,
                        source=FieldSource(source),
                    )
                }
            )
        elif shape == "entries":
            confidences = {"entries": entry}
        else:
            confidences = entry
        results.append(
            {"final_fields": {"capacity": value}, "final_field_confidences": confidences}
        )
    return results


def _materialize(batch: list[dict[str, Any]], base: dict[str, Any]) -> tuple[Any, bytes]:
    proposal = proposer.propose(batch, base, domain="attribution-fixture")
    # Keep draft diagnostics unchanged; success starts a NEW explicit discovery.
    discovery_bytes = yaml.safe_dump(base, sort_keys=False, allow_unicode=True).encode("utf-8")
    discovery_digest = hashlib.sha256(discovery_bytes).hexdigest()
    bound_proposal = proposer.propose(
        batch,
        base,
        domain="attribution-fixture",
        tenant_id="attribution-fixture",
        node_label="Entity",
        discovery_spec_sha256=discovery_digest,
    )
    binding_keys = {"node_label", "discovery_spec_sha256", "ownership_contract_version"}
    assert [fp.model_dump(exclude=binding_keys) for fp in bound_proposal.proposed_fields] == [
        fp.model_dump(exclude=binding_keys) for fp in proposal.proposed_fields
    ]
    assert proposal.ownership_contract_version is None
    assert all(fp.node_label is None for fp in proposal.proposed_fields)
    candidate = workflow.materialize_approved_spec(
        base,
        bound_proposal,
        [
            proposer.ApprovalDecision(field_name=fp.field_name, node_label="Entity")
            for fp in bound_proposal.proposed_fields
        ],
        tenant_id="attribution-fixture",
        node_label="Entity",
        source_sha256=discovery_digest,
    )
    return proposal, candidate


@pytest.mark.parametrize("shape", ["flat", "entries", "model"])
def test_qualifying_mixed_sources_refuse_before_materialization(shape: str, monkeypatch) -> None:
    batch = _batch(["inference", "enrichment", "inference"], shape)
    base = _base()
    original_batch, original_base = copy.deepcopy(batch), copy.deepcopy(base)
    calls = []
    actual = workflow.materialize_approved_spec

    def tracked(*args, **kwargs):
        calls.append(True)
        return actual(*args, **kwargs)

    monkeypatch.setattr(workflow, "materialize_approved_spec", tracked)
    with pytest.raises(ValueError) as refusal:
        _materialize(batch, base)
    assert str(refusal.value) == _MESSAGE
    assert refusal.value.field_sources == _EVIDENCE
    assert calls == []
    assert batch == original_batch
    assert base == original_base


def test_all_qualifying_competing_evidence_is_retained_in_stable_field_order() -> None:
    batch = _batch(["inference", "enrichment"])
    for index, result in enumerate(batch):
        result["final_fields"] = {"quality": "A", **result["final_fields"]}
        result["final_field_confidences"]["quality"] = {
            "confidence": 0.9,
            "source": "manual" if index == 0 else "crm",
        }
    for ordered in (batch, list(reversed(batch))):
        with pytest.raises(ValueError) as refusal:
            proposer.propose(ordered, _base())
        assert refusal.value.field_sources == (
            *_EVIDENCE,
            ("quality", ("crm", "manual")),
        )
        assert str(refusal.value) == _MESSAGE + "; quality=['crm', 'manual']"


@pytest.mark.parametrize("shape", ["flat", "entries", "model"])
@pytest.mark.parametrize("source", ["crm", "enrichment", "inference", "manual", "seed"])
def test_singleton_source_is_preserved_through_real_materializer(shape: str, source: str) -> None:
    base = _base()
    original = copy.deepcopy(base)
    proposal, candidate = _materialize(_batch([source, source, source], shape), base)
    assert len(proposal.proposed_fields) == 1
    assert proposal.proposed_fields[0].source == source
    assert f"    source: {source}" in proposal.yaml_diff
    parsed = yaml.safe_load(candidate)
    assert parsed["ontology"]["nodes"][0]["properties"][-1]["source"] == source
    assert parsed["inference_rules"] == original["inference_rules"]
    assert parsed["semantic_registry"] == original["semantic_registry"]
    assert base == original


@pytest.mark.parametrize("shape", ["numeric", "record-without-source", "missing-source"])
def test_no_source_retains_enrichment_fallback(shape: str) -> None:
    batch = _batch(["inference", "inference"])
    for result in batch:
        if shape == "numeric":
            confidences: Any = {"capacity": 0.9}
        elif shape == "record-without-source":
            confidences = SimpleNamespace(fields={"capacity": SimpleNamespace(confidence=0.9)})
        else:
            confidences = {"capacity": {"confidence": 0.9}}
        result["final_field_confidences"] = confidences
    proposal, candidate = _materialize(batch, _base())
    assert proposal.proposed_fields[0].source == "enrichment"
    assert (
        yaml.safe_load(candidate)["ontology"]["nodes"][0]["properties"][-1]["source"]
        == "enrichment"
    )


@pytest.mark.parametrize("shape", ["flat", "entries", "model"])
@pytest.mark.parametrize(
    "filter_kind",
    ["existing-list", "existing-dict", "low-fill", "low-confidence", "noncanonical", "banned"],
)
def test_existing_and_nonqualifying_mixed_fields_are_still_filtered(
    shape: str, filter_kind: str
) -> None:
    batch = _batch(["inference", "enrichment"])
    if shape != "flat":
        batch = _batch(["inference", "enrichment"], shape)
    base = _base()
    if filter_kind == "existing-list":
        base["ontology"]["nodes"][0]["properties"].append({"name": "capacity", "type": "float"})
    elif filter_kind == "existing-dict":
        base["ontology"]["nodes"] = {"Entity": {"properties": {"capacity": {"type": "float"}}}}
    elif filter_kind == "low-fill":
        batch.extend({"final_fields": {}, "final_field_confidences": {}} for _ in range(3))
    elif filter_kind == "low-confidence":
        for result in batch:
            confidences = result["final_field_confidences"]
            if shape == "model":
                confidences.fields["capacity"].confidence = 0.5
            else:
                (confidences["entries"] if shape == "entries" else confidences)["capacity"][
                    "confidence"
                ] = 0.5
    else:
        name = "badName" if filter_kind == "noncanonical" else "targetnode"
        for result in batch:
            result["final_fields"][name] = result["final_fields"].pop("capacity")
            confidences = result["final_field_confidences"]
            if shape == "model":
                confidences.fields[name] = confidences.fields.pop("capacity")
            else:
                entries = confidences["entries"] if shape == "entries" else confidences
                entries[name] = entries.pop("capacity")
    assert proposer.propose(batch, base).proposed_fields == []


def test_null_observations_do_not_introduce_competing_source_evidence() -> None:
    batch = _batch(["inference", "inference", "enrichment"])
    batch[-1]["final_fields"]["capacity"] = None
    result = proposer.propose(batch, _base())
    assert result.proposed_fields[0].source == "inference"
    assert result.proposed_fields[0].fill_rate == 0.6667


def _emit_process_witness() -> None:
    """Run actual source paths in a fresh interpreter; no services or publication."""
    mixed = sys.argv[-1] == "mixed"
    batch = _batch(["inference", "enrichment", "inference"] if mixed else ["inference"] * 3)
    try:
        proposal, candidate = _materialize(batch, _base())
    except ValueError as exc:
        result = {
            "status": "refused",
            "message": str(exc),
            "field_sources": getattr(exc, "field_sources", None),
        }
    else:
        result = {
            "status": "materialized",
            "source": proposal.proposed_fields[0].source,
            "sha256": hashlib.sha256(candidate).hexdigest(),
            "bytes": base64.b64encode(candidate).decode("ascii"),
        }
    print("F005_WITNESS=" + json.dumps(result, sort_keys=True))


def _process_witness(seed: str, mode: str) -> dict[str, Any]:
    process = subprocess.run(
        [
            sys.executable,
            "-c",
            "import runpy, sys; runpy.run_path(sys.argv[1])['_emit_process_witness']()",
            str(Path(__file__).resolve()),
            mode,
        ],
        cwd=_REPO,
        env={**os.environ, "PYTHONHASHSEED": seed, "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert process.returncode == 0, process.stdout + process.stderr
    witnesses = [
        line.removeprefix("F005_WITNESS=")
        for line in process.stdout.splitlines()
        if line.startswith("F005_WITNESS=")
    ]
    assert len(witnesses) == 1, process.stdout
    return json.loads(witnesses[0])


@pytest.mark.parametrize("seed", _SEEDS)
def test_actual_mixed_source_refusal_is_stable_across_process_hash_seeds(seed: str) -> None:
    assert _process_witness(seed, "mixed") == {
        "status": "refused",
        "message": _MESSAGE,
        "field_sources": [["capacity", ["enrichment", "inference"]]],
    }


@pytest.mark.parametrize("seed", _SEEDS)
def test_actual_singleton_materialized_bytes_and_digest_are_canonical_across_process_hash_seeds(
    seed: str,
) -> None:
    assert _process_witness(seed, "singleton") == {
        "status": "materialized",
        "source": "inference",
        "sha256": _EXPECTED_DIGEST,
        "bytes": base64.b64encode(_EXPECTED_BYTES).decode("ascii"),
    }
