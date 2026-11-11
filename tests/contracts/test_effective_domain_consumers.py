"""Published YAML is the one runtime authority; DB rows are approval audit only.

L9_META:
  tier: 2
  domain: native-domain
  authority: user native-v3 execution decision 2026-11-03
  pr_class: app_code + tier2_test
"""

from __future__ import annotations

import hashlib
import json
import tomllib
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import yaml

from app.engines.domain_yaml_reader import DomainYamlReader
from app.services.effective_domain_registry import EffectiveDomainRegistry


def _l9_install_step(root: Path) -> dict:
    workflow = yaml.safe_load((root / ".github/workflows/pr-pipeline.yml").read_text())
    return next(
        step
        for step in workflow["jobs"]["l9"]["steps"]
        if step.get("name") == "Install L9 dependencies"
    )


def _locked_versions(root: Path) -> dict[str, str]:
    lock = tomllib.loads((root / "uv.lock").read_text())
    return {package["name"]: package["version"] for package in lock["package"]}


@pytest.mark.unit
def test_lightweight_contract_job_installs_declared_sqlalchemy_dependency() -> None:
    root = Path(__file__).resolve().parents[2]
    declared = [
        spec
        for spec in tomllib.loads((root / "pyproject.toml").read_text())["project"]["dependencies"]
        if spec.startswith("sqlalchemy[")
    ]
    assert declared == ["sqlalchemy[asyncio]"]
    # The asyncio extra is what brings greenlet, which sqlalchemy.ext.asyncio needs.
    locked = _locked_versions(root)
    assert "sqlalchemy" in locked
    assert "greenlet" in locked
    install = _l9_install_step(root)
    assert "bash tools/deps/install_locked.sh full" in install["run"]
    assert not install.get("continue-on-error", False)


@pytest.mark.unit
def test_lightweight_contract_job_installs_declared_provider_import_dependency() -> None:
    root = Path(__file__).resolve().parents[2]
    dependencies = tomllib.loads((root / "pyproject.toml").read_text())["project"]["dependencies"]
    assert "perplexityai>=0.2.0" in dependencies
    assert "perplexityai" in _locked_versions(root)
    install = _l9_install_step(root)
    assert "bash tools/deps/install_locked.sh full" in install["run"]


def _publish_fixture(root: Path, version: str = "0.3.0") -> str:
    """Isolated test receipt, never an operator approval or promotion."""
    fixture = Path(__file__).resolve().parents[2] / "domains/plasticos/spec.yaml"
    raw = yaml.safe_load(fixture.read_text())
    raw["version"] = version
    data = yaml.safe_dump(raw, sort_keys=False, allow_unicode=True).encode("utf-8")
    digest = hashlib.sha256(data).hexdigest()
    revision = root / "plasticos/revisions" / digest
    revision.mkdir(parents=True, exist_ok=True)
    (revision / "spec.yaml").write_bytes(data)
    (revision / "receipt.json").write_text(
        json.dumps(
            {
                "schema": "eie.domain-approval/v1",
                "tenant_id": "plasticos",
                "version": version,
                "sha256": digest,
                "reviewed_input_sha256": digest,
                "reviewed_by": "test-fixture-only",
                "approval_ref": "test-fixture-only",
                "reviewed_at": "2026-11-03T21:00:00+00:00",
                "previous_sha256": None,
                "approval_batches": [],
            }
        )
    )
    (root / "plasticos/current.json").write_text(
        json.dumps(
            {
                "schema": "eie.domain-current/v1",
                "sha256": digest,
            }
        )
    )
    return digest


