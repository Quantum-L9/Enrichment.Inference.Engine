#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
SPEC_PATH = REPO_ROOT / "docs/contracts/enforcement/phase5-readiness.yaml"
_CHASSIS_ENDPOINT_PATH = "app/api/v1/chassis_endpoint.py"


@dataclass
class TruthResult:
    name: str
    passed: bool
    evidence: list[str]
    errors: list[str]


def _load_yaml(path: Path) -> dict[str, Any]:
    result = yaml.safe_load(path.read_text(encoding="utf-8"))
    return dict(result) if result else {}


def _run_command(command: list[str]) -> tuple[bool, str]:
    completed = subprocess.run(
        command,
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
    )
    output = "\n".join(
        part for part in [completed.stdout.strip(), completed.stderr.strip()] if part
    ).strip()
    return completed.returncode == 0, output


def _check_required_files(paths: list[str]) -> tuple[list[str], list[str]]:
    present: list[str] = []
    missing: list[str] = []
    for relative_path in paths:
        file_path = REPO_ROOT / relative_path
        if file_path.exists():
            present.append(relative_path)
        else:
            missing.append(relative_path)
    return present, missing


def _ensure_runtime_imports() -> tuple[bool, list[str], list[str]]:
    evidence: list[str] = []
    errors: list[str] = []

    try:
        from fastapi import FastAPI

        from app.api.v1.attestation import get_runtime_attestation
        from app.bootstrap.l9_contract_runtime import install_l9_contract_controls
        from app.services.action_authority import authorize_action
        from app.services.dependency_enforcement import evaluate_action_dependencies
        from app.services.event_contract_guard import emit_event, to_stream_dict, validate_event
        from app.services.packet_enforcement import (
            build_egress_packet,
            canonical_packet_hash,
            validate_ingress_packet,
        )
        from app.services.runtime_attestation import build_runtime_attestation
    except Exception as exc:  # pragma: no cover - direct readiness failure
        return False, evidence, [f"runtime import failure: {exc}"]

    evidence.append("runtime imports resolved")

    packet_payload = {
        "entity": {"Name": "Acme Recycling Corp"},
        "object_type": "Account",
        "objective": "Enrich polymer data",
    }
    ingress_packet = {
        "packet_id": "ingress-pkt-001",
        "action": "enrich",
        "payload": packet_payload,
        "tenant": "acme-corp",
        "source_node": "score-engine",
        "reply_to": "route-engine",
        "lineage": {
            "parent_ids": ["root-pkt-000"],
            "root_id": "root-pkt-000",
            "generation": 2,
            "derivation_type": "dispatch",
        },
        "governance": {"intent": "enrichment"},
        "hop_trace": [
            {
                "node": "gateway",
                "action": "dispatch",
                "status": "completed",
                "timestamp": "2026-04-06T20:00:00+00:00",
            }
        ],
        "delegation_chain": [],
    }
    action_str: str = str(ingress_packet["action"])
    payload_dict: dict[str, Any] = dict(ingress_packet["payload"])  # type: ignore[arg-type]
    tenant_str: str = str(ingress_packet["tenant"])
    ingress_packet["content_hash"] = canonical_packet_hash(
        action_str,
        payload_dict,
        tenant_str,
    )

    try:
        normalized_packet = validate_ingress_packet(ingress_packet)
        egress_packet = build_egress_packet(
            normalized_packet,
            {"status": "completed"},
            current_node="enrichment-engine",
            policy_cleared=True,
        )
        evidence.append(
            f"packet runtime enforcement live (egress generation={egress_packet['lineage']['generation']})"
        )
    except Exception as exc:
        errors.append(f"packet runtime enforcement failed: {exc}")

    try:
        ready_attestation = build_runtime_attestation()
        evidence.append(
            f"runtime attestation built (contract_digest={ready_attestation['contract_digest']})"
        )
    except Exception as exc:
        ready_attestation = None
        errors.append(f"runtime attestation build failed: {exc}")

    if ready_attestation is not None:
        try:
            enrich_eval = evaluate_action_dependencies("enrich", attestation=ready_attestation)
            evidence.append(f"dependency evaluation live (enrich allowed={enrich_eval['allowed']})")
        except Exception as exc:
            errors.append(f"dependency evaluation failed: {exc}")

        writeback_payload = {
            "crm_type": "salesforce",
            "object_type": "Account",
            "record_id": "001ABC",
            "enriched_data": {"industry": "Manufacturing"},
            "_field_confidences": {"industry": 0.95},
        }

        try:
            authorize_action(
                "writeback",
                payload=writeback_payload,
                policy_cleared=True,
                attestation=ready_attestation,
            )
            evidence.append("constitution-backed writeback authorization live")
        except Exception as exc:
            errors.append(f"writeback authorization failed: {exc}")

    event = {
        "event_type": "enrichment_completed",
        "entity_id": "001B000000LpT1FIAV",
        "tenant_id": "acme-corp",
        "domain": "plasticos",
        "payload": {"fields_count": 8},
        "correlation_id": "550e8400-e29b-41d4-a716-446655440000",
        "occurred_at": "2026-04-05T19:54:14+00:00",
    }
    try:
        normalized_event = validate_event(event)
        wire = to_stream_dict(normalized_event)
        emit_result = emit_event(lambda _: None, normalized_event, raise_on_failure=False)
        evidence.append(
            f"event contract guard live (wire_keys={sorted(wire.keys())}, emitted={emit_result['emitted']})"
        )
    except Exception as exc:
        errors.append(f"event contract guard failed: {exc}")

    try:
        app = FastAPI()
        install_l9_contract_controls(app)
        attestation_payload = get_runtime_attestation()
        evidence.append(
            f"bootstrap + attestation route payload ready (node_id={attestation_payload['node_id']})"
        )
    except Exception as exc:
        errors.append(f"bootstrap or attestation route failed: {exc}")

    return len(errors) == 0, evidence, errors


