"""WU-29: the audit gate passes a compliant tree and still fails real violations.

D-6 (owner decision): FastAPI in the four explicitly chassis-tagged adapter
modules is not a CRITICAL; FastAPI in any other engine module still is.
F-118: the pre-commit audit hook can fail.
"""

from __future__ import annotations

import shlex
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from tools import audit_engine
from tools.audit_engine import CHASSIS_ADAPTER_MODULES, AuditResult, check_imports

REPO_ROOT = Path(__file__).parent.parent.parent

_FASTAPI_SOURCE = "from fastapi import APIRouter\n\nrouter = APIRouter()\n"
_TAGGED = "# L9-layer: chassis\n" + _FASTAPI_SOURCE


def _rule11(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rel: str, source: str) -> list:
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source)
    monkeypatch.setattr(audit_engine, "REPO_ROOT", tmp_path)
    result = AuditResult()
    check_imports([path], result)
    return [f for f in result.findings if f.rule == 11]


@pytest.mark.parametrize("rel", sorted(CHASSIS_ADAPTER_MODULES))
def test_tagged_chassis_adapter_may_import_fastapi(
    rel: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _rule11(tmp_path, monkeypatch, rel, _TAGGED) == []


def test_fastapi_in_an_engine_module_is_still_critical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    findings = _rule11(tmp_path, monkeypatch, "app/engines/scoring.py", _FASTAPI_SOURCE)
    assert [f.severity for f in findings] == ["CRITICAL"]


def test_chassis_tag_alone_does_not_exempt_an_engine_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    findings = _rule11(tmp_path, monkeypatch, "app/engines/scoring.py", _TAGGED)
    assert [f.severity for f in findings] == ["CRITICAL"]


def test_listed_adapter_without_its_tag_is_not_exempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    findings = _rule11(tmp_path, monkeypatch, "app/core/auth.py", _FASTAPI_SOURCE)
    assert [f.severity for f in findings] == ["CRITICAL"]


def test_plain_import_fastapi_is_also_caught(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    findings = _rule11(tmp_path, monkeypatch, "app/services/x.py", "import fastapi\n")
    assert [f.severity for f in findings] == ["CRITICAL"]


def test_every_listed_adapter_carries_its_chassis_tag() -> None:
    for rel in CHASSIS_ADAPTER_MODULES:
        source = (REPO_ROOT / rel).read_text()
        assert audit_engine.is_chassis_adapter(Path(rel), source), rel


def test_audit_strict_passes_the_current_tree() -> None:
    proc = subprocess.run(
        [sys.executable, "tools/audit_engine.py", "--strict"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout[-2000:]


def _audit_hook() -> dict:
    config = yaml.safe_load((REPO_ROOT / ".pre-commit-config.yaml").read_text())
    for repo in config["repos"]:
        for hook in repo.get("hooks", []):
            if "audit_engine.py" in str(hook.get("entry", "")):
                return hook
    raise AssertionError("no audit_engine pre-commit hook")


def test_precommit_audit_hook_runs_strict() -> None:
    entry = str(_audit_hook()["entry"])
    assert "--strict" in entry
    # A missing tool must fail the hook, not skip it silently.
    assert "if [ -f" not in entry


def test_precommit_audit_hook_fails_on_a_real_critical(tmp_path: Path) -> None:
    """Negative control (KP-011): the hook's command exits non-zero on a CRITICAL."""
    entry = str(_audit_hook()["entry"])
    app = tmp_path / "app" / "engines"
    app.mkdir(parents=True)
    (app / "leak.py").write_text(_FASTAPI_SOURCE)
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "audit_engine.py").write_text(
        (REPO_ROOT / "tools" / "audit_engine.py").read_text()
    )
    argv = shlex.split(entry)
    assert argv[0] == "python", entry
    proc = subprocess.run(
        [sys.executable, *argv[1:]],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode != 0, proc.stdout[-2000:]
    assert "chassis isolation violation" in proc.stdout
