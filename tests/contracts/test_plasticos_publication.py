"""The checked-in `plasticos` spec is published, and a prod node serving it is Ready.

A tenant-serving environment (`L9_ENVIRONMENT` outside local/dev/test) builds
`DomainYamlReader(settings.domains_dir, require_published=True)` in
`app.main.EnrichmentLifecycle.startup`. That reader ignores the authoring copy
`domains/plasticos/spec.yaml` and admits only
`domains/plasticos/current.json` -> `revisions/<sha256>/{spec.yaml,receipt.json}`,
the store written by `python -m scripts.domain_spec_operator publish`
(docs/runbooks/native-domain-publication.md).

Because the reader never looks at the authoring copy once a pointer exists, an
edit to `domains/plasticos/spec.yaml` that is not republished would be served
nowhere. `test_authoring_spec_is_the_published_revision` is the guard for that.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from app.engines.domain_yaml_reader import DomainPublicationError, DomainYamlReader

REPO_ROOT = Path(__file__).resolve().parents[2]
DOMAINS_ROOT = REPO_ROOT / "domains"
TENANT = "plasticos"
TENANT_DIR = DOMAINS_ROOT / TENANT

REPUBLISH = (
    "bump `version` in domains/plasticos/spec.yaml (a retained version cannot be "
    "rebound to new bytes), then run `python -m scripts.domain_spec_operator publish "
    "--root domains --tenant plasticos --spec domains/plasticos/spec.yaml "
    "--reviewed-input-sha256 <sha256 of the new spec.yaml> --reviewed-by <named reviewer> "
    "--approval-ref <review evidence> --expected-previous-sha256 <sha256 in current.json>` "
    "and commit domains/plasticos/ (docs/runbooks/native-domain-publication.md)"
)


def _pointer_sha() -> str:
    pointer = json.loads((TENANT_DIR / "current.json").read_text(encoding="utf-8"))
    assert pointer["schema"] == "eie.domain-current/v1"
    return str(pointer["sha256"])


@pytest.mark.unit
def test_authoring_spec_is_the_published_revision() -> None:
    """Fails when `domains/plasticos/spec.yaml` changes without republication."""
    authoring = (TENANT_DIR / "spec.yaml").read_bytes()
    sha = _pointer_sha()
    assert hashlib.sha256(authoring).hexdigest() == sha, (
        "domains/plasticos/spec.yaml differs from the published revision that "
        f"current.json names; production would keep serving the old bytes. To republish: {REPUBLISH}"
    )
    assert (TENANT_DIR / "revisions" / sha / "spec.yaml").read_bytes() == authoring


@pytest.mark.unit
def test_plasticos_loads_through_the_published_reader() -> None:
    """The reader a tenant-serving node builds admits the checked-in revision."""
    sha = _pointer_sha()
    receipt = json.loads(
        (TENANT_DIR / "revisions" / sha / "receipt.json").read_text(encoding="utf-8")
    )

    config = DomainYamlReader(DOMAINS_ROOT, require_published=True).load(TENANT)

    assert config.domain_id == TENANT
    assert config.source_sha256 == sha
    assert config.approval_ref == receipt["approval_ref"]
    assert config.version == receipt["version"] == config.raw_spec["version"]
    assert config.node_schemas


@pytest.mark.unit
def test_unpublished_copy_of_the_same_spec_is_refused(tmp_path: Path) -> None:
    """Control: the same bytes without pointer and receipt do not load."""
    (tmp_path / TENANT).mkdir()
    shutil.copyfile(TENANT_DIR / "spec.yaml", tmp_path / TENANT / "spec.yaml")

    with pytest.raises(DomainPublicationError):
        DomainYamlReader(tmp_path, require_published=True).load(TENANT)


# Runs in a fresh interpreter: `app.main` builds the SDK app and its
# NodeRuntimeConfig at import time, so prod mode cannot be entered honestly by
# patching an already-imported module.
_PROBE = """
import json
import sys
from fastapi.testclient import TestClient
from app import main
from app.api.v1 import converge
from app.core.config import get_settings

