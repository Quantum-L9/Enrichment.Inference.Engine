"""Raw native YAML ambiguity must fail before admission or cached reuse.

L9_META:
  tier: 2
  domain: enrichment
  authority: native-domain-remediation-F-003
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml

from app.engines.domain_yaml_reader import DomainPublicationError, DomainYamlReader

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]
CEG_REVISION = "93cf1a83bdb88656a627b442a6d114c3a3502cde"
GOOD = (ROOT / "domains/plasticos/spec.yaml").read_bytes()
DUPLICATES = {
    "name": GOOD.replace(b"name: plasticos", b"name: other\nname: plasticos", 1),
    "version": GOOD.replace(b'version: "0.4.0"', b'version: "9.0.0"\nversion: "0.4.0"', 1),
    "sync": GOOD.replace(b"sync:\n", b"sync: {}\nsync:\n", 1),
    "properties": GOOD.replace(b"    properties:\n", b"    properties: []\n    properties:\n", 1),
    "type": GOOD.replace(
        b"name: facility_id, type: string",
        b"name: facility_id, type: float, type: string",
        1,
    ),
    "condition": GOOD.replace(b"operator: CONTAINS", b"operator: LT, operator: CONTAINS", 1),
}
# Every variant must really inject its duplicate: an anchor that no longer
# matches the specimen would make the variant the valid spec itself.
assert all(variant != GOOD for variant in DUPLICATES.values())


def _parse(content: bytes | str):
    from app.engines.native_yaml import parse_native_yaml

    return parse_native_yaml(content)


def _publish(root: Path, content: bytes) -> Path:
    """Write only temporary test evidence, never an operator approval."""
    digest = hashlib.sha256(content).hexdigest()
    revision = root / "plasticos/revisions" / digest
    revision.mkdir(parents=True, exist_ok=True)
    (revision / "spec.yaml").write_bytes(content)
    receipt = {
        "schema": "eie.domain-approval/v1",
        "tenant_id": "plasticos",
        "version": "0.4.0",
        "sha256": digest,
        "reviewed_input_sha256": digest,
        "reviewed_by": "test-only-reviewer",
        "approval_ref": "test-only-approval",
        "reviewed_at": "2026-01-01T00:00:00Z",
        "previous_sha256": None,
        "approval_batches": [],
    }
    (revision / "receipt.json").write_text(json.dumps(receipt), encoding="utf-8")
    (root / "plasticos/current.json").write_text(
        json.dumps({"schema": "eie.domain-current/v1", "sha256": digest}), encoding="utf-8"
    )
    return revision


@pytest.mark.parametrize("content", DUPLICATES.values(), ids=DUPLICATES)
def test_parser_rejects_explicit_duplicate_at_every_native_depth(content: bytes) -> None:
    with pytest.raises(yaml.YAMLError, match="[Dd]uplicate"):
        _parse(content)


@pytest.mark.parametrize("content", DUPLICATES.values(), ids=DUPLICATES)
@pytest.mark.parametrize("published", [False, True])
def test_reader_refuses_duplicate_without_cache_or_legacy_fallback(
    tmp_path: Path, content: bytes, published: bool
) -> None:
    if published:
        _publish(tmp_path, GOOD)
        (tmp_path / "plasticos/spec.yaml").write_bytes(GOOD)
    else:
        (tmp_path / "plasticos.yaml").write_bytes(GOOD)
    reader = DomainYamlReader(tmp_path)
    assert reader.load("plasticos").version == "0.4.0"
    if published:
        # Hash and receipt correctly bind the ambiguous bytes: hashing is not parsing.
        _publish(tmp_path, content)
        error = DomainPublicationError
    else:
        (tmp_path / "plasticos.yaml").write_bytes(content)
        error = yaml.YAMLError
    for _ in range(2):
        with pytest.raises(error, match="[Dd]uplicate"):
            reader.load("plasticos")


def test_canonical_good_preserves_exact_digest_and_receipt_tamper_refusal(tmp_path: Path) -> None:
    original = bytes(GOOD)
    assert _parse(GOOD) == yaml.safe_load(GOOD)
    assert original == GOOD
    revision = _publish(tmp_path, GOOD)
    reader = DomainYamlReader(tmp_path, require_published=True)
    assert reader.load("plasticos").source_sha256 == hashlib.sha256(GOOD).hexdigest()
    receipt = json.loads((revision / "receipt.json").read_bytes())
    receipt["reviewed_input_sha256"] = "a" * 64
    (revision / "receipt.json").write_text(json.dumps(receipt), encoding="utf-8")
    for _ in range(2):
        with pytest.raises(DomainPublicationError, match="exact published bytes"):
            reader.load("plasticos")


@pytest.mark.parametrize(
    "content",
    [
        "1: value",
        "true: value",
        "null: value",
        "[a, b]: value",
        "{a: b}: value",
        "a: {1: value}",
        "a: &a [*a]",
        "a: &a {self: *a}",
        "a: &a {<<: *a}",
        "a: &a {x: 1}\nb: {<<: [*a]}",
        "a: &a {x: 1}\nb: {<<: *a, <<: *a}",
        "a: &a {x: 1, x: 2}\nb: {<<: *a}",
        "a: &a {x: 1}\nb: {<<: *a, x: 2, x: 3}",
        "a: {<<: 42}",
        "? !!merge {not: string}\n: {x: value}",
        "!!merge other: {x: value}",
        "!!python/object/apply:os.system ['false']",
        "a: 1\n---\nb: 2",
    ],
)
def test_nonstring_cycle_merge_and_unsafe_tag_negatives(content: str) -> None:
    with pytest.raises(yaml.YAMLError):
        _parse(content)


def test_single_merge_allows_explicit_override_in_either_order() -> None:
    for body in ("<<: *base, x: override", "x: override, <<: *base"):
        assert _parse(f"base: &base {{x: original, y: kept}}\nchild: {{{body}}}")["child"] == {
            "x": "override",
            "y": "kept",
        }
    assert _parse('"<<": literal')["<<"] == "literal"
    assert _parse("a: &a [one, two]\nb: *a")["b"] == ["one", "two"]
    assert _parse("a: &a {x: one}\nb: &b {<<: *a, y: two}\nc: {<<: *b, x: three}")["c"] == {
        "x": "three",
        "y": "two",
    }


@pytest.mark.parametrize(
    "kind", ["depth", "alias-depth", "alias-count", "expansion", "nodes", "bytes"]
)
def test_bounded_resource_negatives(kind: str) -> None:
    if kind == "depth":
        content = "a: " + "[" * 70 + "0" + "]" * 70
    elif kind == "alias-depth":
        lines = ["a0: &a0 one"]
        lines.extend(f"a{i}: &a{i} [*a{i - 1}]" for i in range(1, 65))
        content = "\n".join(lines)
    elif kind == "alias-count":
        content = "a: &a [one]\nb: [" + ",".join(["*a"] * 129) + "]"
    elif kind == "expansion":
        lines = ["a0: &a0 [one, two]"]
        lines.extend(f"a{i}: &a{i} [*a{i - 1}, *a{i - 1}]" for i in range(1, 20))
        content = "\n".join(lines)
    elif kind == "nodes":
        content = "a: [" + ",".join(["x"] * 50001) + "]"
    else:
        content = "#" + "x" * (2 * 1024 * 1024)
    with pytest.raises(yaml.YAMLError, match="[Ll]imit|[Bb]ound|[Dd]epth|[Ee]xpansion"):
        _parse(content)


@pytest.mark.parametrize("loader", ["domain", "convergence", "rules"])
def test_retained_legacy_loaders_refuse_recursive_duplicates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, loader: str
) -> None:
    from app.engines.convergence.convergence_config import ConvergenceConfig
    from app.engines.inference.rule_loader import load_rules
    from app.services.domain_yaml_reader import DomainSpec

    monkeypatch.chdir(tmp_path)
    path = tmp_path / "config/domains/plasticos.yaml"
    path.parent.mkdir(parents=True)
    path.write_bytes(DUPLICATES["condition"])
    with pytest.raises(yaml.YAMLError, match="[Dd]uplicate"):
        if loader == "domain":
            DomainSpec.from_yaml(path)
        elif loader == "convergence":
            ConvergenceConfig.from_domain_yaml("plasticos")
        else:
            load_rules(path)


def test_pair_duplicate_refused_before_ceg_subprocess(tmp_path: Path, monkeypatch) -> None:
    from scripts import check_domain_compatibility as pair

    eie = tmp_path / "eie.yaml"
    ceg = tmp_path / "ceg.yaml"
    eie.write_bytes(DUPLICATES["sync"])
    ceg.write_bytes(b"domain: {id: plasticos, version: 1.0.0}\n")
    args = argparse.Namespace(eie_spec=eie, ceg_spec=ceg, eie_receipt=None, ceg_receipt=None)

    def forbidden(*_args, **_kwargs):
        pytest.fail("Ambiguous EIE input must be refused before subprocess invocation")

    monkeypatch.setattr(pair.subprocess, "run", forbidden)
    with pytest.raises(yaml.YAMLError, match="[Dd]uplicate"):
        pair.check_pair(args)


@pytest.fixture
def ceg_native_subject() -> tuple[Path, Path, bytes]:
    """Require the real pinned subject explicitly; never find or skip a sibling."""
    repo = Path(os.environ.get("L9_CEG_NATIVE_REPO", ROOT / ".ci-subjects/ceg")).resolve()
    python = Path(os.environ.get("L9_CEG_NATIVE_PYTHON", sys.executable)).absolute()
    if not repo.is_dir():
        pytest.skip("Pinned CEG subject is provisioned by the hosted native jobs")
    revision = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    assert revision.stdout.strip() == CEG_REVISION
    for relative in ("domains/plasticos/spec.yaml", "tools/validate_domain.py"):
        original = subprocess.run(
            ["git", "-C", str(repo), "show", f"{CEG_REVISION}:{relative}"],
            capture_output=True,
            check=True,
            timeout=10,
        ).stdout
        assert (repo / relative).read_bytes() == original
    return repo, python, (repo / "domains/plasticos/spec.yaml").read_bytes()


@pytest.mark.parametrize(
    ("workflow_name", "job_name"),
    [
        ("ci.yml", "test"),
        ("pr-pipeline.yml", "test"),
        ("pr-pipeline.yml", "l9"),
        ("l9-contract-control.yml", "select-and-run-gates"),
    ],
)
def test_hosted_native_jobs_provision_pinned_real_ceg_subject(workflow_name, job_name) -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows" / workflow_name).read_text())
    steps = workflow["jobs"][job_name]["steps"]
    checkouts = [
        step
        for step in steps
        if step.get("with", {}).get("repository") == "Quantum-L9/Cognitive.Engine.Graphs"
    ]
    assert len(checkouts) == 1
    checkout = checkouts[0]
    assert checkout["uses"] == "actions/checkout@v7"
    assert checkout["with"]["ref"] == CEG_REVISION
    assert checkout["with"]["path"] == ".ci-subjects/ceg"
    assert checkout["with"]["persist-credentials"] is False
    preparation = next(step for step in steps if step.get("name") == "Prepare native CEG validator")
    assert steps.index(checkout) < steps.index(preparation)
    run = preparation["run"]
    assert "requirements-runtime.lock" in run
    assert "--require-hashes" in run
    assert "L9_CEG_NATIVE_REPO=" in run and "L9_CEG_NATIVE_PYTHON=" in run
    assert "GITHUB_ENV" in run
    assert "validate_domain.py" in run
    assert not preparation.get("continue-on-error", False)
    assert "if" not in checkout and "if" not in preparation


def _compatible_ceg_bytes() -> bytes:
    """CEG's tenant spec as vendored in-repo (tests/fixtures/ceg), not a sibling checkout.

    Never derived from the EIE specimen: a CEG spec built from EIE's own
    declarations would make the typed row-contract comparison circular.
    """
    return (ROOT / "tests/fixtures/ceg/plasticos.spec.yaml").read_bytes()


def test_pair_ceg_native_cli_gets_exact_original_snapshot(tmp_path: Path, monkeypatch) -> None:
    from scripts import check_domain_compatibility as pair

    ceg_repo = tmp_path / "ceg-subject"
    (ceg_repo / "tools").mkdir(parents=True)
    (ceg_repo / "tools/validate_domain.py").write_bytes(
        b"# fixture validator; subprocess is patched\n"
    )
    ceg_bytes = _compatible_ceg_bytes()
    eie = tmp_path / "eie.yaml"
    ceg = tmp_path / "ceg.yaml"
    eie.write_bytes(GOOD)
    ceg.write_bytes(ceg_bytes)
    args = argparse.Namespace(
        eie_spec=eie,
        ceg_spec=ceg,
        eie_receipt=None,
        ceg_receipt=None,
        ceg_repo=ceg_repo,
        ceg_python=tmp_path / "python",
        field_mappings=None,
        output=tmp_path / "report.json",
    )
    snapshots = []

    def diagnostic_run(command, **_kwargs):
        if "rev-parse" in command:
            return SimpleNamespace(returncode=0, stdout=CEG_REVISION, stderr="")
        snapshots.append(Path(command[2]).read_bytes())
        assert command[3:] == [
            "--root",
            str(Path(command[2]).parent),
            "--strict",
            "--complete",
            "--json",
        ]
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")

    monkeypatch.setattr(pair.subprocess, "run", diagnostic_run)
    report, code = pair.check_pair(args)
    assert snapshots == [ceg_bytes]
    assert report["ceg_sha256"] == hashlib.sha256(ceg_bytes).hexdigest()
    assert code == 2  # Diagnostic fixture only; no real native CLI approval witness.
    assert report["release_ready"] is False


def test_pair_real_pinned_ceg_native_cli_and_compatibility(
    tmp_path: Path, ceg_native_subject
) -> None:
    """Run CEG's real CLI offline, retaining both native schemas and compatibility."""
    from scripts import check_domain_compatibility as pair

    ceg_repo, ceg_python, ceg_bytes = ceg_native_subject
    eie, ceg = tmp_path / "eie.yaml", tmp_path / "ceg.yaml"
    eie.write_bytes(GOOD)
    ceg.write_bytes(ceg_bytes)
    report, code = pair.check_pair(
        argparse.Namespace(
            eie_spec=eie,
            ceg_spec=ceg,
            eie_receipt=None,
            ceg_receipt=None,
            ceg_repo=ceg_repo,
            ceg_python=ceg_python,
            field_mappings=None,
            output=tmp_path / "diagnostic.json",
        )
    )
    assert report["ceg_native"]["status"] == "Passed", report
    assert report["ceg_native"]["evidence"]["exit_code"] == 0
    assert report["ceg_source_revision"] == CEG_REVISION
    assert report["ceg_sha256"] == hashlib.sha256(ceg_bytes).hexdigest()
    assert (
        report["ceg_validator_sha256"]
        == hashlib.sha256((ceg_repo / "tools/validate_domain.py").read_bytes()).hexdigest()
    )
    assert report["eie_native"]["status"] == "Passed"
    assert report["compatibility"]["verdict"] == "pass"
    assert code == 2 and report["release_ready"] is False
    assert all(item["status"] == "Unknown" for item in report["approvals"].values())


