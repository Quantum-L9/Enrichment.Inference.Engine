"""Offline pair artifact security; no remote validation or production writes.

L9_META:
  tier: 2
  domain: enrichment
  authority: native-hosted-correction-g27r-20261104
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import stat
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml

from app.engines.domain_yaml_reader import DomainPublicationError
from app.engines.native_yaml import parse_native_yaml
from app.services import domain_spec_workflow as workflow
from scripts import check_domain_compatibility as pair

ROOT = Path(__file__).resolve().parents[2]
GOOD = (ROOT / "domains/plasticos/spec.yaml").read_bytes()


def _args(tmp_path: Path) -> argparse.Namespace:
    eie, ceg = tmp_path / "eie.yaml", tmp_path / "ceg.yaml"
    eie.write_bytes(GOOD)
    ceg.write_bytes(b"domain: {id: plasticos, version: 1.0.0}\n")
    repo = tmp_path / "trusted-ceg"
    (repo / "tools").mkdir(parents=True)
    (repo / "tools/validate_domain.py").write_bytes(b"# disposable native CLI witness\n")
    return argparse.Namespace(
        eie_spec=eie,
        ceg_spec=ceg,
        eie_receipt=None,
        ceg_receipt=None,
        field_mappings=None,
        ceg_repo=repo,
        ceg_python=Path(sys.executable),
        output=tmp_path / "report.json",
    )


def _main(args: argparse.Namespace, monkeypatch: pytest.MonkeyPatch) -> int:
    argv = ["check_domain_compatibility.py"]
    for key, value in vars(args).items():
        if value is not None:
            argv.extend(["--" + key.replace("_", "-"), str(value)])
    monkeypatch.setattr(sys, "argv", argv)
    return pair.main()


@pytest.mark.parametrize("failure", [False, True])
@pytest.mark.parametrize("existing", [False, True])
def test_main_writes_legitimate_and_failed_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys, failure: bool, existing: bool
) -> None:
    args = _args(tmp_path)
    if existing:
        args.output.write_bytes(b"previous valid report\n")
    report = {"schema": "eie.domain-pair-check/v1", "release_ready": False, "status": "Passed"}

    def check(_args):
        if failure:
            raise ValueError("test-only native refusal")
        return report, 2

    monkeypatch.setattr(pair, "check_pair", check)
    assert _main(args, monkeypatch) == (1 if failure else 2)
    saved = json.loads(args.output.read_bytes())
    assert saved == json.loads(capsys.readouterr().out)
    assert saved["release_ready"] is False
    assert saved["status"] == ("Failed" if failure else "Passed")
    if failure:
        assert saved["reason"] == "test-only native refusal"
    assert list(tmp_path.glob(".pair-report-*")) == []


def test_main_refuses_output_symlink_without_overwriting_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    args = _args(tmp_path)
    target = tmp_path / "unrelated.txt"
    original = b"unrelated operator evidence must stay exact\n"
    target.write_bytes(original)
    args.output.symlink_to(target)
    args.eie_spec = tmp_path / "missing-eie.yaml"
    assert _main(args, monkeypatch) == 1
    assert target.read_bytes() == original
    assert args.output.is_symlink()
    assert json.loads(capsys.readouterr().out)["release_ready"] is False


@pytest.mark.parametrize("kind", ["symlink-parent", "hardlink", "directory", "fifo", "traversal"])
def test_main_refuses_unsafe_output_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys, kind: str
) -> None:
    args = _args(tmp_path)
    target = tmp_path / "unrelated.txt"
    target.write_bytes(b"unchanged")
    if kind == "symlink-parent":
        link = tmp_path / "linked-directory"
        link.symlink_to(tmp_path, target_is_directory=True)
        args.output = link / "unrelated.txt"
    elif kind == "hardlink":
        os.link(target, args.output)
    elif kind == "directory":
        args.output.mkdir()
    elif kind == "fifo":
        os.mkfifo(args.output)
    else:
        args.output = tmp_path / "trusted-ceg" / ".." / "unrelated.txt"
    monkeypatch.setattr(pair, "check_pair", lambda _args: ({"release_ready": False}, 2))
    assert _main(args, monkeypatch) == 1
    assert target.read_bytes() == b"unchanged"
    assert json.loads(capsys.readouterr().out)["release_ready"] is False


@pytest.mark.parametrize(
    "field", ["eie_spec", "ceg_spec", "eie_receipt", "ceg_receipt", "field_mappings"]
)
@pytest.mark.parametrize(
    "kind", ["symlink", "symlink-parent", "hardlink", "directory", "fifo", "traversal"]
)
def test_all_operator_inputs_refused_before_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str, kind: str
) -> None:
    args = _args(tmp_path)
    original = tmp_path / "input-original"
    original.write_bytes(GOOD if field == "eie_spec" else b"{}")
    unsafe = tmp_path / "unsafe-input"
    if kind == "symlink":
        unsafe.symlink_to(original)
    elif kind == "symlink-parent":
        unsafe.symlink_to(tmp_path, target_is_directory=True)
        unsafe = unsafe / original.name
    elif kind == "hardlink":
        os.link(original, unsafe)
    elif kind == "directory":
        unsafe.mkdir()
    elif kind == "fifo":
        os.mkfifo(unsafe)
    else:
        unsafe = tmp_path / "trusted-ceg" / ".." / original.name
    setattr(args, field, unsafe)

    def forbidden(*_args, **_kwargs):
        pytest.fail("Unsafe input must be refused before trusted CEG execution")

    monkeypatch.setattr(pair.subprocess, "run", forbidden)
    with pytest.raises((OSError, DomainPublicationError)):
        pair.check_pair(args)


@pytest.mark.parametrize("corruption", ["duplicate", "schema"])
async def test_generated_candidate_revalidated_before_applied_keys(
    monkeypatch: pytest.MonkeyPatch, corruption: str
) -> None:
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
    base = parse_native_yaml(GOOD)
    before = copy.deepcopy(base)
    evidence = {digest: GOOD}
    keys = [("Existing", "unchanged")]
    generated = []

    def corrupt(candidate, **kwargs):
        if corruption == "duplicate":
            text = "name: other\n" + original_dump(candidate, **kwargs)
        else:
            candidate = copy.deepcopy(candidate)
            candidate["name"] = "other"
            text = original_dump(candidate, **kwargs)
        generated.append(text.encode("utf-8"))
        return text

    monkeypatch.setattr(workflow.yaml, "safe_dump", corrupt)
    with pytest.raises((yaml.YAMLError, DomainPublicationError)):
        await workflow.materialize_audited_spec(
            base,
            tenant_id="plasticos",
            domain="plasticos",
            discovery_specs=evidence,
            applied_field_keys=keys,
        )
    assert len(generated) == 1
    assert keys == [("Existing", "unchanged")]
    assert base == before
    assert evidence == {digest: GOOD}


def test_installed_revision_has_no_write_permissions(tmp_path: Path) -> None:
    """0o550 is the immutable directory contract: no write bit, no access for others."""
    root_fd = os.open(tmp_path, workflow._DIRECTORY_FLAGS)
    try:
        # Exercise only a disposable install helper, not actual approval/promotion.
        workflow._install_revision(
            root_fd, "test-only-revision", b"exact fixture bytes", {"test": True}
        )
    finally:
        os.close(root_fd)
    revision = tmp_path / "test-only-revision"
    assert stat.S_IMODE(revision.stat().st_mode) == 0o550
    children = (revision, revision / "spec.yaml", revision / "receipt.json")
    for child in children:
        assert child.stat().st_mode & 0o222 == 0
    # Root ignores mode bits, so capability checks are meaningful only for other uids.
    if os.geteuid() != 0:
        for child in children:
            assert not os.access(child, os.W_OK)
        with pytest.raises(PermissionError):
            (revision / "new-child").write_bytes(b"forbidden")
        with pytest.raises(PermissionError):
            (revision / "spec.yaml").write_bytes(b"forbidden")
    assert (revision / "spec.yaml").read_bytes() == b"exact fixture bytes"


@pytest.mark.parametrize("operation", ["read", "report"])
def test_artifact_descriptors_close_on_every_open_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    """Real descriptors must close after failure at every acquisition point."""
    import errno

    artifact = tmp_path / "artifact.json"
    original = b"original report or input"
    artifact.write_bytes(original)
    real_open, real_fstat = os.open, os.fstat

    def invoke():
        if operation == "read":
            assert pair._read(str(artifact)) == original
        else:
            pair._write_report(artifact, b"new complete report")

    # Absolute parent walk plus child read; report adds one staging descriptor.
    total = len(artifact.parts) + (operation == "report")
    for failure_at in range(1, total + 1):
        opened = []
        count = 0

        def tracked_open(*args, _failure_at=failure_at, _opened=opened, **kwargs):
            nonlocal count
            count += 1
            if count == _failure_at:
                raise OSError(errno.EIO, "test-only open failure")
            descriptor = real_open(*args, **kwargs)
            _opened.append(descriptor)
            return descriptor

        with monkeypatch.context() as patch:
            patch.setattr(os, "open", tracked_open)
            with pytest.raises(OSError, match="test-only open failure"):
                invoke()
        assert count == failure_at
        for descriptor in opened:
            with pytest.raises(OSError) as caught:
                real_fstat(descriptor)
            assert caught.value.errno == errno.EBADF
        assert artifact.read_bytes() == original
        assert list(tmp_path.glob(".pair-report-*")) == []


@pytest.mark.parametrize("operation", ["read", "report"])
@pytest.mark.parametrize("fault", ["fstat", "read-or-fdopen"])
def test_artifact_descriptors_close_on_body_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str, fault: str
) -> None:
    import errno

    artifact = tmp_path / "artifact.json"
    artifact.write_bytes(b"original")
    opened = []
    real_open, real_fstat = os.open, os.fstat

    def tracked_open(*args, **kwargs):
        descriptor = real_open(*args, **kwargs)
        opened.append(descriptor)
        return descriptor

    def fail(*_args, **_kwargs):
        raise OSError(errno.EIO, "test-only body failure")

    with monkeypatch.context() as patch:
        patch.setattr(os, "open", tracked_open)
        target = "fstat" if fault == "fstat" else ("read" if operation == "read" else "fdopen")
        patch.setattr(os, target, fail)
        with pytest.raises(OSError, match="test-only body failure"):
            if operation == "read":
                pair._read(str(artifact))
            else:
                pair._write_report(artifact, b"new report")
    for descriptor in opened:
        with pytest.raises(OSError) as caught:
            real_fstat(descriptor)
        assert caught.value.errno == errno.EBADF
    assert artifact.read_bytes() == b"original"
    assert list(tmp_path.glob(".pair-report-*")) == []


@pytest.mark.parametrize("fault", ["file-fsync", "stat", "replace", "link"])
def test_report_failure_before_install_preserves_previous_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    output = tmp_path / "report.json"
    if fault != "link":
        output.write_bytes(b"previous valid report")

    def fail(*_args, **_kwargs):
        raise OSError("test-only installation failure")

    with monkeypatch.context() as patch:
        patch.setattr(os, "fsync" if fault == "file-fsync" else fault, fail)
        with pytest.raises(OSError, match="test-only installation failure"):
            pair._write_report(output, b"new complete report")
    if fault != "link":
        assert output.read_bytes() == b"previous valid report"
    else:
        assert not output.exists()
    assert list(tmp_path.glob(".pair-report-*")) == []


def test_report_refuses_changed_child_before_replace(tmp_path: Path, monkeypatch) -> None:
    output = tmp_path / "report.json"
    target = tmp_path / "unrelated.txt"
    output.write_bytes(b"previous report")
    target.write_bytes(b"unchanged target")
    original_write = workflow._write_file

    def change_child(*args, **kwargs):
        original_write(*args, **kwargs)
        output.unlink()
        output.symlink_to(target)

    monkeypatch.setattr(workflow, "_write_file", change_child)
    with pytest.raises(ValueError, match="changed during write"):
        pair._write_report(output, b"new report")
    assert output.is_symlink()
    assert target.read_bytes() == b"unchanged target"
    assert list(tmp_path.glob(".pair-report-*")) == []


def test_report_creation_does_not_overwrite_concurrent_child(tmp_path: Path, monkeypatch) -> None:
    output = tmp_path / "report.json"
    original_write = workflow._write_file

    def create_child(*args, **kwargs):
        original_write(*args, **kwargs)
        output.write_bytes(b"concurrent valid report")

    monkeypatch.setattr(workflow, "_write_file", create_child)
    with pytest.raises(FileExistsError):
        pair._write_report(output, b"must not replace concurrent report")
    assert output.read_bytes() == b"concurrent valid report"
    assert list(tmp_path.glob(".pair-report-*")) == []


def test_private_regular_input_byte_bound(tmp_path: Path) -> None:
    artifact = tmp_path / "oversized.yaml"
    artifact.write_bytes(b"x" * (workflow._MAX_FILE_BYTES + 1))
    with pytest.raises(DomainPublicationError, match="size limit"):
        pair._read(str(artifact))


def test_regular_inputs_keep_exact_snapshots_and_trusted_execution_semantics(
    tmp_path, monkeypatch
) -> None:
    """Synthetic diagnostic only; actual pinned CLI proof is a separate integration."""
    args = _args(tmp_path)
    original_ceg = args.ceg_spec.read_bytes()
    original_receipt = b'{"test_only": true}\n'
    for engine in ("eie", "ceg"):
        receipt = tmp_path / f"{engine}-receipt.json"
        receipt.write_bytes(original_receipt)
        setattr(args, f"{engine}_receipt", receipt)
    args.field_mappings = tmp_path / "mappings.json"
    args.field_mappings.write_bytes(b"{}\n")
    calls = []

    def native(command, **kwargs):
        calls.append((command, kwargs))
        assert kwargs.get("shell", False) is False
        if command[0] == "git":
            assert command == ["git", "-C", str(args.ceg_repo), "rev-parse", "HEAD"]
            return SimpleNamespace(returncode=0, stdout="test-only-revision\n")
        assert command[:2] == [
            str(args.ceg_python),
            str(args.ceg_repo / "tools/validate_domain.py"),
        ]
        snapshot = Path(command[2])
        assert snapshot.read_bytes() == original_ceg
        assert (snapshot.parent / "receipt.json").read_bytes() == original_receipt
        assert command[3:] == [
            "--root",
            str(snapshot.parent),
            "--strict",
            "--complete",
            "--json",
            "--receipt",
            str(snapshot.parent / "receipt.json"),
            "--require-reviewed",
        ]
        assert kwargs["cwd"] == args.ceg_repo
        assert kwargs["env"]["PYTHONPATH"] == str(args.ceg_repo)
        args.eie_spec.write_bytes(b"changed after snapshot")
        args.ceg_spec.write_bytes(b"changed after snapshot")
        args.field_mappings.write_bytes(b'{"changed": true}')
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")

    def compatibility(eie, _ceg, *, field_mappings):
        assert eie == parse_native_yaml(GOOD)
        assert field_mappings == {}
        return {"verdict": "pass", "eie_version": "0.3.0", "ceg_version": "1.0.0"}

    monkeypatch.setattr(pair.subprocess, "run", native)
    monkeypatch.setattr(pair, "check_compatibility", compatibility)
    report, code = pair.check_pair(args)
    assert len(calls) == 2
    assert report["eie_sha256"] == hashlib.sha256(GOOD).hexdigest()
    assert report["ceg_sha256"] == hashlib.sha256(original_ceg).hexdigest()
    assert code == 1 and report["release_ready"] is False  # Synthetic receipts cannot approve.
