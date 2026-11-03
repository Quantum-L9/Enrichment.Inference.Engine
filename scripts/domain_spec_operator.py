"""Trusted operator-only EIE native workflow; no HTTP or automatic promotion.

Run with ``python -m scripts.domain_spec_operator --help`` from the repository.
L9_META:
  tier: 3
  authority: native-domain-supply-chain-g27r-v3
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import yaml

from app.engines.domain_yaml_reader import DomainPublicationError
from app.services import domain_spec_workflow as workflow


@contextmanager
def _parent(path: str, *, create: bool = False) -> Iterator[tuple[int, str]]:
    """Read/write local operator artifacts without following any symlink."""
    file_path = Path(path).expanduser()
    if ".." in file_path.parts:
        raise DomainPublicationError("Artifact paths cannot contain traversal")
    file_path = file_path.absolute()
    descriptors: list[int] = []
    try:
        current_fd = os.open(file_path.anchor, workflow._DIRECTORY_FLAGS)
        descriptors.append(current_fd)
        for part in file_path.parts[1:-1]:
            current_fd = workflow._open_directory(current_fd, part, create=create)
            descriptors.append(current_fd)
        yield current_fd, file_path.name
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _read(path: str) -> bytes:
    with _parent(path) as (parent_fd, name):
        return workflow._read_bytes(parent_fd, name)


def _write(path: str, content: bytes) -> None:
    # New files only: never overwrite an operator's reviewed artifact.
    with _parent(path, create=True) as (parent_fd, name):
        workflow._write_file(parent_fd, name, content, mode=0o600)
        os.fsync(parent_fd)


def _native(path: str, tenant: str) -> dict[str, Any]:
    raw, _ = workflow._native_bytes(_read(path), tenant)
    return raw


def _review_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--reviewed-input-sha256", required=True, help="Digest of exact final reviewed YAML bytes"
    )
    parser.add_argument(
        "--reviewed-by", required=True, help="Explicit real named reviewer (not inferred)"
    )
    parser.add_argument("--approval-ref", required=True, help="Explicit review evidence reference")
    parser.add_argument(
        "--expected-previous-sha256", help="CAS prior hash; omit only for first publication"
    )


def build_parser() -> argparse.ArgumentParser:
    """Build the offline CLI parser; no production service endpoints are added."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    scan = commands.add_parser(
        "scan-to-seed", help="CRM metadata to redacted report and PARTIAL seed draft"
    )
    scan.add_argument("--crm-metadata", required=True)
    scan.add_argument("--template", required=True, help="Complete native EIE template, not a seed")
    scan.add_argument("--tenant", required=True)
    scan.add_argument(
        "--source", required=True, help="Technical source identifier; never a credential or URL"
    )
    scan.add_argument("--seed-output", required=True)
    scan.add_argument("--report-output", required=True)

    materialize = commands.add_parser(
        "materialize-approved",
        help="Explicit approved audit rows to full candidate; NEVER publishes",
    )
    materialize.add_argument(
        "--base-spec", required=True, help="Complete native EIE base YAML, not partial seed"
    )
    materialize.add_argument("--tenant", required=True)
    materialize.add_argument("--domain", required=True, help="Explicit domain; must equal tenant")
    materialize.add_argument(
        "--node-label", help="Optional equality assertion over ALL selected recorded owners"
    )
    materialize.add_argument(
        "--discovery-spec",
        action="append",
        required=True,
        help="Exact original complete discovery YAML artifact; repeat for distinct digests",
    )
    materialize.add_argument(
        "--batch-run-id",
        action="append",
        default=[],
        help="Limit to selected approved audit batch(es)",
    )
    materialize.add_argument(
        "--allow-overwrite",
        action="store_true",
        help="Explicitly permit changing an existing field's type/source",
    )
    materialize.add_argument("--output", required=True)

    publish = commands.add_parser(
        "publish", help="Publish separately reviewed complete native byte artifact"
    )
    publish.add_argument(
        "--root", required=True, help="Operator-owned local publication store (not runtime mount)"
    )
    publish.add_argument("--tenant", required=True)
    publish.add_argument("--spec", required=True)
    publish.add_argument("--approval-batch", action="append", default=[])
    _review_options(publish)

    rollback = commands.add_parser(
        "rollback", help="CAS restore existing approved bytes and original receipt"
    )
    rollback.add_argument("--root", required=True)
    rollback.add_argument("--tenant", required=True)
    rollback.add_argument("--sha256", required=True)
    rollback.add_argument("--expected-previous-sha256", required=True)
    return parser


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    if args.command == "scan-to-seed":
        metadata = json.loads(_read(args.crm_metadata))
        seed, report = workflow.scan_to_seed(
            metadata,
            _read(args.template),
            tenant_id=args.tenant,
            source_identifier=args.source,
        )
        if Path(args.seed_output).absolute() == Path(args.report_output).absolute():
            raise DomainPublicationError("Seed and report output paths must be distinct")
        _write(args.seed_output, yaml.safe_dump(seed, sort_keys=False, allow_unicode=True).encode())
        _write(args.report_output, (json.dumps(report, indent=2, sort_keys=True) + "\n").encode())
        return {
            "status": "draft_only",
            "seed_output": args.seed_output,
            "report_output": args.report_output,
        }
    if args.command == "materialize-approved":
        discovery_specs: dict[str, bytes] = {}
        for path in args.discovery_spec:
            exact_bytes = _read(path)
            _, digest = workflow._native_bytes(exact_bytes, args.tenant)
            discovery_specs[digest] = exact_bytes
        keys: list[tuple[str, str]] = []
        candidate, batches = await workflow.materialize_audited_spec(
            _native(args.base_spec, args.tenant),
            tenant_id=args.tenant,
            domain=args.domain,
            node_label=args.node_label,
            batch_run_ids=args.batch_run_id,
            allow_overwrite=args.allow_overwrite,
            discovery_specs=discovery_specs,
            applied_field_keys=keys,
        )
        _write(args.output, candidate)
        return {
            "status": "candidate_only_requires_exact_byte_review",
            "output": args.output,
            "sha256": hashlib.sha256(candidate).hexdigest(),
            "applied_approval_batches": batches,
            "applied_field_keys": keys,
        }
    if args.command == "publish":
        receipt = workflow.publish_effective_spec(
            args.root,
            args.tenant,
            _read(args.spec),
            reviewed_input_sha256=args.reviewed_input_sha256,
            reviewed_by=args.reviewed_by,
            approval_ref=args.approval_ref,
            approval_batches=args.approval_batch,
            expected_previous_sha256=args.expected_previous_sha256,
        )
        return {"status": "published_local_store_only", "receipt": receipt}
    receipt = workflow.rollback_effective_spec(
        args.root,
        args.tenant,
        args.sha256,
        expected_previous_sha256=args.expected_previous_sha256,
    )
    return {"status": "restored_original_approved_revision", "receipt": receipt}


def main(argv: list[str] | None = None) -> int:
    """Execute one explicitly requested offline operation, returning exit status."""
    args = build_parser().parse_args(argv)
    try:
        result = asyncio.run(_run(args))
    except Exception as exc:
        # Avoid echoing CRM data, DB strings, credentials or response bodies.
        # Validation diagnostics are operator-safe; service failures are redacted.
        if isinstance(exc, (ValueError, OSError)):
            message = str(exc) if isinstance(exc, DomainPublicationError) else type(exc).__name__
        else:
            message = f"{type(exc).__name__}: operation failed; no automatic publication/retry"
        sys.stderr.write(json.dumps({"status": "refused", "error": message}) + "\n")
        return 2
    sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
