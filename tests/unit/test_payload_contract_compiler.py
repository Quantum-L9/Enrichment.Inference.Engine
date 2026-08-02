"""TASK-059: EIE payload contract compiler validator."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
COMPILER = ROOT / "tools" / "payload_contract_compiler.py"


def _load_compiler():
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    spec = importlib.util.spec_from_file_location("payload_contract_compiler", COMPILER)
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_compiler_report_passes(tmp_path: Path) -> None:
    mod = _load_compiler()
    report_path = tmp_path / "report.json"
    report = mod.compile_report(write_path=report_path)
    assert report["result"] == "PASS", json.dumps(report, indent=2)[:2000]
    assert report_path.is_file()
    assert report["evidence_authority"]["evidence_model"] == "FeatureEvidence"
    assert report["evidence_authority"]["feature_count"] >= 1
    assert all(s["status"] == "PASS" for s in report["schemas"])
    assert all(p["status"] == "PASS" for p in report["positives"])
    assert all(n["status"] == "PASS" for n in report["negatives"])


def test_compiler_cli_exit_zero() -> None:
    mod = _load_compiler()
    report_path = ROOT / "artifacts" / "payload-contract-compiler-cli-test.json"
    try:
        assert mod.main(["--report", str(report_path)]) == 0
        data = json.loads(report_path.read_text(encoding="utf-8"))
        assert data["result"] == "PASS"
    finally:
        if report_path.exists():
            report_path.unlink()


def test_compiler_cli_rejects_escape_path(tmp_path: Path) -> None:
    mod = _load_compiler()
    with pytest.raises(ValueError, match="repo root"):
        mod.main(["--report", str(tmp_path / "escape.json")])


def test_contract_schemas_exist() -> None:
    assert (ROOT / "contracts/feature_evidence/feature-evidence.schema.yaml").is_file()
    assert (ROOT / "contracts/evidence_odoo_mapping/evidence-odoo-mapping.schema.yaml").is_file()