def _repo_truth(spec: dict[str, Any], *, run_pytest: bool) -> TruthResult:
    section = spec["truths"]["repo_truth"]
    present, missing = _check_required_files(section["required_files"])

    ok_constitution, constitution_output = _run_command(
        [sys.executable, "scripts/l9_contract_control.py", "verify-constitution"]
    )
    ok_attestation, attestation_output = _run_command(
        [sys.executable, "scripts/l9_contract_control.py", "verify-attestation"]
    )

    repo_evidence = [f"present file: {path}" for path in present]
    repo_errors = [f"missing file: {path}" for path in missing]

    if ok_constitution:
        repo_evidence.append("constitution verification passed")
    else:
        repo_errors.append(f"constitution verification failed: {constitution_output}")

    if ok_attestation:
        repo_evidence.append("attestation verification passed")
    else:
        repo_errors.append(f"attestation verification failed: {attestation_output}")

    if run_pytest:
        command = [
            sys.executable,
            "-m",
            "pytest",
            *section["pytest_files"],
            "-q",
            "--disable-warnings",
            "--maxfail=1",
        ]
        ok_pytest, pytest_output = _run_command(command)
        if ok_pytest:
            repo_evidence.append("repo readiness pytest subset passed")
        else:
            repo_errors.append(f"repo readiness pytest subset failed: {pytest_output}")

    return TruthResult(
        name="repo_truth",
        passed=len(repo_errors) == 0,
        evidence=repo_evidence,
        errors=repo_errors,
    )


def _runtime_truth(spec: dict[str, Any]) -> TruthResult:
    section = spec["truths"]["runtime_truth"]
    present, missing = _check_required_files(section["required_files"])
    ok_runtime, runtime_evidence, runtime_errors = _ensure_runtime_imports()

    errors = [f"missing file: {path}" for path in missing] + runtime_errors
    pass_evidence = [f"present file: {path}" for path in present] + runtime_evidence
    if ok_runtime:
        pass_evidence.append("runtime import and execution checks passed")

    return TruthResult(
        name="runtime_truth",
        passed=len(errors) == 0,
        evidence=pass_evidence,
        errors=errors,
    )


