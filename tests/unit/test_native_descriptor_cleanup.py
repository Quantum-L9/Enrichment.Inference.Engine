"""Exercise native descriptor ownership with real Linux descriptors and failures.

L9_META:
  tier: 2
  authority: native-hosted-correction-g27r-20261104
"""

from __future__ import annotations

import errno
import os
import stat

import pytest

from app.engines.domain_yaml_reader import DomainPublicationError
from app.services import domain_spec_workflow as workflow
from scripts import domain_spec_operator as operator


def _fd_snapshot() -> set[int]:
    """Open directory and regular-file descriptors of this process.

    The native owners under test open only directories and regular files, so
    those are the only kinds a leak can take. Sockets, pipes and anonymous
    descriptors are excluded: background threads (the OpenTelemetry gRPC
    exporter retrying its collector, for one) open and close them on their own
    schedule, which made a whole-process comparison fail at random. The
    transient descriptor used to enumerate procfs itself is ignored too.
    """
    descriptors: set[int] = set()
    for name in os.listdir("/proc/self/fd"):
        descriptor = int(name)
        try:
            mode = os.fstat(descriptor).st_mode
        except OSError as exc:
            assert exc.errno == errno.EBADF
        else:
            if stat.S_ISDIR(mode) or stat.S_ISREG(mode):
                descriptors.add(descriptor)
    return descriptors


def _assert_closed(opened: list[int], before: set[int]) -> None:
    assert _fd_snapshot() == before
    for descriptor in opened:
        with pytest.raises(OSError) as failure:
            os.fstat(descriptor)
        assert failure.value.errno == errno.EBADF


def _track_open(monkeypatch: pytest.MonkeyPatch, opened: list[int], fail_at: int = 0) -> None:
    real_open = os.open
    calls = 0

    def tracked_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal calls
        calls += 1
        if calls == fail_at:
            raise OSError(errno.EACCES, "injected native descriptor open failure")
        descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
        opened.append(descriptor)
        return descriptor

    monkeypatch.setattr(workflow.os, "open", tracked_open)


@pytest.mark.parametrize("create", [False, True])
@pytest.mark.parametrize("failure", [None, RuntimeError, OSError])
def test_parent_closes_real_descriptors_on_exit(tmp_path, monkeypatch, create, failure) -> None:
    artifact = tmp_path / "operator" / "nested" / "artifact.json"
    if not create:
        artifact.parent.mkdir(parents=True)
    before = _fd_snapshot()
    opened: list[int] = []
    _track_open(monkeypatch, opened)

    def use_parent() -> None:
        with operator._parent(str(artifact), create=create) as (parent_fd, name):
            assert name == artifact.name
            assert os.path.samestat(os.fstat(parent_fd), artifact.parent.stat())
            if failure is not None:
                raise failure("injected context body failure")

    if failure is None:
        use_parent()
    else:
        with pytest.raises(failure, match="injected context body failure"):
            use_parent()
    assert len(opened) == len(artifact.absolute().parts) - 1
    _assert_closed(opened, before)


@pytest.mark.parametrize("create", [False, True])
def test_parent_closes_ancestors_at_every_open_failure(tmp_path, monkeypatch, create) -> None:
    artifact = tmp_path / "operator" / "nested" / "artifact.json"
    artifact.parent.mkdir(parents=True)
    # Includes the initial anchor and every intermediate component, not a mock
    # directory descriptor or a source-text assertion about a finally block.
    for fail_at in range(1, len(artifact.absolute().parts)):
        opened: list[int] = []
        before = _fd_snapshot()
        with monkeypatch.context() as patch:
            _track_open(patch, opened, fail_at)
            with (
                pytest.raises(OSError, match="injected native descriptor open failure"),
                operator._parent(str(artifact), create=create),
            ):
                pytest.fail("failed acquisition must not yield")
        assert len(opened) == fail_at - 1
        _assert_closed(opened, before)


