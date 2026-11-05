"""The immutable reviewed YAML, not DB audit rows, is effective authority.

L9_META:
  tier: 2
  domain: enrichment
  authority: native-domain-supply-chain-g27r-v3
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import yaml

from app.engines.domain_yaml_reader import DomainPublicationError, DomainYamlReader

pytestmark = pytest.mark.unit
SPEC = Path(__file__).resolve().parents[2] / "domains/plasticos/spec.yaml"


def _publish(
    root: Path, *, version: str = "0.3.0", tenant: str = "plasticos"
) -> tuple[Path, dict[str, Any]]:
    native = yaml.safe_load(SPEC.read_bytes())
    native["name"] = tenant
    native["version"] = version
    content = yaml.safe_dump(native, sort_keys=False).encode()
    sha = hashlib.sha256(content).hexdigest()
    revision = root / tenant / "revisions" / sha
    revision.mkdir(parents=True, exist_ok=True)
    (revision / "spec.yaml").write_bytes(content)
    # Explicit test-only evidence: not a real operator approval.
    receipt = {
        "schema": "eie.domain-approval/v1",
        "tenant_id": tenant,
        "version": version,
        "sha256": sha,
        "reviewed_input_sha256": sha,
        "reviewed_by": "test-only-reviewer",
        "approval_ref": "test-only-approval",
        "reviewed_at": "2026-01-01T00:00:00Z",
        "previous_sha256": None,
        "approval_batches": [],
    }
    (revision / "receipt.json").write_text(json.dumps(receipt), encoding="utf-8")
    (root / tenant / "current.json").write_text(
        json.dumps({"schema": "eie.domain-current/v1", "sha256": sha}), encoding="utf-8"
    )
    return revision, receipt


def test_required_root_missing_refuses_constructor(tmp_path: Path) -> None:
    with pytest.raises(DomainPublicationError):
        DomainYamlReader(tmp_path / "missing", require_published=True)
    assert DomainYamlReader(tmp_path / "missing").list_domains() == []


def test_published_config_binds_exact_bytes_and_approval(tmp_path: Path) -> None:
    revision, receipt = _publish(tmp_path)
    reader = DomainYamlReader(tmp_path, require_published=True)
    assert reader.require_published is True
    assert reader.list_directory_domains() == ["plasticos"]
    config = reader.load("plasticos")
    assert config.source_sha256 == hashlib.sha256((revision / "spec.yaml").read_bytes()).hexdigest()
    assert config.approval_ref == receipt["approval_ref"]
    assert config.version == "0.3.0"
    assert config.raw_spec["inference_rules"]
    assert config.node_schemas["Facility"].enrichment_hints.priority_fields


def test_read_approved_spec_bytes_once_per_load(tmp_path: Path) -> None:
    _publish(tmp_path)
    reader = DomainYamlReader(tmp_path, require_published=True)
    with patch.object(reader, "_read_published_file", wraps=reader._read_published_file) as read:
        reader.load("plasticos")
    assert len([call for call in read.call_args_list if call.args[0][-1] == "spec.yaml"]) == 1


def test_pointer_reload_and_restart_select_same_effective_revision(tmp_path: Path) -> None:
    _publish(tmp_path)
    reader = DomainYamlReader(tmp_path, require_published=True)
    old = reader.load("plasticos")
    _publish(tmp_path, version="0.4.0-discovered")
    current = reader.load("plasticos")
    restarted = DomainYamlReader(tmp_path, require_published=True).load("plasticos")
    assert current == restarted
    assert current.source_sha256 != old.source_sha256
    assert current.version == "0.4.0-discovered"


def test_returns_are_defensive_including_nested_hints_and_raw_spec(tmp_path: Path) -> None:
    _publish(tmp_path)
    reader = DomainYamlReader(tmp_path)
    config = reader.load("plasticos")
    config.raw_spec["inference_rules"].clear()
    config.node_schemas["Facility"].properties.clear()
    hints = reader.get_enrichment_hints("plasticos", "Facility")
    hints["priority_fields"].clear()
    schema = reader.get_target_schema("plasticos", "Facility")
    schema.clear()
    fresh = reader.load("plasticos")
    assert fresh.raw_spec["inference_rules"]
    assert fresh.node_schemas["Facility"].properties
    assert fresh.node_schemas["Facility"].enrichment_hints.priority_fields


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("schema", "other/v1"),
        ("tenant_id", "other"),
        ("version", "9.0.0"),
        ("sha256", "a" * 64),
        ("reviewed_input_sha256", "invalid"),
        ("reviewed_by", ""),
        ("approval_ref", ""),
        ("reviewed_at", "not-a-date"),
        ("reviewed_at", "2999-01-01T00:00:00Z"),
        ("reviewed_at", "2026-01-01"),
        ("previous_sha256", "bad"),
        ("approval_batches", {}),
    ],
)
def test_receipt_refusal_after_cache_fill(tmp_path: Path, key: str, value: object) -> None:
    revision, receipt = _publish(tmp_path)
    reader = DomainYamlReader(tmp_path)
    reader.load("plasticos")
    receipt[key] = value
    (revision / "receipt.json").write_text(json.dumps(receipt), encoding="utf-8")
    with pytest.raises(DomainPublicationError):
        reader.load("plasticos")


@pytest.mark.parametrize("name", ["spec.yaml", "receipt.json", "current.json"])
def test_missing_published_component_never_uses_legacy(tmp_path: Path, name: str) -> None:
    revision, _ = _publish(tmp_path)
    tenant = tmp_path / "plasticos"
    (tenant / "spec.yaml").write_bytes(SPEC.read_bytes())
    reader = DomainYamlReader(tmp_path)
    reader.load("plasticos")
    path = tenant / name if name == "current.json" else revision / name
    path.unlink()
    with pytest.raises(DomainPublicationError):
        reader.load("plasticos")


def test_digest_tamper_detected_even_when_mtime_and_size_unchanged(tmp_path: Path) -> None:
    revision, _ = _publish(tmp_path)
    path = revision / "spec.yaml"
    reader = DomainYamlReader(tmp_path)
    reader.load("plasticos")
    stat = path.stat()
    path.write_bytes(path.read_bytes().replace(b"0.3.0", b"9.3.0", 1))
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    with pytest.raises(DomainPublicationError, match="digest"):
        reader.load("plasticos")


@pytest.mark.parametrize("sha", ["../escape", "A" * 64, "a" * 63, None])
def test_unsafe_pointer_never_falls_back(tmp_path: Path, sha: object) -> None:
    _publish(tmp_path)
    (tmp_path / "plasticos/spec.yaml").write_bytes(SPEC.read_bytes())
    (tmp_path / "plasticos/current.json").write_text(
        json.dumps({"schema": "eie.domain-current/v1", "sha256": sha}), encoding="utf-8"
    )
    with pytest.raises(DomainPublicationError):
        DomainYamlReader(tmp_path).load("plasticos")


@pytest.mark.parametrize(
    "component", ["tenant", "revisions", "revision", "spec.yaml", "receipt.json", "current.json"]
)
def test_symlink_published_components_fail(tmp_path: Path, component: str) -> None:
    root = tmp_path / "root"
    revision, _ = _publish(root)
    tenant = root / "plasticos"
    path = {"tenant": tenant, "revisions": tenant / "revisions", "revision": revision}.get(
        component
    )
    if path is None:
        path = tenant / component if component == "current.json" else revision / component
    relocated = tmp_path / "relocated"
    path.rename(relocated)
    path.symlink_to(relocated, target_is_directory=relocated.is_dir())
    with pytest.raises(DomainPublicationError):
        DomainYamlReader(root, require_published=True).load("plasticos")


def test_legacy_fixture_compatibility_and_bounded_cache(tmp_path: Path) -> None:
    reader = DomainYamlReader(tmp_path, max_cache_entries=2)
    for tenant in ("acme", "beta", "gamma"):
        (tmp_path / f"{tenant}.yaml").write_text(
            yaml.safe_dump(
                {
                    "domain": tenant,
                    "ontology": {"nodes": [{"label": "Partner", "properties": ["id"]}]},
                }
            ),
            encoding="utf-8",
        )
        config = reader.load(tenant)
        assert config.source_sha256 is None
        assert config.approval_ref is None
        assert config.node_schemas["Partner"].properties == {"id": "string"}
    assert len(reader._cache) <= 2
    reader.invalidate("gamma")
    assert "gamma" not in reader._cache
    reader.invalidate()
    assert not reader._cache
    with pytest.raises(DomainPublicationError):
        DomainYamlReader(tmp_path, require_published=True).load("acme")


def test_published_reference_cannot_escape_immutable_yaml_authority(tmp_path: Path) -> None:
    native = yaml.safe_load(SPEC.read_bytes())
    native.pop("inference_rules")
    native["inference_rules_path"] = "rules/inference.yaml"
    with patch.object(Path, "read_bytes", return_value=yaml.safe_dump(native).encode()):
        _publish(tmp_path)
    with pytest.raises(DomainPublicationError, match="inline"):
        DomainYamlReader(tmp_path, require_published=True).load("plasticos")


def test_snapshot_hints_do_not_reload_or_mutate_snapshot(tmp_path: Path) -> None:
    from app.engines.domain_yaml_reader import enrichment_hints_for

    _publish(tmp_path)
    reader = DomainYamlReader(tmp_path)
    config = reader.load("plasticos")
    _publish(tmp_path, version="0.4.0")
    with patch.object(reader, "load", side_effect=AssertionError("must not reload")):
        hints = enrichment_hints_for(config, "Facility")
        hints["priority_fields"].clear()
        assert enrichment_hints_for(config, "Facility")["priority_fields"]
        assert enrichment_hints_for(config, "Unknown") == {}
    assert config.version == "0.3.0"


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("reviewed_input_sha256", "b" * 64),
        ("approval_batches", [""]),
        ("approval_batches", ["batch", "batch"]),
        ("approval_batches", [{}]),
    ],
)
def test_stale_review_and_malformed_batch_receipts_refused(
    tmp_path: Path, key: str, value: object
) -> None:
    revision, receipt = _publish(tmp_path)
    receipt[key] = value
    (revision / "receipt.json").write_text(json.dumps(receipt), encoding="utf-8")
    with pytest.raises(DomainPublicationError):
        DomainYamlReader(tmp_path, require_published=True).load("plasticos")


def test_valid_looking_receipt_edit_refused_repeatedly(tmp_path: Path) -> None:
    revision, receipt = _publish(tmp_path)
    reader = DomainYamlReader(tmp_path)
    reader.load("plasticos")
    receipt["approval_ref"] = "different-test-approval"
    (revision / "receipt.json").write_text(json.dumps(receipt), encoding="utf-8")
    for _ in range(2):
        with pytest.raises(DomainPublicationError, match="Immutable approval receipt changed"):
            reader.load("plasticos")


def test_pointer_switch_during_admission_is_fail_closed(tmp_path: Path) -> None:
    _publish(tmp_path)
    reader = DomainYamlReader(tmp_path)
    read = reader._read_published_file
    pointer_reads = 0

    def racing_read(parts: tuple[str, ...]) -> bytes:
        nonlocal pointer_reads
        if parts[-1] == "current.json":
            pointer_reads += 1
            if pointer_reads == 2:
                _publish(tmp_path, version="0.4.0")
        return read(parts)

    with (
        patch.object(reader, "_read_published_file", side_effect=racing_read),
        pytest.raises(DomainPublicationError, match="changed during admission"),
    ):
        reader.load("plasticos")
    assert reader.load("plasticos").version == "0.4.0"


@pytest.mark.parametrize("filename", ["spec.yaml", "receipt.json"])
def test_hardlinked_revision_files_refused(tmp_path: Path, filename: str) -> None:
    revision, _ = _publish(tmp_path)
    os.link(revision / filename, tmp_path / "outside-link")
    with pytest.raises(DomainPublicationError):
        DomainYamlReader(tmp_path, require_published=True).load("plasticos")


def test_symlink_root_ancestor_refuses_production_constructor(tmp_path: Path) -> None:
    actual = tmp_path / "actual"
    _publish(actual / "domains")
    alias = tmp_path / "alias"
    alias.symlink_to(actual, target_is_directory=True)
    with pytest.raises(DomainPublicationError):
        DomainYamlReader(alias / "domains", require_published=True)


@pytest.mark.parametrize("component", ["spec.yaml", "receipt.json", "current.json"])
def test_nonregular_and_oversized_components_refused(tmp_path: Path, component: str) -> None:
    revision, _ = _publish(tmp_path)
    path = (
        tmp_path / "plasticos/current.json" if component == "current.json" else revision / component
    )
    path.unlink()
    os.mkfifo(path)
    with pytest.raises(DomainPublicationError):
        DomainYamlReader(tmp_path).load("plasticos")
    path.unlink()
    path.write_bytes(b" " * (3 * 1024 * 1024))
    with pytest.raises(DomainPublicationError):
        DomainYamlReader(tmp_path).load("plasticos")


def test_seed_version_never_effective_even_with_fixture_receipt(tmp_path: Path) -> None:
    _publish(tmp_path, version="0.1.0-seed")
    with pytest.raises(DomainPublicationError, match="Seed/draft"):
        DomainYamlReader(tmp_path, require_published=True).load("plasticos")