def _governance_truth(spec: dict[str, Any]) -> TruthResult:
    section = spec["truths"]["governance_truth"]
    present, missing = _check_required_files(section["required_files"])
    errors = [f"missing file: {path}" for path in missing]
    pass_evidence = [f"present file: {path}" for path in present]

    try:
        from app.services.action_authority import (
            ActionAuthorizationError,
            authorize_action,
            get_action_policy,
            get_tool_policy,
        )

        policy = get_action_policy("writeback")
        tool_policy = get_tool_policy("writeback")
        if policy["mutation_class"] != "external_mutation":
            errors.append("writeback action mutation_class is not external_mutation")
        else:
            pass_evidence.append("writeback action mutation_class is external_mutation")

        if policy["approval_mode"] != "threshold_or_human":
            errors.append("writeback action approval_mode is not threshold_or_human")
        else:
            pass_evidence.append("writeback action approval_mode is threshold_or_human")

        if tool_policy["chassis_action"] != "writeback":
            errors.append("writeback tool does not map to writeback chassis action")
        else:
            pass_evidence.append("writeback tool maps to writeback chassis action")

        attestation = {
            "dependency_readiness": {
                "PerplexitySONAR": {
                    "required": True,
                    "ready": True,
                    "env_vars": [],
                    "missing_env": [],
                },
                "Redis": {"required": False, "ready": True, "env_vars": [], "missing_env": []},
                "PostgreSQL": {"required": False, "ready": True, "env_vars": [], "missing_env": []},
                "ConstellationGate": {
                    "required": True,
                    "ready": True,
                    "env_vars": [],
                    "missing_env": [],
                },
                "OdooCRM": {"required": False, "ready": False, "env_vars": [], "missing_env": []},
                "SalesforceCRM": {
                    "required": False,
                    "ready": True,
                    "env_vars": [],
                    "missing_env": [],
                },
                "HubSpotCRM": {
                    "required": False,
                    "ready": False,
                    "env_vars": [],
                    "missing_env": [],
                },
                "OpenAI": {"required": False, "ready": False, "env_vars": [], "missing_env": []},
                "Anthropic": {"required": False, "ready": False, "env_vars": [], "missing_env": []},
            }
        }

        try:
            authorize_action(
                "writeback",
                payload={
                    "crm_type": "salesforce",
                    "object_type": "Account",
                    "record_id": "001ABC",
                    "enriched_data": {"industry": "Manufacturing"},
                    "_field_confidences": {"industry": 0.95},
                },
                policy_cleared=False,
                attestation=attestation,
            )
            errors.append("writeback authorization did not block missing policy clearance")
        except ActionAuthorizationError:
            pass_evidence.append("writeback authorization blocks missing policy clearance")

        authorize_action(
            "writeback",
            payload={
                "crm_type": "salesforce",
                "object_type": "Account",
                "record_id": "001ABC",
                "enriched_data": {"industry": "Manufacturing"},
                "_field_confidences": {"industry": 0.95},
            },
            policy_cleared=True,
            attestation=attestation,
        )
        pass_evidence.append(
            "writeback authorization succeeds when constitution conditions are met"
        )
    except Exception as exc:
        errors.append(f"governance truth execution failed: {exc}")

    return TruthResult(
        name="governance_truth",
        passed=len(errors) == 0,
        evidence=pass_evidence,
        errors=errors,
    )


def _operational_truth(spec: dict[str, Any]) -> TruthResult:
    section = spec["truths"]["operational_truth"]
    present, missing = _check_required_files(section["required_files"])
    errors = [f"missing file: {path}" for path in missing]
    pass_evidence = [f"present file: {path}" for path in present]

    try:
        from app.services.runtime_attestation import build_runtime_attestation

        attestation = build_runtime_attestation()
        required_fields = {
            "contract_digest",
            "dependency_readiness",
            "degraded_modes",
            "policy_mode",
        }
        missing_fields = [field for field in sorted(required_fields) if field not in attestation]
        if missing_fields:
            errors.append(f"runtime attestation missing operational fields: {missing_fields}")
        else:
            pass_evidence.append(
                "runtime attestation exposes contract_digest, dependency_readiness, degraded_modes, and policy_mode"
            )

        if not isinstance(attestation["dependency_readiness"], dict):
            errors.append("dependency_readiness is not an object")
        if not isinstance(attestation["degraded_modes"], list):
            errors.append("degraded_modes is not a list")
    except Exception as exc:
        errors.append(f"operational truth execution failed: {exc}")

    return TruthResult(
        name="operational_truth",
        passed=len(errors) == 0,
        evidence=pass_evidence,
        errors=errors,
    )


