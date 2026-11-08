"""Offline native EIE authoring/publication; published YAML is the sole authority.

No HTTP routes, database replay, graph policy generation or runtime mutation.
The caller must supply real review evidence. Filesystem publication is intended
for a trusted operator-owned local store, not a writable runtime mount.

L9_META:
  tier: 3
  authority: native-domain-supply-chain-g27r-v3
"""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import re
import stat
import uuid
from collections import defaultdict
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from app.engines.convergence import schema_proposer
from app.engines.domain_yaml_reader import (
    DomainPublicationError,
    is_legal_tenant_id,
    validate_native_spec,
)
from app.engines.native_yaml import parse_native_yaml
from app.services import crm_field_scanner, pg_store

_HASH = re.compile(r"^[0-9a-f]{64}$")
_SEMVER = re.compile(
    r"((?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*))"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
)
_VersionPrecedence = tuple[tuple[tuple[int, str], ...], bool, tuple[tuple[bool, int, str], ...]]
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
_MAX_FILE_BYTES = 2 * 1024 * 1024
_MAX_SIDECAR_BYTES = 256 * 1024


def _semver_precedence(version: str) -> _VersionPrecedence:
    """SemVer order/equality, excluding build metadata; never normalize bytes."""
    match = _SEMVER.fullmatch(version)
    if match is None:
        raise DomainPublicationError("Publication version must be valid SemVer")
    prerelease = match.group(2).split(".") if match.group(2) else []
    if any(part.isdigit() and len(part) > 1 and part[0] == "0" for part in prerelease):
        raise DomainPublicationError("SemVer numeric prerelease cannot have leading zeroes")
    # Length then ASCII digits compares unbounded integers without int() limits.
    core = tuple((len(part), part) for part in match.group(1).split("."))
    identifiers = tuple(
        (not part.isdigit(), len(part) if part.isdigit() else 0, part) for part in prerelease
    )
    return core, not prerelease, identifiers


def _digest(value: object, name: str) -> str:
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise DomainPublicationError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _identity(value: object, name: str) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or value != value.strip()
        or len(value) > 2048
    ):
        raise DomainPublicationError(f"Explicit nonblank {name} is required")
    return value


def _tenant(tenant_id: object) -> str:
    if not isinstance(tenant_id, str) or not is_legal_tenant_id(tenant_id):
        raise DomainPublicationError("Invalid tenant_id")
    return tenant_id


def _complete(raw: object, tenant_id: str) -> dict[str, Any]:
    try:
        validated = validate_native_spec(raw, _tenant(tenant_id))
    except ValueError as exc:
        raise DomainPublicationError(str(exc)) from exc
    metadata = validated.get("metadata", {})
    if "seed" in validated["version"].lower() or (
        isinstance(metadata, dict) and metadata.get("draft_only")
    ):
        raise DomainPublicationError("Seed/draft artifacts cannot become effective native specs")
    if "inference_rules_path" in validated:
        raise DomainPublicationError(
            "Published inference rules must be inline in the approved YAML"
        )
    return validated


def _native_bytes(spec_bytes: bytes, tenant_id: str) -> tuple[dict[str, Any], str]:
    if not isinstance(spec_bytes, bytes) or not spec_bytes or len(spec_bytes) > _MAX_FILE_BYTES:
        raise DomainPublicationError("A bounded complete native YAML byte artifact is required")
    try:
        raw = parse_native_yaml(spec_bytes)
    except (yaml.YAMLError, UnicodeError) as exc:
        raise DomainPublicationError("Invalid native YAML") from exc
    validated = _complete(raw, tenant_id)
    return validated, hashlib.sha256(spec_bytes).hexdigest()


def _batches(values: Sequence[str]) -> list[str]:
    if isinstance(values, (str, bytes)):
        raise DomainPublicationError("approval_batches must be a sequence of batch identifiers")
    result = [_identity(value, "approval batch") for value in values]
    if len(set(result)) != len(result):
        raise DomainPublicationError("Duplicate approval batch identifier")
    return result


def _json_bytes(value: dict[str, Any]) -> bytes:
    content = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    if len(content) > _MAX_SIDECAR_BYTES:
        raise DomainPublicationError("Publication sidecar exceeds reader admission limit")
    return content