@pytest.mark.parametrize("seam", ["native_bytes", "scan_template"])
def test_parent_workflow_wiring_refuses_duplicate(seam: str) -> None:
    from app.services import domain_spec_workflow as workflow

    with pytest.raises(DomainPublicationError, match="Invalid.*YAML"):
        if seam == "native_bytes":
            workflow._native_bytes(DUPLICATES["name"], "plasticos")
        else:
            workflow.scan_to_seed(
                [], DUPLICATES["name"], tenant_id="plasticos", source_identifier="crm"
            )


async def test_parent_workflow_wiring_materialize_reparse_refuses_duplicate(monkeypatch) -> None:
    from app.services import domain_spec_workflow as workflow

    digest = hashlib.sha256(GOOD).hexdigest()
    record = SimpleNamespace(
        tenant_id="plasticos",
        domain="plasticos",
        node_label="Facility",
        discovery_spec_sha256=digest,
        ownership_contract_version=1,
        approval_status="approved",
        batch_run_id="test-only-batch",
        reviewed_by="test-only-reviewer",
        reviewed_at=datetime(2026, 1, 1, tzinfo=UTC),
        field_name="novel_field",
        field_type="string",
        source="crm",
        fill_rate=0.9,
        avg_confidence=0.9,
    )
    monkeypatch.setattr(
        workflow.pg_store, "get_approved_schema_proposals", AsyncMock(return_value=[record])
    )
    original_dump = workflow.yaml.safe_dump
    original_parse = workflow.parse_native_yaml
    generated = []
    parsed = []

    def duplicate_generated_yaml(candidate, **kwargs):
        serialized = original_dump(candidate, **kwargs)
        assert "novel_field" in serialized
        duplicate = "name: other\n" + serialized
        generated.append(duplicate.encode("utf-8"))
        return duplicate

    def observe_parse(content):
        parsed.append(content)
        return original_parse(content)

    monkeypatch.setattr(workflow.yaml, "safe_dump", duplicate_generated_yaml)
    monkeypatch.setattr(workflow, "parse_native_yaml", observe_parse)
    with pytest.raises(yaml.YAMLError, match="[Dd]uplicate"):
        await workflow.materialize_audited_spec(
            _parse(GOOD),
            tenant_id="plasticos",
            domain="plasticos",
            node_label="Facility",
            discovery_specs={digest: GOOD},
        )
    assert len(generated) == 1
    assert parsed == [GOOD, generated[0]]
