"""Explicit cross-repository proof, never a deployment or real approval.

The independent native owner is a Cognitive.Engine.Graphs checkout plus an
interpreter that can run its ``tools/validate_domain.py``. It is resolved from
CEG_REPO / CEG_PYTHON (operator-supplied; CI's ``integration`` job sets them to
its pinned CEG checkout), else from L9_CEG_NATIVE_REPO / L9_CEG_NATIVE_PYTHON,
the pinned native CEG subject the hosted ``test`` jobs already provision. EIE's
side runs in the interpreter running this suite. Without an owner the module
skips — or fails under EIE_REQUIRE_INTEGRATION=1.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from tests.conftest import skip_or_fail_unavailable

ROOT = Path(__file__).resolve().parents[2]
CEG_ROOT = os.environ.get("CEG_REPO") or os.environ.get("L9_CEG_NATIVE_REPO")
CEG_PYTHON = os.environ.get("CEG_PYTHON") or os.environ.get("L9_CEG_NATIVE_PYTHON")
pytestmark = [pytest.mark.integration]


@pytest.fixture(autouse=True)
def _require_native_owner() -> None:
    if not CEG_ROOT or not CEG_PYTHON:
        skip_or_fail_unavailable(
            "explicit independent CEG native checkout/interpreter not supplied "
            "(CEG_REPO/CEG_PYTHON or L9_CEG_NATIVE_REPO/L9_CEG_NATIVE_PYTHON)"
        )


def _inputs(tmp_path):
    eie = tmp_path / "eie-root/plasticos/spec.yaml"
    ceg = tmp_path / "ceg-root/plasticos/spec.yaml"
    eie.parent.mkdir(parents=True)
    ceg.parent.mkdir(parents=True)
    eie.write_bytes((ROOT / "domains/plasticos/spec.yaml").read_bytes())
    ceg.write_bytes((Path(CEG_ROOT) / "domains/plasticos/spec.yaml").read_bytes())
    return eie, ceg


def _reviewed_ceg(spec):
    raw = yaml.safe_load(spec.read_bytes())
    raw["domain"]["version"] = "1.3.0-reviewed"
    spec.write_text(yaml.safe_dump(raw, sort_keys=False))


def _receipt(spec, engine, output):
    raw = yaml.safe_load(spec.read_bytes())
    version = raw["version"] if engine == "eie" else raw["domain"]["version"]
    digest = hashlib.sha256(spec.read_bytes()).hexdigest()
    # Test evidence only: deliberately never persisted into an operator store.
    output.write_text(
        json.dumps(
            {
                "schema": f"{engine}.domain-approval/v1",
                "tenant_id": "plasticos",
                "version": version,
                "sha256": digest,
                "reviewed_input_sha256": digest,
                "reviewed_by": "isolated-test-fixture-only",
                "approval_ref": f"test-only-{engine}",
                "reviewed_at": "2026-11-03T21:00:00+00:00",
                "previous_sha256": None,
            }
        )
    )
    if engine == "eie":
        record = json.loads(output.read_text())
        record["approval_batches"] = []
        output.write_text(json.dumps(record))
    return output


def _run(tmp_path, eie, ceg, receipts=False):
    output = tmp_path / "pair-proof.json"
    cmd = [
        sys.executable,
        str(ROOT / "scripts/check_domain_compatibility.py"),
        "--eie-spec",
        str(eie),
        "--ceg-spec",
        str(ceg),
        "--ceg-repo",
        CEG_ROOT,
        "--ceg-python",
        CEG_PYTHON,
        "--output",
        str(output),
    ]
    if receipts:
        cmd += [
            "--eie-receipt",
            str(_receipt(eie, "eie", tmp_path / "eie-receipt.json")),
            "--ceg-receipt",
            str(_receipt(ceg, "ceg", tmp_path / "ceg-receipt.json")),
        ]
    result = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=90, check=False)
    return result.returncode, json.loads(output.read_text())


def test_native_fixtures_pass_with_two_distinct_hashes_but_approvals_unknown(tmp_path):
    eie, ceg = _inputs(tmp_path)
    code, proof = _run(tmp_path, eie, ceg)
    assert code == 2
    assert proof["eie_native"]["status"] == proof["ceg_native"]["status"] == "Passed"
    assert proof["compatibility"]["verdict"] == "pass"
    assert proof["eie_sha256"] != proof["ceg_sha256"]
    assert not proof["release_ready"]
    assert proof["approvals"]["eie"]["status"] == "Unknown"


@pytest.mark.parametrize("conflict", ["path", "targetnode"])
def test_both_native_specs_valid_yet_transport_pair_refused(tmp_path, conflict):
    eie, ceg = _inputs(tmp_path)
    raw = yaml.safe_load(eie.read_text())
    raw["sync"]["endpoints"][0][conflict] = {
        "path": "/v1/sync/absent",
        "targetnode": "Other",
    }[conflict]
    eie.write_text(yaml.safe_dump(raw))
    code, proof = _run(tmp_path, eie, ceg)
    assert code == 1
    assert proof["eie_native"]["status"] == proof["ceg_native"]["status"] == "Passed"
    assert proof["compatibility"]["verdict"] == "fail"
    assert not proof["release_ready"]


@pytest.mark.parametrize(
    ("conflict", "value", "reason"),
    [
        ("method", "PUT", "Unsupported sync.method"),
        (
            "idproperty",
            "unknown_id",
            "/v1/sync/facilities: idproperty unknown_id must be a declared scalar row property",
        ),
    ],
)
def test_eie_native_refuses_transport_fields_before_any_pair_check(
    tmp_path, conflict, value, reason
):
    """A sync method or id property EIE cannot send fails EIE's own validation.

    EIE's native schema checks these against its typed row contract, so the
    spec is refused on its own: CEG is never consulted and no compatibility
    verdict is produced.
    """
    eie, ceg = _inputs(tmp_path)
    raw = yaml.safe_load(eie.read_text())
    raw["sync"]["endpoints"][0][conflict] = value
    eie.write_text(yaml.safe_dump(raw))
    code, proof = _run(tmp_path, eie, ceg)
    assert code == 1
    assert proof["eie_native"] == {"status": "Failed", "reason": reason}
    assert "ceg_native" not in proof
    assert "compatibility" not in proof
    assert not proof["release_ready"]


def test_isolated_receipts_enable_exact_pair_record_not_identical_yaml(tmp_path):
    eie, ceg = _inputs(tmp_path)
    _reviewed_ceg(ceg)
    code, proof = _run(tmp_path, eie, ceg, receipts=True)
    assert code == 0 and proof["release_ready"]
    pair = proof["pair_record"]
    assert pair["eie"]["sha256"] != pair["ceg"]["sha256"]
    # The pair records the version of the EIE spec under test, whatever release it is.
    assert pair["eie"]["spec_version"] == yaml.safe_load(eie.read_bytes())["version"]
    assert pair["ceg"]["spec_version"] == "1.3.0-reviewed"


def test_post_review_tamper_refuses_release_even_if_native_spec_still_valid(tmp_path):
    eie, ceg = _inputs(tmp_path)
    _reviewed_ceg(ceg)
    eie_receipt = _receipt(eie, "eie", tmp_path / "eie-receipt.json")
    ceg_receipt = _receipt(ceg, "ceg", tmp_path / "ceg-receipt.json")
    raw = yaml.safe_load(eie.read_bytes())
    # A real post-review edit: a version the reviewed spec does not carry.
    raw["version"] = f"{raw['version']}-tampered"
    eie.write_text(yaml.safe_dump(raw))
    output = tmp_path / "tamper-proof.json"
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/check_domain_compatibility.py"),
            "--eie-spec",
            str(eie),
            "--ceg-spec",
            str(ceg),
            "--ceg-repo",
            CEG_ROOT,
            "--ceg-python",
            CEG_PYTHON,
            "--eie-receipt",
            str(eie_receipt),
            "--ceg-receipt",
            str(ceg_receipt),
            "--output",
            str(output),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    proof = json.loads(output.read_text())
    assert result.returncode == 1 and not proof["release_ready"]
    assert proof["approvals"]["eie"]["status"] == "Failed"


def test_independent_readonly_roots_refuse_partial_pair_and_restore_prior_pair(tmp_path):
    from app.engines.domain_spec_compatibility import verify_pair_record

    eie, ceg = _inputs(tmp_path)
    # Native graph authoring specimen is retained; this is a derived, isolated
    # reviewed-stage candidate with no production or business approval claim.
    graph = yaml.safe_load(ceg.read_bytes())
    graph["domain"]["version"] = "1.3.0-reviewed"
    ceg.write_text(yaml.safe_dump(graph, sort_keys=False))
    code, proof = _run(tmp_path, eie, ceg, receipts=True)
    assert code == 0
    original_eie, original_ceg = eie.read_bytes(), ceg.read_bytes()
    eie.chmod(0o444)
    ceg.chmod(0o444)
    pair = proof["pair_record"]

    def matches():
        return verify_pair_record(
            pair,
            tenant_id="plasticos",
            eie_sha256=hashlib.sha256(eie.read_bytes()).hexdigest(),
            ceg_sha256=hashlib.sha256(ceg.read_bytes()).hexdigest(),
        )

    assert matches()
    # An independently changed EIE current artifact cannot reuse the old pair.
    changed = yaml.safe_load(original_eie)
    changed["version"] = "0.4.0"
    eie.chmod(0o644)
    eie.write_text(yaml.safe_dump(changed))
    eie.chmod(0o444)
    assert not matches()
    # Restore exact approved bytes, not a newly inferred merged artifact.
    eie.chmod(0o644)
    eie.write_bytes(original_eie)
    eie.chmod(0o444)
    assert ceg.read_bytes() == original_ceg
    assert matches()