def _track_descriptor(tracked: list[int], descriptor: int) -> int:
    """Register a descriptor for cleanup before any later call can skip it."""
    try:
        tracked.append(descriptor)
    except Exception:
        os.close(descriptor)
        raise
    return descriptor


def _open_directory(parent_fd: int, name: str, *, create: bool) -> int:
    if create:
        try:
            os.mkdir(name, mode=0o755, dir_fd=parent_fd)
            os.fsync(parent_fd)
        except FileExistsError:
            # The following no-follow directory open checks the existing entry.
            pass
    return os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)


@contextmanager
def _store(root: str | Path, tenant_id: str, *, create: bool) -> Iterator[tuple[int, int]]:
    """Anchor every access with no-follow descriptors, including root ancestors."""
    tenant = _tenant(tenant_id)
    root_path = Path(root).expanduser()
    if ".." in root_path.parts:
        raise DomainPublicationError("Publication root cannot contain path traversal")
    root_path = root_path.absolute()
    descriptors: list[int] = []
    try:
        opened = os.open(root_path.anchor, _DIRECTORY_FLAGS)
        current_fd = _track_descriptor(descriptors, opened)
        for component in root_path.parts[1:]:
            opened = _open_directory(current_fd, component, create=create)
            current_fd = _track_descriptor(descriptors, opened)
        opened = _open_directory(current_fd, tenant, create=create)
        tenant_fd = _track_descriptor(descriptors, opened)
        opened = _open_directory(tenant_fd, "revisions", create=create)
        revisions_fd = _track_descriptor(descriptors, opened)
        opened = os.open(
            ".publication.lock",
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
            0o600,
            dir_fd=tenant_fd,
        )
        lock_fd = _track_descriptor(descriptors, opened)
        if not stat.S_ISREG(os.fstat(lock_fd).st_mode) or os.fstat(lock_fd).st_nlink != 1:
            raise DomainPublicationError("Publication lock must be a private regular file")
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        yield tenant_fd, revisions_fd
    except OSError as exc:
        raise DomainPublicationError("Unsafe or inaccessible publication store") from exc
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _read_bytes(parent_fd: int, name: str) -> bytes:
    file_fd = os.open(name, _FILE_FLAGS, dir_fd=parent_fd)
    try:
        info = os.fstat(file_fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise DomainPublicationError("Published entries must be private regular files")
        if info.st_size > _MAX_FILE_BYTES:
            raise DomainPublicationError("Published entry exceeds size limit")
        chunks: list[bytes] = []
        size = 0
        while chunk := os.read(file_fd, 65536):
            size += len(chunk)
            if size > _MAX_FILE_BYTES:
                raise DomainPublicationError("Published entry exceeds size limit")
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(file_fd)


def _read_json(parent_fd: int, name: str) -> dict[str, Any]:
    def unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise DomainPublicationError("Duplicate publication sidecar key")
            result[key] = value
        return result

    try:
        content = _read_bytes(parent_fd, name)
        if len(content) > _MAX_SIDECAR_BYTES:
            raise DomainPublicationError("Publication sidecar exceeds reader admission limit")
        value = json.loads(content, object_pairs_hook=unique_keys)
    except (ValueError, UnicodeError) as exc:
        raise DomainPublicationError("Malformed publication JSON") from exc
    if not isinstance(value, dict):
        raise DomainPublicationError("Publication JSON must be an object")
    return value


def _revision(revisions_fd: int, tenant_id: str, sha256: str) -> tuple[bytes, dict[str, Any]]:
    revision_fd = os.open(_digest(sha256, "revision hash"), _DIRECTORY_FLAGS, dir_fd=revisions_fd)
    try:
        if set(os.listdir(revision_fd)) != {"spec.yaml", "receipt.json"}:
            raise DomainPublicationError("Revision must contain exactly spec.yaml and receipt.json")
        spec_bytes = _read_bytes(revision_fd, "spec.yaml")
        raw, actual_hash = _native_bytes(spec_bytes, tenant_id)
        receipt = _read_json(revision_fd, "receipt.json")
        if actual_hash != sha256:
            raise DomainPublicationError("Revision bytes do not match their digest")
        if (
            receipt.get("schema") != "eie.domain-approval/v1"
            or receipt.get("tenant_id") != tenant_id
            or receipt.get("version") != raw["version"]
            or receipt.get("sha256") != sha256
            or receipt.get("reviewed_input_sha256") != sha256
        ):
            raise DomainPublicationError("Receipt does not bind this reviewed native revision")
        _identity(receipt.get("reviewed_by"), "reviewed_by")
        _identity(receipt.get("approval_ref"), "approval_ref")
        reviewed_at = _identity(receipt.get("reviewed_at"), "reviewed_at")
        try:
            timestamp = datetime.fromisoformat(reviewed_at)
        except ValueError as exc:
            raise DomainPublicationError("Invalid review timestamp") from exc
        if timestamp.tzinfo is None or timestamp > datetime.now(UTC):
            raise DomainPublicationError("Review timestamp must be timezone-aware and not future")
        previous = receipt.get("previous_sha256")
        if "previous_sha256" not in receipt or previous == sha256:
            raise DomainPublicationError(
                "Receipt must identify a distinct previous revision or null"
            )
        if previous is not None:
            _digest(previous, "previous_sha256")
        if "approval_batches" not in receipt or not isinstance(receipt["approval_batches"], list):
            raise DomainPublicationError("Receipt must declare approval_batches")
        _batches(receipt["approval_batches"])
        return spec_bytes, receipt
    finally:
        os.close(revision_fd)


def _current(tenant_fd: int, revisions_fd: int, tenant_id: str) -> str | None:
    try:
        pointer = _read_json(tenant_fd, "current.json")
    except FileNotFoundError:
        return None
    if set(pointer) != {"schema", "sha256"} or pointer.get("schema") != "eie.domain-current/v1":
        raise DomainPublicationError("Invalid effective-revision pointer")
    sha256 = _digest(pointer.get("sha256"), "current hash")
    _revision(revisions_fd, tenant_id, sha256)
    return sha256


def _retained_versions(revisions_fd: int, tenant_id: str) -> dict[str, _VersionPrecedence]:
    """Verify all retained tenant revisions while the publication lock is held."""
    versions: dict[str, _VersionPrecedence] = {}
    bindings: dict[_VersionPrecedence, str] = {}
    predecessors: set[str] = set()
    for sha256 in sorted(os.listdir(revisions_fd)):
        _, receipt = _revision(revisions_fd, tenant_id, sha256)
        precedence = _semver_precedence(receipt["version"])
        if precedence in bindings:
            raise DomainPublicationError(
                "Retained history has conflicting exact bytes for the same SemVer precedence"
            )
        bindings[precedence] = sha256
        versions[sha256] = precedence
        if receipt["previous_sha256"] is not None:
            predecessors.add(_digest(receipt["previous_sha256"], "previous_sha256"))
    if predecessors - versions.keys():
        raise DomainPublicationError("Retained history references an unknown previous revision")
    return versions


def _write_file(parent_fd: int, name: str, value: bytes, mode: int = 0o444) -> None:
    file_fd = os.open(
        name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode, dir_fd=parent_fd
    )
    try:
        with os.fdopen(file_fd, "wb", closefd=False) as stream:
            stream.write(value)
            stream.flush()
            os.fsync(file_fd)
    finally:
        os.close(file_fd)


def _install_revision(
    revisions_fd: int, sha256: str, spec_bytes: bytes, receipt: dict[str, Any]
) -> None:
    staging = f".staging-{uuid.uuid4().hex}"
    os.mkdir(staging, mode=0o700, dir_fd=revisions_fd)
    stage_fd = os.open(staging, _DIRECTORY_FLAGS, dir_fd=revisions_fd)
    installed = False
    try:
        _write_file(stage_fd, "spec.yaml", spec_bytes)
        _write_file(stage_fd, "receipt.json", _json_bytes(receipt))
        os.fsync(stage_fd)
        os.fchmod(stage_fd, 0o555)
        os.rename(staging, sha256, src_dir_fd=revisions_fd, dst_dir_fd=revisions_fd)
        installed = True
        os.fsync(revisions_fd)
    finally:
        if not installed:
            os.fchmod(stage_fd, stat.S_IRWXU)
            for name in ("spec.yaml", "receipt.json"):
                try:
                    os.unlink(name, dir_fd=stage_fd)
                except FileNotFoundError:
                    continue
            os.rmdir(staging, dir_fd=revisions_fd)
        os.close(stage_fd)


def _replace_pointer(tenant_fd: int, sha256: str) -> None:
    temp_name = f".current-{uuid.uuid4().hex}.json"
    try:
        _write_file(
            tenant_fd, temp_name, _json_bytes({"schema": "eie.domain-current/v1", "sha256": sha256})
        )
        os.replace(temp_name, "current.json", src_dir_fd=tenant_fd, dst_dir_fd=tenant_fd)
        os.fsync(tenant_fd)
    finally:
        try:
            os.unlink(temp_name, dir_fd=tenant_fd)
        except FileNotFoundError:
            # Renamed into current.json, or never created. Nothing left to unlink.
            pass


def publish_effective_spec(
    root: str | Path,
    tenant_id: str,
    spec_bytes: bytes,
    *,
    reviewed_input_sha256: str,
    reviewed_by: str,
    approval_ref: str,
    approval_batches: Sequence[str] = (),
    expected_previous_sha256: str | None = None,
) -> dict[str, Any]:
    """Validate exact reviewed bytes, install immutable revision, then CAS pointer.

    ``None`` expects no prior current revision. Matching duplicate publication
    is idempotent and retains its original receipt. Conflicting review evidence
    cannot overwrite an existing receipt. Old revisions are retained forever.
    Ordinary publication cannot lower current SemVer precedence or rebind any
    retained precedence to different bytes, including build-metadata changes.
    """
    raw, sha256 = _native_bytes(spec_bytes, tenant_id)
    if _digest(reviewed_input_sha256, "reviewed_input_sha256") != sha256:
        raise DomainPublicationError("Reviewed input digest does not match exact publication bytes")
    reviewer = _identity(reviewed_by, "reviewed_by")
    reference = _identity(approval_ref, "approval_ref")
    batches = _batches(approval_batches)
    if expected_previous_sha256 is not None:
        _digest(expected_previous_sha256, "expected_previous_sha256")
    with _store(root, tenant_id, create=True) as (tenant_fd, revisions_fd):
        previous = _current(tenant_fd, revisions_fd, tenant_id)
        precedence = _semver_precedence(raw["version"])
        versions = _retained_versions(revisions_fd, tenant_id)
        receipt: dict[str, Any] | None = None
        try:
            old_bytes, existing_receipt = _revision(revisions_fd, tenant_id, sha256)
        except FileNotFoundError:
            # A revision directory missing one file is corruption, not absence.
            try:
                os.stat(sha256, dir_fd=revisions_fd, follow_symlinks=False)
            except FileNotFoundError:
                receipt = None
            else:
                raise DomainPublicationError("Incomplete existing revision") from None
        else:
            if (
                old_bytes != spec_bytes
                or existing_receipt["reviewed_by"] != reviewer
                or existing_receipt["approval_ref"] != reference
                or existing_receipt["approval_batches"] != batches
            ):
                raise DomainPublicationError("Immutable revision has conflicting review evidence")
            if previous == sha256:
                return existing_receipt
            receipt = existing_receipt
        if previous != expected_previous_sha256:
            raise DomainPublicationError("Compare-and-swap failed: current revision changed")
        if any(key == precedence and digest != sha256 for digest, key in versions.items()):
            raise DomainPublicationError("Same SemVer precedence is bound to different exact bytes")
        if previous is not None and precedence < versions[previous]:
            raise DomainPublicationError("SemVer downgrade requires explicit approved rollback")
        if receipt is None:
            receipt = {
                "schema": "eie.domain-approval/v1",
                "tenant_id": tenant_id,
                "version": raw["version"],
                "sha256": sha256,
                "reviewed_input_sha256": reviewed_input_sha256,
                "reviewed_by": reviewer,
                "approval_ref": reference,
                "reviewed_at": datetime.now(UTC).isoformat(),
                "previous_sha256": previous,
                "approval_batches": batches,
            }
            _install_revision(revisions_fd, sha256, spec_bytes, receipt)
        _replace_pointer(tenant_fd, sha256)
        return receipt


def rollback_effective_spec(
    root: str | Path,
    tenant_id: str,
    sha256: str,
    *,
    expected_previous_sha256: str,
) -> dict[str, Any]:
    """Restore only an existing approved revision, keeping its original review."""
    target = _digest(sha256, "rollback hash")
    expected = _digest(expected_previous_sha256, "expected_previous_sha256")
    with _store(root, tenant_id, create=False) as (tenant_fd, revisions_fd):
        _, receipt = _revision(revisions_fd, tenant_id, target)
        previous = _current(tenant_fd, revisions_fd, tenant_id)
        if previous != expected:
            raise DomainPublicationError("Compare-and-swap failed: current revision changed")
        _replace_pointer(tenant_fd, target)
        return receipt


def _decisions(
    proposal_set: schema_proposer.SchemaProposalSet,
    approval_decisions: list[schema_proposer.ApprovalDecision],
) -> dict[tuple[str, str], bool]:
    try:
        return schema_proposer.validate_ownership(proposal_set, approval_decisions)
    except ValueError as exc:
        raise DomainPublicationError(str(exc)) from exc


def materialize_approved_spec(
    base_spec: dict[str, Any],
    proposal_set: schema_proposer.SchemaProposalSet,
    approval_decisions: list[schema_proposer.ApprovalDecision],
    *,
    tenant_id: str,
    node_label: str | None = None,
    allow_overwrite: bool = False,
    source_digest: str | None = None,
    source_sha256: str | None = None,
) -> bytes:
    """Produce deterministic complete candidate bytes for review; do not publish."""
    _complete(base_spec, tenant_id)
    if proposal_set.tenant_id != tenant_id or proposal_set.domain != tenant_id:
        raise DomainPublicationError("Owner-bound proposal tenant/domain must equal tenant_id")
    _decisions(proposal_set, approval_decisions)
    try:
        updated = schema_proposer.apply(
            base_spec,
            approval_decisions,
            proposal_set,
            node_label=node_label,
            allow_overwrite=allow_overwrite,
            source_digest=source_digest,
            source_sha256=source_sha256,
        )
    except ValueError as exc:
        raise DomainPublicationError(str(exc)) from exc
    _complete(updated, tenant_id)
    serialized: str = yaml.safe_dump(updated, sort_keys=False, allow_unicode=True)
    return serialized.encode("utf-8")


async def approve_and_publish(
    root: str | Path,
    tenant_id: str,
    base_spec: dict[str, Any],
    proposal_set: schema_proposer.SchemaProposalSet,
    approval_decisions: list[schema_proposer.ApprovalDecision],
    *,
    node_label: str | None = None,
    batch_run_id: str,
    reviewed_input_sha256: str,
    reviewed_by: str,
    approval_ref: str,
    expected_previous_sha256: str | None = None,
    allow_overwrite: bool = False,
    source_digest: str | None = None,
    source_sha256: str | None = None,
) -> dict[str, Any] | None:
    """Persist explicit human decisions transactionally before publishing bytes.

    Database rows are audit only. Publication failure after audit commit leaves
    the prior pointer unchanged; retry exact reviewed bytes with the same batch.
    Rejections are audited but never applied, and undecided fields are absent.
    """
    reviewer = _identity(reviewed_by, "reviewed_by")
    _identity(approval_ref, "approval_ref")
    batch = _identity(batch_run_id, "batch_run_id")
    candidate = materialize_approved_spec(
        base_spec,
        proposal_set,
        approval_decisions,
        tenant_id=tenant_id,
        node_label=node_label,
        allow_overwrite=allow_overwrite,
        source_digest=source_digest,
        source_sha256=source_sha256,
    )
    if (
        _digest(reviewed_input_sha256, "reviewed_input_sha256")
        != hashlib.sha256(candidate).hexdigest()
    ):
        raise DomainPublicationError("Reviewed input digest does not match materialized candidate")
    if expected_previous_sha256 is not None:
        _digest(expected_previous_sha256, "expected_previous_sha256")
    decided = _decisions(proposal_set, approval_decisions)
    rows = [
        {
            **fp.model_dump(),
            "proposed_gate": None,
            "proposed_scoring_dimension": None,
            "yaml_diff": proposal_set.yaml_diff,
            "approved": decided[
                (schema_proposer._validate_node_label(fp.node_label), fp.field_name)
            ],
        }
        for fp in proposal_set.proposed_fields
        if (fp.node_label, fp.field_name) in decided
    ]
    if rows:
        records = await pg_store.persist_schema_proposal_batch(
            tenant_id=tenant_id,
            domain=tenant_id,
            batch_run_id=batch,
            reviewed_by=reviewer,
            decisions=rows,
        )
        if len(records) != len(rows):
            raise DomainPublicationError(
                "Approval audit did not persist the complete decision batch"
            )
    if not schema_proposer.applied_field_keys(approval_decisions, proposal_set):
        return None
    return publish_effective_spec(
        root,
        tenant_id,
        candidate,
        reviewed_input_sha256=reviewed_input_sha256,
        reviewed_by=reviewer,
        approval_ref=approval_ref,
        approval_batches=(batch,),
        expected_previous_sha256=expected_previous_sha256,
    )


def scan_to_seed(
    crm_metadata: list[dict[str, Any]] | dict[str, Any],
    template_bytes: bytes,
    *,
    tenant_id: str,
    source_identifier: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Reuse CRM scanner/seed generator for a redacted offline partial draft.

    Ambiguous normalized source/domain names are withheld rather than silently
    resolved by the scanner's lookup. Record values/descriptions are never copied
    into the report or seed. Completion and exact-byte review are separate steps.
    """
    tenant = _tenant(tenant_id)
    source = _identity(source_identifier, "source_identifier")
    try:
        raw_template = parse_native_yaml(template_bytes)
    except (yaml.YAMLError, UnicodeError) as exc:
        raise DomainPublicationError("Invalid template YAML") from exc
    template_id = raw_template.get("name") if isinstance(raw_template, dict) else None
    template, template_hash = _native_bytes(template_bytes, _tenant(template_id))
    entries = crm_metadata.get("fields") if isinstance(crm_metadata, dict) else crm_metadata
    if not isinstance(entries, list):
        raise DomainPublicationError("CRM metadata must be a field list or an object with fields")
    fields: list[crm_field_scanner.CRMField] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise DomainPublicationError("CRM field metadata must be an object")
        name = _identity(entry.get("name"), "CRM field name")
        field_type = _identity(
            entry.get("field_type", entry.get("type", "string")), "CRM field type"
        )
        resource = entry.get("source_resource")
        if resource is not None:
            _identity(resource, "source_resource")
        fields.append(
            crm_field_scanner.CRMField(
                name=name,
                field_type=field_type,
                source_system=source,
                source_resource=resource,
            )
        )
    source_groups: dict[str, list[str]] = defaultdict(list)
    domain_groups: dict[str, list[str]] = defaultdict(list)
    for crm_field in fields:
        source_groups[crm_field_scanner._normalize(crm_field.name)].append(crm_field.name)
    for prop in crm_field_scanner._extract_domain_properties(template):
        domain_groups[crm_field_scanner._normalize(prop.name)].append(prop.name)
    ambiguous_keys = {
        key
        for groups in (source_groups, domain_groups)
        for key, names in groups.items()
        if len(names) > 1
    }
    scan_template = copy.deepcopy(template)
    for node in scan_template["ontology"]["nodes"]:
        node["properties"] = [
            prop
            for prop in node["properties"]
            if crm_field_scanner._normalize(prop["name"]) not in ambiguous_keys
        ]
    unambiguous = [
        field for field in fields if crm_field_scanner._normalize(field.name) not in ambiguous_keys
    ]
    scan = crm_field_scanner.scan_crm_fields(
        unambiguous,
        scan_template,
        domain_id=template_id,
        domain_version=template["version"],
    )
    seed = crm_field_scanner.generate_seed_yaml(scan, scan_template, tenant, source_system=source)
    # Templates may contain private free text. Preserve schema names/types only.
    for prop in seed["ontology"]["nodes"][0]["properties"]:
        prop.pop("description", None)
    report = {
        "schema": "eie.domain-scan/v1",
        "tenant_id": tenant,
        "source_identifier": source,
        "template_sha256": template_hash,
        "template_version": template["version"],
        "scanned_at": datetime.now(UTC).isoformat(),
        "draft_only": True,
        "evidence_redacted": True,
        "total_crm_fields": len(fields),
        **{
            key: value
            for key, value in crm_field_scanner.scan_result_to_dict(scan).items()
            if key in {"matched", "unmapped", "missing", "coverage_ratio", "scan_hash"}
        },
        "ambiguous": [
            {
                "normalized_field": key,
                "crm_fields": source_groups.get(key, []),
                "domain_properties": domain_groups.get(key, []),
            }
            for key in sorted(ambiguous_keys)
        ],
    }
    seed["metadata"].update({"template_sha256": template_hash, "draft_only": True})
    return seed, report


async def materialize_audited_spec(
    base_spec: dict[str, Any],
    *,
    tenant_id: str,
    domain: str,
    node_label: str | None = None,
    batch_run_ids: Sequence[str] = (),
    allow_overwrite: bool = False,
    discovery_specs: Mapping[str, bytes] | None = None,
    applied_field_keys: list[tuple[str, str]] | None = None,
) -> tuple[bytes, list[str]]:
    """Verify original discovery artifacts, then rebase recorded owners only.

    This explicit offline bridge never activates or re-audits. Every selected
    approval must be owner-bound, including same-type no-ops. The optional label
    asserts all selected owners; it never selects a subset or fills legacy NULLs.
    Exact discovery bytes must verify before a newer complete base can be used.
    Full rebased candidate bytes require separate exact-byte review/publication.
    """
    tenant = _tenant(tenant_id)
    if domain != tenant:
        raise DomainPublicationError("Explicit domain must equal tenant_id")
    selected = set(_batches(batch_run_ids))
    revised = _complete(base_spec, tenant)
    if node_label is not None:
        try:
            schema_proposer._validate_node_label(node_label)
            schema_proposer._target_node_def(revised["ontology"]["nodes"], node_label)
        except ValueError as exc:
            raise DomainPublicationError(str(exc)) from exc
    records = await pg_store.get_approved_schema_proposals(tenant_id=tenant, domain=domain)
    groups: dict[str, list[schema_proposer.FieldProposal]] = {}
    batch_digests: dict[str, str] = {}
    identities: set[tuple[str, str, str]] = set()
    verified_discovery: dict[str, dict[str, Any]] = {}
    # Whole selected input preflight precedes all candidate construction.
    for record in records:
        if record.tenant_id != tenant or record.domain != domain:
            raise DomainPublicationError("Approval audit row has cross-tenant identity")
        if record.approval_status != "approved":
            continue
        batch = _identity(record.batch_run_id, "audit batch_run_id")
        if selected and batch not in selected:
            continue
        _identity(record.reviewed_by, "audit reviewed_by")
        if not isinstance(record.reviewed_at, datetime) or record.reviewed_at.tzinfo is None:
            raise DomainPublicationError(
                "Approved audit row lacks a timezone-aware review timestamp"
            )
        if record.reviewed_at > datetime.now(UTC):
            raise DomainPublicationError("Approved audit row has a future review timestamp")
        if getattr(record, "ownership_contract_version", None) != 1:
            raise DomainPublicationError(
                "Approved audit row is not owner-bound (contract version 1)"
            )
        try:
            owner = schema_proposer._validate_node_label(getattr(record, "node_label", None))
            digest = schema_proposer._validate_discovery_digest(
                getattr(record, "discovery_spec_sha256", None)
            )
        except ValueError as exc:
            raise DomainPublicationError(f"Approved audit row is not owner-bound: {exc}") from exc
        if node_label is not None and owner != node_label:
            raise DomainPublicationError(
                "node_label assertion conflicts with selected recorded owner"
            )
        field_name = record.field_name
        if not isinstance(field_name, str) or not schema_proposer._is_canonical_field_name(
            field_name
        ):
            raise DomainPublicationError("Approved audit row has a noncanonical field name")
        key = (batch, owner, field_name)
        if key in identities:
            raise DomainPublicationError("Duplicate approved owner/field pair in audit batch")
        identities.add(key)
        if batch in batch_digests and batch_digests[batch] != digest:
            raise DomainPublicationError("Audit batch has conflicting discovery spec digests")
        batch_digests[batch] = digest
        if digest not in verified_discovery:
            evidence = discovery_specs.get(digest) if discovery_specs is not None else None
            if not isinstance(evidence, bytes) or hashlib.sha256(evidence).hexdigest() != digest:
                raise DomainPublicationError("Missing or changed exact discovery spec artifact")
            try:
                discovery, actual_digest = _native_bytes(evidence, tenant)
            except ValueError as exc:
                raise DomainPublicationError(f"Invalid discovery spec artifact: {exc}") from exc
            if actual_digest != digest:
                raise DomainPublicationError("Exact discovery spec artifact digest mismatch")
            verified_discovery[digest] = discovery
        try:
            schema_proposer._target_node_def(verified_discovery[digest]["ontology"]["nodes"], owner)
            schema_proposer._target_node_def(revised["ontology"]["nodes"], owner)
        except ValueError as exc:
            raise DomainPublicationError(str(exc)) from exc
        groups.setdefault(batch, []).append(
            schema_proposer.FieldProposal(
                node_label=owner,
                field_name=field_name,
                field_type=record.field_type or "string",
                source=record.source or "enrichment",
                fill_rate=record.fill_rate or 0.0,
                avg_confidence=record.avg_confidence or 0.0,
                discovery_spec_sha256=digest,
                ownership_contract_version=1,
            )
        )
    if selected - groups.keys():
        raise DomainPublicationError("Requested batch has no approved audit records")
    applied_batches: list[str] = []
    applied_keys: list[tuple[str, str]] = []
    for batch, batch_fields in groups.items():
        fields: list[schema_proposer.FieldProposal] = []
        for fp in batch_fields:
            target = schema_proposer._target_node_def(revised["ontology"]["nodes"], fp.node_label)
            existing = {prop["name"]: prop for prop in target["properties"]}
            if fp.field_name in existing:
                if existing[fp.field_name]["type"] == fp.field_type:
                    continue
                if not allow_overwrite:
                    raise DomainPublicationError(
                        f"Approved field {(fp.node_label, fp.field_name)!r} already exists with a different type"
                    )
            fields.append(fp)
        if not fields:
            continue
        revision = schema_proposer.revision_from_approved(
            revised,
            fields=fields,
            gates=[],
            dims=[],
            domain=tenant,
            tenant_id=tenant,
            discovery_spec_sha256=batch_digests[batch],
            ownership_contract_version=1,
        )
        decisions = [
            schema_proposer.ApprovalDecision(
                node_label=field.node_label, field_name=field.field_name, approved=True
            )
            for field in fields
        ]
        # Original bytes/owners were verified above. This intentionally rebases
        # into a new candidate rather than claiming the current base is discovery.
        batch_keys = schema_proposer.applied_field_keys(decisions, revision)
        for fp in fields:
            schema_proposer._apply_node_properties(
                revised["ontology"]["nodes"],
                {fp.field_name},
                [fp],
                node_label=fp.node_label,
                allow_overwrite=allow_overwrite,
            )
        revised["version"] = revision.version_bump
        _complete(revised, tenant)
        applied_batches.append(batch)
        applied_keys.extend(batch_keys)
    candidate = yaml.safe_dump(revised, sort_keys=False, allow_unicode=True).encode("utf-8")
    # Validate the actual generated bytes, not only the pre-serialization dict.
    # Preserve parser diagnostics and do not expose applied keys on refusal.
    _complete(parse_native_yaml(candidate), tenant)
    if applied_field_keys is not None:
        applied_field_keys.extend(applied_keys)
    return candidate, applied_batches