out = {"environment": get_settings().l9_environment}
with TestClient(main.app) as client:
    specs = converge._domain_specs
    out["registry_state"] = main._domain_registry_state
    out["registry_type"] = type(specs).__name__
    out["tenants"] = sorted(specs)
    reader = getattr(specs, "reader", None)
    out["require_published"] = getattr(reader, "require_published", None)
    out["source_sha256"] = reader.load("plasticos").source_sha256 if out["tenants"] else None
    for name, path in (("eie_ready", main.READINESS_ENDPOINT), ("sdk_ready", "/v1/ready")):
        response = client.get(path)
        out[name] = {"status": response.status_code, "body": response.json()}
sys.stdout.write("PROBE " + json.dumps(out) + "\\n")
"""


def _prod_probe(domains_dir: Path) -> dict[str, Any]:
    """Start the real app under L9_ENVIRONMENT=prod and read both readiness routes.

    Real: module import of `app.main`, `_build_runtime_config`, the SDK lifespan
    with `run_preflight` (signature required), `EnrichmentLifecycle.startup`,
    the `require_published` reader over `domains_dir`, `_load_domain_registry`,
    `_hydrate_domain_registry` and both route functions.

    Substituted: Gate registration is switched off (`GATE_REGISTRATION_ENABLED`
    false), so the Gate half of readiness reads `disabled`, not `registered`;
    the signing key is a test literal; no Redis, PostgreSQL or Gate is running
    (startup opens no connection to them, and the native registry replays no
    PostgreSQL rows); OpenTelemetry export is disabled (`OTEL_SDK_DISABLED`), which
    only spares the exporter's shutdown wait on an unreachable collector.
    """
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(REPO_ROOT),
        "L9_ENVIRONMENT": "prod",
        "L9_REQUIRE_SIGNATURE": "true",
        "L9_SIGNING_KEY": "test-only-not-a-secret",
        "L9_SIGNING_KEY_ID": "test",
        "GATE_REGISTRATION_ENABLED": "false",
        "DOMAINS_DIR": str(domains_dir),
        "OTEL_SDK_DISABLED": "true",
    }
    completed = subprocess.run(  # noqa: S603 — fixed argv, our own interpreter
        [sys.executable, "-c", _PROBE],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    lines = [line for line in completed.stdout.splitlines() if line.startswith("PROBE ")]
    assert completed.returncode == 0 and lines, completed.stderr[-4000:]
    result: dict[str, Any] = json.loads(lines[-1].removeprefix("PROBE "))
    return result


@pytest.mark.unit
def test_prod_node_with_the_published_spec_is_ready() -> None:
    """`GET /api/v1/ready` (EIE, registry-dependent) and SDK `GET /v1/ready` are 200.

    Only `/api/v1/ready` depends on the domain registry. The SDK's `/v1/ready`
    reflects lifecycle start and SDK Gate participation, which EIE disables
    (`auto_register_with_gate=False`); it is asserted for completeness and would
    stay 200 with no domain at all (see the control test below).
    """
    result = _prod_probe(DOMAINS_ROOT)

    assert result["environment"] == "prod"
    assert result["require_published"] is True
    assert result["registry_type"] == "EffectiveDomainRegistry"
    assert result["registry_state"] == "loaded"
    assert result["tenants"] == [TENANT]
    assert result["source_sha256"] == _pointer_sha()
    assert result["eie_ready"]["status"] == 200
    assert result["eie_ready"]["body"]["ready"] is True
    assert result["eie_ready"]["body"]["gate_registration"] == "disabled"
    assert result["sdk_ready"]["status"] == 200
    assert result["sdk_ready"]["body"]["ready"] is True


@pytest.mark.unit
def test_prod_node_with_an_unpublished_spec_is_not_ready(tmp_path: Path) -> None:
    """Control: identical startup, same spec bytes, no publication -> 503."""
    (tmp_path / TENANT).mkdir()
    shutil.copyfile(TENANT_DIR / "spec.yaml", tmp_path / TENANT / "spec.yaml")

    result = _prod_probe(tmp_path)

    assert result["environment"] == "prod"
    assert result["tenants"] == []
    assert result["registry_state"] in {"empty", "failed"}
    assert result["eie_ready"]["status"] == 503
    assert result["eie_ready"]["body"]["ready"] is False
