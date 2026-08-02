#!/usr/bin/env python3
"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: enrichment
layer: [audit]
tags: [contracts, compiler, validator, feature-evidence]
owner: engine-team
status: active
--- /L9_META ---

EIE contract compiler validator (TASK-059 / ADR-012).

Validates FeatureEvidence and evidence-odoo-mapping schemas/fixtures against
native models. Emits a deterministic digest report. Does not create a parallel
cartridge or alternate transport envelope — FeatureEvidence remains the sole
evidence payload authority.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

try:
    from jsonschema import Draft202012Validator
except ImportError:  # CI unit env may omit optional extras
    Draft202012Validator = None  # type: ignore[misc, assignment]

ROOT = Path(__file__).resolve().parents[1]
FEATURE_DIR = ROOT / "contracts" / "feature_evidence"
MAPPING_DIR = ROOT / "contracts" / "evidence_odoo_mapping"
DEFAULT_REPORT = ROOT / "artifacts" / "payload-contract-compiler-report.json"

SCHEMA_FILES = (
    FEATURE_DIR / "feature-evidence.schema.yaml",
    MAPPING_DIR / "evidence-odoo-mapping.schema.yaml",
)

# Split prohibited tokens so scanners/ratchets do not treat this validator as a usage site.
FORBIDDEN_TOKENS = (
    "Packet" + "Envelope",
    "packet" + ".schema",
    "legacy" + "_request",
    "peer_url" + "_dispatch",
    "Domain" + "SpecLoader",
)