def _agent_truth(spec: dict[str, Any]) -> TruthResult:
    section = spec["truths"]["agent_truth"]
    present, missing = _check_required_files(section["required_files"])
    errors = [f"missing file: {path}" for path in missing]
    pass_evidence = [f"present file: {path}" for path in present]

    try:
        from scripts.l9_contract_control import build_review_signal, select_gates

        selection = select_gates(
            [
                _CHASSIS_ENDPOINT_PATH,
                "docs/contracts/api/openapi.yaml",
                "tests/contracts/tier2/test_enforcement_packet_runtime.py",
            ]
        )
        if "constitution_verify" not in selection.ids:
            errors.append("gate selection did not include constitution_verify")
        else:
            pass_evidence.append("gate selection includes constitution_verify")

        ok_block, _ = build_review_signal([_CHASSIS_ENDPOINT_PATH])
        if ok_block:
            errors.append(
                "review signal did not block contract-bound change without companion updates"
            )
        else:
            pass_evidence.append(
                "review signal blocks contract-bound change without companion updates"
            )

        ok_pass, markdown_pass = build_review_signal(
            [
                _CHASSIS_ENDPOINT_PATH,
                "docs/contracts/api/openapi.yaml",
                "docs/contracts/node.constitution.yaml",
                "tests/contracts/tier2/test_enforcement_packet_runtime.py",
            ]
        )
        if not ok_pass:
            errors.append(f"review signal did not pass with companion updates: {markdown_pass}")
        else:
            pass_evidence.append(
                "review signal passes when companion contract/test surfaces are present"
            )
    except Exception as exc:
        errors.append(f"agent truth execution failed: {exc}")

    return TruthResult(
        name="agent_truth",
        passed=len(errors) == 0,
        evidence=pass_evidence,
        errors=errors,
    )


def run_phase5_readiness(*, run_pytest: bool = True) -> dict[str, Any]:
    spec = _load_yaml(SPEC_PATH)
    results = [
        _repo_truth(spec, run_pytest=run_pytest),
        _runtime_truth(spec),
        _governance_truth(spec),
        _operational_truth(spec),
        _agent_truth(spec),
    ]

    overall_ready = all(result.passed for result in results)
    return {
        "overall_ready_for_phase5": overall_ready,
        "results": [asdict(result) for result in results],
    }


def _render_markdown(report: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("# Phase 5 Readiness Report")
    lines.append("")
    lines.append(
        f"Overall ready for phase 5: **{'YES' if report['overall_ready_for_phase5'] else 'NO'}**"
    )
    lines.append("")

    for result in report["results"]:
        lines.append(f"## {result['name']}")
        lines.append("")
        lines.append(f"Passed: **{'YES' if result['passed'] else 'NO'}**")
        lines.append("")
        lines.append("### Evidence")
        for item in result["evidence"]:
            lines.append(f"- {item}")
        lines.append("")
        lines.append("### Errors")
        if result["errors"]:
            for item in result["errors"]:
                lines.append(f"- {item}")
        else:
            lines.append("- none")
        lines.append("")

    return "\n".join(lines)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate readiness for phase 5")
    parser.add_argument(
        "--no-pytest",
        action="store_true",
        help="skip repo truth pytest subset during readiness validation",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit JSON instead of markdown",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    report = run_phase5_readiness(run_pytest=not args.no_pytest)

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(_render_markdown(report))

    return 0 if report["overall_ready_for_phase5"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