@pytest.mark.parametrize("create", [False, True])
@pytest.mark.parametrize("failure", [None, RuntimeError, OSError])
def test_store_closes_real_descriptors_and_releases_lock(
    tmp_path, monkeypatch, create, failure
) -> None:
    root = tmp_path / "store"
    if not create:
        (root / "alpha" / "revisions").mkdir(parents=True)
    before = _fd_snapshot()
    opened: list[int] = []
    with monkeypatch.context() as patch:
        _track_open(patch, opened)

        def use_store() -> None:
            with workflow._store(root, "alpha", create=create) as (tenant_fd, revisions_fd):
                assert os.path.samestat(os.fstat(tenant_fd), (root / "alpha").stat())
                assert os.path.samestat(
                    os.fstat(revisions_fd), (root / "alpha" / "revisions").stat()
                )
                if failure is not None:
                    raise failure("injected context body failure")

        if failure is None:
            use_store()
        elif failure is OSError:
            with pytest.raises(DomainPublicationError, match="Unsafe or inaccessible") as error:
                use_store()
            assert isinstance(error.value.__cause__, OSError)
        else:
            with pytest.raises(failure, match="injected context body failure"):
                use_store()
    assert len(opened) == len(root.absolute().parts) + 3
    _assert_closed(opened, before)
    # An actual nonblocking flock proves the previous lock has been released.
    lock_fd = os.open(root / "alpha" / ".publication.lock", os.O_RDWR | os.O_NOFOLLOW)
    try:
        workflow.fcntl.flock(lock_fd, workflow.fcntl.LOCK_EX | workflow.fcntl.LOCK_NB)
    finally:
        os.close(lock_fd)
    assert _fd_snapshot() == before


@pytest.mark.parametrize("create", [False, True])
def test_store_closes_ancestors_tenant_revisions_and_lock_at_every_open_failure(
    tmp_path, monkeypatch, create
) -> None:
    root = tmp_path / "store"
    (root / "alpha" / "revisions").mkdir(parents=True)
    for fail_at in range(1, len(root.absolute().parts) + 4):
        before = _fd_snapshot()
        opened: list[int] = []
        with monkeypatch.context() as patch:
            _track_open(patch, opened, fail_at)
            with (
                pytest.raises(DomainPublicationError, match="Unsafe or inaccessible") as error,
                workflow._store(root, "alpha", create=create),
            ):
                pytest.fail("failed acquisition must not yield")
            assert isinstance(error.value.__cause__, OSError)
            assert error.value.__cause__.errno == errno.EACCES
        assert len(opened) == fail_at - 1
        _assert_closed(opened, before)


@pytest.mark.parametrize("failure_stage", ["fstat", "flock"])
def test_store_closes_lock_and_ancestors_on_lock_setup_failure(
    tmp_path, monkeypatch, failure_stage
) -> None:
    root = tmp_path / "store"
    (root / "alpha" / "revisions").mkdir(parents=True)
    before = _fd_snapshot()
    opened: list[int] = []
    real_fstat = os.fstat

    def fail_fstat(descriptor):
        if opened and descriptor == opened[-1]:
            raise OSError(errno.EIO, "injected lock metadata failure")
        return real_fstat(descriptor)

    def fail_flock(descriptor, operation):
        assert descriptor == opened[-1]
        assert operation == workflow.fcntl.LOCK_EX
        raise OSError(errno.EIO, "injected lock acquisition failure")

    with monkeypatch.context() as patch:
        _track_open(patch, opened)
        if failure_stage == "fstat":
            patch.setattr(workflow.os, "fstat", fail_fstat)
        else:
            patch.setattr(workflow.fcntl, "flock", fail_flock)
        with (
            pytest.raises(DomainPublicationError, match="Unsafe or inaccessible") as error,
            workflow._store(root, "alpha", create=False),
        ):
            pytest.fail("failed lock setup must not yield")
        assert isinstance(error.value.__cause__, OSError)
        assert error.value.__cause__.errno == errno.EIO
    assert len(opened) == len(root.absolute().parts) + 3
    _assert_closed(opened, before)


