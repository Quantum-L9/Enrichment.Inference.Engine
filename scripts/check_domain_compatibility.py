#!/usr/bin/env python3
"""Check an independent EIE/CEG pair, without compiling or merging either YAML.

Exit 0: native validation, approval evidence and compatibility all pass.
Exit 1: validation/compatibility/approval evidence refused.
Exit 2: diagnostic compatibility passes but approvals are Unknown (not release-ready).

The supplied CEG checkout/interpreter are explicit operator execution inputs.
They must be trusted by the operator; accepting untrusted executable inputs is
not a supported sandbox boundary. Artifact reads/writes refuse symlink components.
No remote service, package installation, deployment, or publication is performed.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import yaml

from app.engines.domain_spec_compatibility import RULE_SET, check_compatibility
from app.engines.domain_yaml_reader import (
    _json_object,
    _validate_receipt,
    is_legal_tenant_id,
    validate_native_spec,
)
from app.engines.native_yaml import parse_native_yaml
from app.services import domain_spec_workflow as workflow
from scripts.domain_spec_operator import _parent, _read


def _write_report(path: Path, content: bytes) -> None:
    """Atomically replace only a private regular child, never follow a link.

    Unlike reviewed operator artifacts, a diagnostic report may be overwritten.
    Stage complete bytes first so a write failure preserves the previous report.
    Parent directories must already exist, as with the original report writer.
    """
    with _parent(str(path)) as (parent_fd, name):
        previous = None
        try:
            descriptor = os.open(name, workflow._FILE_FLAGS, dir_fd=parent_fd)
        except FileNotFoundError:
            previous = None
        else:
            try:
                previous = os.fstat(descriptor)
                if not stat.S_ISREG(previous.st_mode) or previous.st_nlink != 1:
                    raise ValueError("Report output must be a private regular file")
            finally:
                os.close(descriptor)
        staging = f".pair-report-{uuid.uuid4().hex}"
        try:
            workflow._write_file(parent_fd, staging, content, mode=0o600)
            if previous is None:
                # A concurrent new child must not be silently overwritten.
                os.link(
                    staging,
                    name,
                    src_dir_fd=parent_fd,
                    dst_dir_fd=parent_fd,
                    follow_symlinks=False,
                )
            else:
                current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                if (
                    not stat.S_ISREG(current.st_mode)
                    or current.st_nlink != 1
                    or (current.st_dev, current.st_ino) != (previous.st_dev, previous.st_ino)
                ):
                    raise ValueError("Report output changed during write")
                # replace never follows the destination, even if changed after stat.
                os.replace(staging, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            os.fsync(parent_fd)
        finally:
            try:
                os.unlink(staging, dir_fd=parent_fd)
            except FileNotFoundError:
                staging = ""


def _receipt(data: bytes | None, engine: str, tenant: str, version: str, digest: str) -> dict:
    if data is None:
        return {"status": "Unknown", "reason": "approval receipt not supplied"}
    if len(data) > 16 * 1024:
        return {"status": "Failed", "reason": "approval receipt exceeds native bound"}
    try:
        raw = _json_object(data)
        if engine == "eie":
            _validate_receipt(raw, tenant, digest, version)
    except (ValueError, TypeError, RecursionError) as exc:
        return {"status": "Failed", "reason": str(exc)}
    valid = (
        raw.get("schema") == f"{engine}.domain-approval/v1"
        and raw.get("tenant_id") == tenant
        and raw.get("version") == version
        and raw.get("sha256") == digest
        and raw.get("reviewed_input_sha256") == digest
        and isinstance(raw.get("reviewed_by"), str)
        and raw["reviewed_by"].strip()
        and isinstance(raw.get("approval_ref"), str)
        and raw["approval_ref"].strip()
    )
    try:
        timestamp = datetime.datetime.fromisoformat(raw.get("reviewed_at", ""))
        valid = valid and timestamp.tzinfo is not None
    except (ValueError, TypeError):
        valid = False
    if not valid:
        return {
            "status": "Failed",
            "reason": "receipt identity, review coordinates or bytes mismatch",
        }
    return {
        "status": "Passed",
        "approval_ref": raw["approval_ref"],
        "reviewed_by": raw["reviewed_by"],
        "authentication": "operator-supplied; not cryptographically authenticated",
    }


def check_pair(args: argparse.Namespace) -> tuple[dict, int]:
    eie_bytes, ceg_bytes = _read(str(args.eie_spec)), _read(str(args.ceg_spec))
    eie_receipt_bytes = _read(str(args.eie_receipt)) if args.eie_receipt else None
    ceg_receipt_bytes = _read(str(args.ceg_receipt)) if args.ceg_receipt else None
    eie_hash, ceg_hash = (
        hashlib.sha256(eie_bytes).hexdigest(),
        hashlib.sha256(ceg_bytes).hexdigest(),
    )
    eie, ceg = parse_native_yaml(eie_bytes), parse_native_yaml(ceg_bytes)
    mapping_path = getattr(args, "field_mappings", None)
    mappings = json.loads(_read(str(mapping_path))) if mapping_path else None
    tenant = eie.get("name") if isinstance(eie, dict) else None
    report = {
        "schema": "eie.domain-pair-check/v1",
        "rule_set": RULE_SET,
        "observed_at": datetime.datetime.now(datetime.UTC).isoformat(),
        "eie_sha256": eie_hash,
        "ceg_sha256": ceg_hash,
        "release_ready": False,
    }
    rule_path = Path(__file__).resolve().parents[1] / "app/engines/domain_spec_compatibility.py"
    report["rule_set_sha256"] = hashlib.sha256(rule_path.read_bytes()).hexdigest()
    revision = subprocess.run(
        ["git", "-C", str(args.ceg_repo), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    report["ceg_source_revision"] = (
        revision.stdout.strip() if revision.returncode == 0 else "Unknown"
    )
    report["ceg_validator_sha256"] = hashlib.sha256(
        (args.ceg_repo / "tools/validate_domain.py").read_bytes()
    ).hexdigest()
    try:
        if not isinstance(tenant, str):
            raise ValueError("Native EIE tenant must be a string")
        validate_native_spec(eie, tenant)
        report["eie_native"] = {"status": "Passed"}
    except (ValueError, TypeError) as exc:
        report["eie_native"] = {"status": "Failed", "reason": str(exc)}
        return report, 1
    # CEG's native CLI reads a private snapshot of the exact hashed input bytes.
    # This prevents mutation between hashing and native validation from proving
    # one file while the pair record points at another.
    with tempfile.TemporaryDirectory(prefix="native-domain-pair-") as temp:
        ceg_tenant = ceg.get("domain", {}).get("id") if isinstance(ceg, dict) else None
        if not isinstance(ceg_tenant, str) or not is_legal_tenant_id(ceg_tenant):
            report["ceg_native"] = {"status": "Failed", "reason": "illegal native CEG tenant"}
            return report, 1
        snapshot = Path(temp) / ceg_tenant / "spec.yaml"
        snapshot.parent.mkdir()
        snapshot.write_bytes(ceg_bytes)
        native_args = [
            str(snapshot),
            "--root",
            str(snapshot.parent),
            "--strict",
            "--complete",
            "--json",
        ]
        if ceg_receipt_bytes is not None:
            snapshot_receipt = snapshot.parent / "receipt.json"
            snapshot_receipt.write_bytes(ceg_receipt_bytes)
            native_args.extend(["--receipt", str(snapshot_receipt)])
        if args.eie_receipt is not None and args.ceg_receipt is not None:
            native_args.append("--require-reviewed")
        result = subprocess.run(
            [
                str(args.ceg_python),
                str(args.ceg_repo / "tools/validate_domain.py"),
                *native_args,
            ],
            cwd=args.ceg_repo,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
            env={**os.environ, "PYTHONPATH": str(args.ceg_repo)},
        )
    try:
        native = json.loads(result.stdout)
    except json.JSONDecodeError:
        native = {"reason": result.stderr[-4000:] or result.stdout[-4000:]}
    report["ceg_native"] = {
        "status": "Passed" if result.returncode == 0 else "Failed",
        "evidence": native,
    }
    if result.returncode != 0:
        return report, 1
    proof = check_compatibility(eie, ceg, field_mappings=mappings)
    report["compatibility"] = proof
    if proof["verdict"] != "pass":
        return report, 1
    approvals = {
        "eie": _receipt(eie_receipt_bytes, "eie", tenant, proof["eie_version"], eie_hash),
        "ceg": _receipt(ceg_receipt_bytes, "ceg", tenant, proof["ceg_version"], ceg_hash),
    }
    report["approvals"] = approvals
    if any(item["status"] == "Failed" for item in approvals.values()):
        return report, 1
    if any(item["status"] == "Unknown" for item in approvals.values()):
        return report, 2
    report["release_ready"] = True
    report["pair_record"] = {
        "schema": "eie.domain-compatible-pair/v1",
        "tenant_id": tenant,
        "eie": {
            "spec_version": proof["eie_version"],
            "sha256": eie_hash,
            "approval_ref": approvals["eie"]["approval_ref"],
        },
        "ceg": {
            "spec_version": proof["ceg_version"],
            "sha256": ceg_hash,
            "approval_ref": approvals["ceg"]["approval_ref"],
        },
        "compatibility": {
            "rule_set": RULE_SET,
            "verdict": "pass",
            "proof_ref": str(args.output.expanduser().absolute()),
            "rule_set_sha256": report["rule_set_sha256"],
            "ceg_source_revision": report["ceg_source_revision"],
            "ceg_validator_sha256": report["ceg_validator_sha256"],
            "field_mappings": mappings or {},
        },
    }
    return report, 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--eie-spec", type=Path, required=True)
    parser.add_argument("--ceg-spec", type=Path, required=True)
    parser.add_argument("--ceg-repo", type=Path, required=True)
    parser.add_argument("--ceg-python", type=Path, required=True)
    parser.add_argument("--eie-receipt", type=Path)
    parser.add_argument("--ceg-receipt", type=Path)
    parser.add_argument("--field-mappings", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        report, code = check_pair(args)
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        yaml.YAMLError,
        subprocess.TimeoutExpired,
    ) as exc:
        report, code = (
            {
                "schema": "eie.domain-pair-check/v1",
                "release_ready": False,
                "status": "Failed",
                "reason": str(exc),
            },
            1,
        )
    try:
        _write_report(args.output, (json.dumps(report, indent=2, sort_keys=True) + "\n").encode())
    except (OSError, ValueError) as exc:
        report, code = (
            {
                "schema": "eie.domain-pair-check/v1",
                "release_ready": False,
                "status": "Failed",
                "reason": f"Report output refused: {exc}",
            },
            1,
        )
    print(json.dumps(report, indent=2, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