@pytest.mark.unit
def test_registry_reader_observes_pointer_update_and_refuses_mutation(tmp_path):
    _publish_fixture(tmp_path)
    reader = DomainYamlReader(tmp_path, require_published=True)
    registry = EffectiveDomainRegistry(reader, ["plasticos"])
    assert registry["plasticos"]["version"] == "0.3.0"
    _publish_fixture(tmp_path, "0.4.0")
    assert registry.get("plasticos")["version"] == reader.load("plasticos").version == "0.4.0"
    with pytest.raises(TypeError):
        registry["plasticos"] = {"version": "forged"}
    snap = registry["plasticos"]
    snap["version"] = "forged"
    assert registry["plasticos"]["version"] == "0.4.0"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_audit_replay_cannot_override_published_yaml(tmp_path):
    from app.api.v1 import converge

    _publish_fixture(tmp_path)
    reader = DomainYamlReader(tmp_path, require_published=True)
    registry = converge.build_domain_spec_registry(reader)
    with (
        patch.object(converge, "_domain_specs", registry),
        patch.object(
            converge.pg_store,
            "get_approved_schema_proposals",
            new_callable=AsyncMock,
        ) as ledger,
    ):
        assert await converge.hydrate_approved_revisions() == {}
        ledger.assert_not_awaited()
        assert converge._domain_specs["plasticos"]["version"] == "0.3.0"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_bad_published_spec_does_not_converge_without_hints(tmp_path):
    from app.engines import handlers

    digest = _publish_fixture(tmp_path)
    reader = DomainYamlReader(tmp_path, require_published=True)
    reader.load("plasticos")
    (tmp_path / "plasticos/revisions" / digest / "spec.yaml").write_text("name: tampered")
    with patch.object(handlers, "_domain_reader", reader), pytest.raises(ValueError):
        await handlers._load_domain_convergence_context(
            domain_id="plasticos", node_label="Facility"
        )


@pytest.mark.unit
def test_bad_published_spec_cannot_reach_graph_settings_fallback(tmp_path):
    from app.engines.packet_router import PacketRouter

    digest = _publish_fixture(tmp_path)
    reader = DomainYamlReader(tmp_path, require_published=True)
    reader.load("plasticos")
    (tmp_path / "plasticos/revisions" / digest / "spec.yaml").write_text("name: tampered")
    router = PacketRouter(
        gate_url="https://gate.example",
        domain_reader=reader,
        fallback_entity_type="other",
        fallback_id_property="id",
    )
    with pytest.raises(ValueError):
        router.graph_sync_contract("plasticos")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_http_decisions_persist_but_do_not_auto_publish_on_readonly_runtime(tmp_path):
    from app.api.v1 import converge
    from app.engines.convergence.loop_state import LoopState, LoopStatus
    from app.engines.convergence.schema_proposer import (
        ApprovalDecision,
        FieldProposal,
        SchemaProposalSet,
    )

    digest = _publish_fixture(tmp_path)
    reader = DomainYamlReader(tmp_path, require_published=True)
    registry = converge.build_domain_spec_registry(reader)
    state = LoopState(
        entity_id="test-only-entity",
        domain="plasticos",
        status=LoopStatus.HUMAN_HOLD,
        node_label="Facility",
        discovery_spec_sha256=digest,
        ownership_contract_version=1,
    )
    store = AsyncMock()
    store.load.return_value = state
    proposal = SchemaProposalSet(
        domain="plasticos",
        tenant_id="plasticos",
        discovery_spec_sha256=digest,
        ownership_contract_version=1,
        version_bump="0.4.0-discovered",
        proposed_fields=[
            FieldProposal(
                field_name="new_evidence",
                field_type="string",
                node_label="Facility",
                discovery_spec_sha256=digest,
                ownership_contract_version=1,
            )
        ],
    )
    body = converge.ApproveRequestBody(
        decisions=[ApprovalDecision(node_label="Facility", field_name="new_evidence")],
        reviewed_by="test-fixture-only",
    )
    with (
        patch.object(converge, "_domain_specs", registry),
        patch.object(
            converge,
            "_get_state_store",
            return_value=store,
        ),
        patch.object(converge, "require_direct_domain_access"),
        patch.object(
            converge,
            "_propose_for_run",
            return_value=proposal,
        ),
        patch.object(
            converge, "persist_approved_revision", new_callable=AsyncMock, return_value=1
        ) as ledger,
    ):
        result = await converge.approve_proposals(state.run_id, body)
    assert result["approved"] == result["persisted"] == 1
    assert result["publication_status"] == "awaiting_operator_publication"
    assert result["new_version"] == "0.3.0"
    assert "new_evidence" not in reader.load("plasticos").node_schemas["Facility"].properties
    assert ledger.await_args.kwargs["reviewed_by"] == "test-fixture-only"
    assert ledger.await_args.kwargs["proposal_set"].discovery_spec_sha256 == digest
    assert ledger.await_args.kwargs["decisions"][0].node_label == "Facility"
    assert state.discovery_spec_sha256 == reader.load("plasticos").source_sha256 == digest
    assert DomainYamlReader(tmp_path, require_published=True).load("plasticos").version == "0.3.0"
