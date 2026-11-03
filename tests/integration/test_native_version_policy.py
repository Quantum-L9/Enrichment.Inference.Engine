"""F-004 local-only publisher/reader witnesses; all approvals are fixtures.

L9_META:
  tier: 2
  authority: native-domain-remediation-F004
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest
import yaml

from app.engines.domain_yaml_reader import DomainPublicationError, DomainYamlReader
from app.services import domain_spec_workflow as workflow

REPO = Path(__file__).parents[2]
pytestmark = pytest.mark.integration


def _spec(version: str = "1.0.0", *, altered: bool = False) -> bytes:
    # Same complete native specimen as the original publication regression suite.
    raw = yaml.safe_load((REPO / "domains/plasticos/spec.yaml").read_bytes())
    raw["name"] = "alpha"
    raw["version"] = version
    if altered:
        raw["ontology"]["nodes"][0]["enrichment_hints"]["objective_template"] += " Reviewed change."
    return yaml.safe_dump(raw, sort_keys=False, allow_unicode=True).encode()


def _publish(root: Path, value: bytes, previous: str | None = None) -> dict:
    return workflow.publish_effective_spec(
        root,
        "alpha",
        value,
        reviewed_input_sha256=hashlib.sha256(value).hexdigest(),
        reviewed_by="Fixture Version Reviewer",
        approval_ref="test-fixture-only/version-policy",
        approval_batches=("fixture-version-batch",),
        expected_previous_sha256=previous,
    )


def _pointer(root: Path) -> str:
    return json.loads((root / "alpha/current.json").read_bytes())["sha256"]


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and not path.is_symlink()
    }


def _refuse_unchanged(root: Path, value: bytes, previous: str, match: str = "SemVer") -> None:
    before = _snapshot(root)
    with pytest.raises(DomainPublicationError, match=match):
        _publish(root, value, previous)
    assert _snapshot(root) == before
    assert _pointer(root) == previous


def test_ordinary_downgrade_1_1_to_0_9_refused(tmp_path: Path) -> None:
    current = _publish(tmp_path, _spec("1.1.0"))
    _refuse_unchanged(tmp_path, _spec("0.9.0"), current["sha256"])
    loaded = DomainYamlReader(tmp_path, require_published=True).load("alpha")
    assert loaded.version == "1.1.0"
    assert loaded.source_sha256 == current["sha256"]


def test_retained_prior_hash_requires_explicit_rollback(tmp_path: Path) -> None:
    original = _spec()
    first = _publish(tmp_path, original)
    current = _publish(tmp_path, _spec("1.1.0"), first["sha256"])
    _refuse_unchanged(tmp_path, original, current["sha256"])


@pytest.mark.parametrize("alteration", ["meaningful", "whitespace"])
def test_same_version_binds_exact_original_bytes(tmp_path: Path, alteration: str) -> None:
    original = _spec("1.1.0")
    current = _publish(tmp_path, original)
    value = _spec("1.1.0", altered=True) if alteration == "meaningful" else original + b"\n"
    assert hashlib.sha256(value).hexdigest() != current["sha256"]
    _refuse_unchanged(tmp_path, value, current["sha256"])


def test_exact_hash_and_review_idempotence_preserve_original_receipt(tmp_path: Path) -> None:
    original = _spec()
    receipt = _publish(tmp_path, original)
    before = _snapshot(tmp_path)
    # Preserve existing matching-current retry semantics, including stale CAS input.
    assert _publish(tmp_path, original) == receipt
    assert _publish(tmp_path, original, "f" * 64) == receipt
    assert _snapshot(tmp_path) == before
    assert (tmp_path / f"alpha/revisions/{receipt['sha256']}/spec.yaml").read_bytes() == original


@pytest.mark.parametrize("rollback", [False, True])
@pytest.mark.parametrize("alteration", ["meaningful", "whitespace", "build_metadata"])
def test_version_binding_survives_newer_current_and_rollback(
    tmp_path: Path, rollback: bool, alteration: str
) -> None:
    original = _spec()
    first = _publish(tmp_path, original)
    second = _publish(tmp_path, _spec("1.1.0"), first["sha256"])
    current = second
    if rollback:
        current = workflow.rollback_effective_spec(
            tmp_path, "alpha", first["sha256"], expected_previous_sha256=second["sha256"]
        )
        assert current == first
    values = {
        "meaningful": _spec(altered=True),
        "whitespace": original + b"\n# changed reviewed bytes\n",
        "build_metadata": _spec("1.0.0+retry.02"),
    }
    _refuse_unchanged(tmp_path, values[alteration], current["sha256"])


@pytest.mark.parametrize("alteration", ["meaningful", "whitespace", "build_metadata"])
def test_retained_higher_precedence_conflict_after_lower_current_rollback(
    tmp_path: Path, alteration: str
) -> None:
    first = _publish(tmp_path, _spec())
    retained_bytes = _spec("1.1.0+linux")
    retained = _publish(tmp_path, retained_bytes, first["sha256"])
    newest = _publish(tmp_path, _spec("1.2.0"), retained["sha256"])
    assert (
        workflow.rollback_effective_spec(
            tmp_path, "alpha", first["sha256"], expected_previous_sha256=newest["sha256"]
        )
        == first
    )
    values = {
        "meaningful": _spec("1.1.0+linux", altered=True),
        "whitespace": retained_bytes + b"\n",
        "build_metadata": _spec("1.1.0+windows"),
    }
    _refuse_unchanged(tmp_path, values[alteration], first["sha256"])


@pytest.mark.parametrize(
    ("first_version", "attempt"),
    [
        ("1.1.0", "1.1.0+ci.02"),
        ("1.1.0+linux.001", "1.1.0+windows.002"),
        ("1.1.0+linux", "1.1.0"),
        ("1.1.0-rc.1+linux", "1.1.0-rc.1+windows"),
    ],
)
def test_build_metadata_cannot_evade_precedence_uniqueness(
    tmp_path: Path, first_version: str, attempt: str
) -> None:
    first = _publish(tmp_path, _spec(first_version))
    _refuse_unchanged(tmp_path, _spec(attempt), first["sha256"])


def test_numeric_core_and_semver_prerelease_order(tmp_path: Path) -> None:
    versions = [
        "1.9.0",
        "1.10.0-0",
        "1.10.0-0.1",
        "1.10.0-1",
        "1.10.0-A",
        "1.10.0-Z",
        "1.10.0-a",
        "1.10.0-alpha",
        "1.10.0-alpha.1",
        "1.10.0-alpha.2",
        "1.10.0-alpha.10",
        "1.10.0-alpha.beta",
        "1.10.0-beta",
        "1.10.0-beta.2",
        "1.10.0-beta.11",
        "1.10.0-rc.1",
        "1.10.0+release.0001",
        "1.10.1-0",
        "2.0.0",
    ]
    previous_bytes = _spec(versions[0])
    current = _publish(tmp_path, previous_bytes)
    for version in versions[1:]:
        value = _spec(version)
        newer = _publish(tmp_path, value, current["sha256"])
        assert newer["previous_sha256"] == current["sha256"]
        _refuse_unchanged(tmp_path, previous_bytes, newer["sha256"])
        loaded = DomainYamlReader(tmp_path, require_published=True).load("alpha")
        assert loaded.version == version
        assert loaded.source_sha256 == newer["sha256"]
        previous_bytes, current = value, newer


def test_large_numeric_identifiers_do_not_depend_on_machine_integer_limits(tmp_path: Path) -> None:
    huge = "9" * 5000
    versions = ["1.0.0-" + huge, "1.0.0", huge + ".0.0"]
    previous_bytes = _spec(versions[0])
    current = _publish(tmp_path, previous_bytes)
    for version in versions[1:]:
        value = _spec(version)
        current = _publish(tmp_path, value, current["sha256"])
        _refuse_unchanged(tmp_path, previous_bytes, current["sha256"])
        assert DomainYamlReader(tmp_path, require_published=True).load("alpha").version == version
        previous_bytes = value


@pytest.mark.parametrize(
    "version", ["1.01.0", "1.0.0-01", "1.0.0-alpha.01", "1.0.0+", "1.0.1\u0661"]
)
def test_invalid_semver_never_creates_current(tmp_path: Path, version: str) -> None:
    with pytest.raises(DomainPublicationError):
        _publish(tmp_path, _spec(version))
    assert not (tmp_path / "alpha/current.json").exists()


@pytest.mark.parametrize(
    "corruption",
    [
        "unknown_name",
        "unfinished_staging",
        "missing_spec",
        "changed_bytes",
        "invalid_receipt",
        "unknown_previous",
        "symlink_revision",
        "nondirectory_revision",
        "historical_precedence_conflict",
    ],
)
@pytest.mark.parametrize("duplicate_current", [False, True])
def test_unknown_or_corrupt_retained_history_refuses_publication(
    tmp_path: Path, corruption: str, duplicate_current: bool
) -> None:
    root = tmp_path / "store"
    first = _publish(root, _spec())
    current_bytes = _spec("1.1.0")
    current = _publish(root, current_bytes, first["sha256"])
    revisions = root / "alpha/revisions"
    prior = revisions / first["sha256"]
    if corruption in {"unknown_name", "unfinished_staging"}:
        (revisions / ("untrusted" if corruption == "unknown_name" else ".staging-crash")).mkdir()
    elif corruption == "symlink_revision":
        (revisions / ("f" * 64)).symlink_to(prior, target_is_directory=True)
    elif corruption == "nondirectory_revision":
        (revisions / ("f" * 64)).write_bytes(b"not a revision")
    elif corruption == "historical_precedence_conflict":
        other = _publish(tmp_path / "other", _spec(altered=True))
        shutil.copytree(
            tmp_path / f"other/alpha/revisions/{other['sha256']}", revisions / other["sha256"]
        )
    elif corruption == "missing_spec":
        prior.chmod(0o755)
        (prior / "spec.yaml").unlink()
    elif corruption == "changed_bytes":
        (prior / "spec.yaml").chmod(0o644)
        (prior / "spec.yaml").write_bytes(_spec("8.0.0"))
    else:
        path = prior / "receipt.json"
        path.chmod(0o644)
        changed = dict(first)
        changed["reviewed_by" if corruption == "invalid_receipt" else "previous_sha256"] = (
            "" if corruption == "invalid_receipt" else "f" * 64
        )
        path.write_text(json.dumps(changed))
    candidate = current_bytes if duplicate_current else _spec("1.2.0")
    _refuse_unchanged(root, candidate, current["sha256"], match=".*")


def test_explicit_rollback_retains_receipts_and_allows_exact_forward_restore(
    tmp_path: Path,
) -> None:
    original = _spec()
    first = _publish(tmp_path, original)
    newer = _spec("1.1.0")
    second = _publish(tmp_path, newer, first["sha256"])
    before = _snapshot(tmp_path)
    restored = workflow.rollback_effective_spec(
        tmp_path, "alpha", first["sha256"], expected_previous_sha256=second["sha256"]
    )
    assert restored == first
    assert _pointer(tmp_path) == first["sha256"]
    after = _snapshot(tmp_path)
    assert {key: value for key, value in after.items() if key != "alpha/current.json"} == {
        key: value for key, value in before.items() if key != "alpha/current.json"
    }
    loaded = DomainYamlReader(tmp_path, require_published=True).load("alpha")
    assert loaded.version == "1.0.0"
    assert loaded.source_sha256 == first["sha256"]
    assert _publish(tmp_path, newer, first["sha256"]) == second
    assert _snapshot(tmp_path) == before


def test_stale_publication_and_rollback_cas_preserved(tmp_path: Path) -> None:
    first = _publish(tmp_path, _spec())
    second = _publish(tmp_path, _spec("1.1.0"), first["sha256"])
    before = _snapshot(tmp_path)
    with pytest.raises(DomainPublicationError, match="Compare-and-swap"):
        _publish(tmp_path, _spec("1.2.0"), first["sha256"])
    with pytest.raises(DomainPublicationError, match="Compare-and-swap"):
        workflow.rollback_effective_spec(
            tmp_path, "alpha", first["sha256"], expected_previous_sha256=first["sha256"]
        )
    assert _pointer(tmp_path) == second["sha256"]
    assert _snapshot(tmp_path) == before