@pytest.mark.parametrize("lock_kind", ["symlink", "hardlink", "fifo"])
def test_store_refuses_unsafe_real_lock_without_descriptor_leak(
    tmp_path, monkeypatch, lock_kind
) -> None:
    root = tmp_path / "store"
    tenant = root / "alpha"
    (tenant / "revisions").mkdir(parents=True)
    lock = tenant / ".publication.lock"
    target = tmp_path / "external-lock"
    target.write_bytes(b"unchanged outside lock")
    if lock_kind == "symlink":
        lock.symlink_to(target)
    elif lock_kind == "hardlink":
        os.link(target, lock)
    else:
        os.mkfifo(lock)
    before = _fd_snapshot()
    opened: list[int] = []
    with monkeypatch.context() as patch:
        _track_open(patch, opened)
        with (
            pytest.raises(DomainPublicationError),
            workflow._store(root, "alpha", create=False),
        ):
            pytest.fail("unsafe lock must not yield")
    assert target.read_bytes() == b"unchanged outside lock"
    _assert_closed(opened, before)


@pytest.mark.parametrize("owner", ["parent", "store"])
def test_native_owners_refuse_symlink_ancestor_without_descriptor_leak(
    tmp_path, monkeypatch, owner
) -> None:
    outside = tmp_path / "outside"
    (outside / "alpha" / "revisions").mkdir(parents=True)
    linked = tmp_path / "linked"
    linked.symlink_to(outside, target_is_directory=True)
    before = _fd_snapshot()
    opened: list[int] = []
    with monkeypatch.context() as patch:
        _track_open(patch, opened)
        if owner == "parent":
            with pytest.raises(OSError), operator._parent(str(linked / "artifact.json")):
                pytest.fail("symlink ancestor must not yield")
        else:
            with (
                pytest.raises(DomainPublicationError),
                workflow._store(linked, "alpha", create=False),
            ):
                pytest.fail("symlink ancestor must not yield")
    assert not (outside / "alpha" / ".publication.lock").exists()
    _assert_closed(opened, before)


@pytest.mark.parametrize("owner", ["parent", "store"])
@pytest.mark.parametrize("failure_stage", ["mkdir", "fsync"])
def test_native_owners_close_descriptors_on_directory_creation_failure(
    tmp_path, monkeypatch, owner, failure_stage
) -> None:
    root = tmp_path / "new-native"
    before = _fd_snapshot()
    opened: list[int] = []
    real_mkdir = os.mkdir

    def fail_mkdir(path, mode=0o777, *, dir_fd=None):
        if path == root.name:
            raise OSError(errno.EACCES, "injected directory creation failure")
        return real_mkdir(path, mode, dir_fd=dir_fd)

    def fail_fsync(descriptor):
        assert descriptor == opened[-1]
        raise OSError(errno.EIO, "injected directory synchronization failure")

    with monkeypatch.context() as patch:
        _track_open(patch, opened)
        if failure_stage == "mkdir":
            patch.setattr(workflow.os, "mkdir", fail_mkdir)
        else:
            patch.setattr(workflow.os, "fsync", fail_fsync)
        if owner == "parent":
            with (
                pytest.raises(OSError, match="injected directory"),
                operator._parent(str(root / "artifact.json"), create=True),
            ):
                pytest.fail("directory creation failure must not yield")
        else:
            with (
                pytest.raises(DomainPublicationError, match="Unsafe or inaccessible") as error,
                workflow._store(root, "alpha", create=True),
            ):
                pytest.fail("directory creation failure must not yield")
            assert isinstance(error.value.__cause__, OSError)
    assert root.exists() is (failure_stage == "fsync")
    assert not (root / "alpha" / ".publication.lock").exists()
    _assert_closed(opened, before)