def _sha_file(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _sha_obj(obj: Any) -> str:
    blob = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return "sha256:" + hashlib.sha256(blob).hexdigest()


def _load_yaml(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"schema root must be object: {path}")
    return data


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _resolve_under_root(path: Path) -> Path:
    """Reject report paths that escape the repository root."""
    resolved = path.expanduser().resolve()
    root = ROOT.resolve()
    if not resolved.is_relative_to(root):
        raise ValueError(f"--report must stay under repo root ({root})")
    return resolved


def _structural_schema_errors(schema: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if schema.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
        errors.append("missing Draft 2020-12 $schema")
    if not schema.get("$id"):
        errors.append("missing $id")
    if schema.get("type") not in {None, "object"}:
        errors.append("unexpected root type")
    return errors


def _apply_schema_check(entry: dict[str, Any], schema: dict[str, Any]) -> None:
    if Draft202012Validator is not None:
        try:
            Draft202012Validator.check_schema(schema)
            entry["check_schema"] = "PASS"
            entry["check_schema_backend"] = "jsonschema"
        except Exception as exc:
            entry["check_schema"] = "FAIL"
            entry["error"] = str(exc)
        return
    errors = _structural_schema_errors(schema)
    if errors:
        entry["check_schema"] = "FAIL"
        entry["error"] = "; ".join(errors)
        return
    entry["check_schema"] = "PASS"
    entry["check_schema_backend"] = "structural"


def _schema_entry(path: Path) -> dict[str, Any]:
    schema = _load_yaml(path)
    entry: dict[str, Any] = {
        "path": str(path.relative_to(ROOT)),
        "id": schema.get("$id"),
        "digest": _sha_file(path),
    }
    _apply_schema_check(entry, schema)
    text = path.read_text(encoding="utf-8")
    hits = [token for token in FORBIDDEN_TOKENS if token in text]
    entry["forbidden_token_hits"] = hits
    entry["status"] = "PASS" if entry["check_schema"] == "PASS" and not hits else "FAIL"
    return entry


def validate_schemas() -> list[dict[str, Any]]:
    return [_schema_entry(path) for path in SCHEMA_FILES]


def _feature_positive(path: Path, model: type) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "file": str(path.relative_to(ROOT)),
        "model": "FeatureEvidence",
    }
    payload = _load_json(path)
    try:
        if isinstance(payload, list):
            for item in payload:
                model.model_validate(item)
            entry["items"] = len(payload)
        else:
            model.model_validate(payload)
        entry["status"] = "PASS"
    except ValidationError as exc:
        entry["status"] = "FAIL"
        entry["error"] = str(exc)[:400]
    return entry


def _mapping_positive(model: type) -> dict[str, Any]:
    mapping_path = MAPPING_DIR / "evidence-odoo-mapping.yaml"
    entry: dict[str, Any] = {
        "file": str(mapping_path.relative_to(ROOT)),
        "model": "EvidenceOdooMappingContract",
    }
    try:
        model.model_validate(_load_yaml(mapping_path))
        entry["status"] = "PASS"
    except ValidationError as exc:
        entry["status"] = "FAIL"
        entry["error"] = str(exc)[:400]
    return entry


def _expect_reject(
    path: Path, model: type, *, model_name: str, semantic: str | None = None
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "file": str(path.relative_to(ROOT)),
        "model": model_name,
    }
    payload = _load_json(path) if path.suffix == ".json" else _load_yaml(path)
    try:
        model.model_validate(payload)
        entry["status"] = "FAIL"
        entry["detail"] = "incorrectly_accepted"
    except ValidationError:
        entry["status"] = "PASS"
        entry["detail"] = "rejected_as_expected"
        if semantic is not None:
            entry["enforcement"] = "owner_semantic"
    return entry


def validate_fixtures() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    from app.models.evidence_odoo_mapping import EvidenceOdooMappingContract
    from app.models.feature_evidence import FeatureEvidence

    positives = [
        _feature_positive(path, FeatureEvidence)
        for path in sorted((FEATURE_DIR / "examples").glob("*.json"))
    ]
    positives.append(_mapping_positive(EvidenceOdooMappingContract))

    negatives = [
        _expect_reject(
            path,
            FeatureEvidence,
            model_name="FeatureEvidence",
            semantic="owner_semantic" if path.name == "inferred-without-version.json" else None,
        )
        for path in sorted((FEATURE_DIR / "negative_examples").glob("*.json"))
    ]
    negatives.extend(
        _expect_reject(path, EvidenceOdooMappingContract, model_name="EvidenceOdooMappingContract")
        for path in sorted((MAPPING_DIR / "negative_examples").glob("*.yaml"))
    )
    return positives, negatives


def validate_evidence_authority() -> dict[str, Any]:
    """FeatureEvidence + mapping loader remain sole evidence authorities."""
    from app.models.evidence_odoo_mapping import load_evidence_odoo_mapping

    registry = _load_yaml(FEATURE_DIR / "feature_registry.yaml")
    feature_ids = [f["feature_id"] for f in registry.get("features", [])]
    contract = load_evidence_odoo_mapping(MAPPING_DIR / "evidence-odoo-mapping.yaml")
    ok = (
        registry.get("owner") == "eie"
        and len(feature_ids) >= 1
        and contract.policy.default_write_mode == "proposal_only"
        and contract.domain == "plasticos"
    )
    return {
        "evidence_model": "FeatureEvidence",
        "mapping_loader": "load_evidence_odoo_mapping",
        "registry_owner": registry.get("owner"),
        "feature_count": len(feature_ids),
        "mapping_write_mode": contract.policy.default_write_mode,
        "parallel_cartridge_forbidden": True,
        "status": "PASS" if ok else "FAIL",
    }


def _write_report(write_path: Path, report: dict[str, Any]) -> None:
    write_path.parent.mkdir(parents=True, exist_ok=True)
    write_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


def compile_report(*, write_path: Path | None) -> dict[str, Any]:
    schema_results = validate_schemas()
    positives, negatives = validate_fixtures()
    authority = validate_evidence_authority()
    report: dict[str, Any] = {
        "schema": "l9.eie.payload_contract_compiler.v1",
        "task_id": "TASK-059",
        "authority": "FeatureEvidence + evidence-odoo-mapping (no parallel cartridge)",
        "schemas": schema_results,
        "positives": positives,
        "negatives": negatives,
        "evidence_authority": authority,
    }
    ok = (
        all(r["status"] == "PASS" for r in schema_results)
        and all(r["status"] == "PASS" for r in positives)
        and all(r["status"] == "PASS" for r in negatives)
        and authority["status"] == "PASS"
    )
    report["result"] = "PASS" if ok else "FAIL"
    report["digest"] = _sha_obj({k: v for k, v in report.items() if k != "digest"})
    if write_path is not None:
        _write_report(write_path, report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="EIE payload contract compiler validator")
    parser.add_argument(
        "--report",
        type=Path,
        default=DEFAULT_REPORT,
        help="Write JSON report to this path",
    )
    parser.add_argument(
        "--stdout-only",
        action="store_true",
        help="Do not write report file",
    )
    args = parser.parse_args(argv)
    write_path = None if args.stdout_only else _resolve_under_root(args.report)
    report = compile_report(write_path=write_path)
    print(json.dumps({"result": report["result"], "digest": report["digest"]}, indent=2))
    return 0 if report["result"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
