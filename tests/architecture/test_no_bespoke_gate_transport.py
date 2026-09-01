"""Architecture guards: Gate registration and transport belong to the Gate_SDK.

EIE owns enrichment. The SDK owns the registration call and the /v1/execute
worker runtime. These guards fail if EIE production code grows its own copy of
either — the drift that put a hand-rolled ``POST /v1/admin/register`` with its
own retry loop and status taxonomy in ``app/services/gate_registration.py``.

Scope is EIE production code only (``app/``). The SDK is *expected* to contain
these constructs; so are these tests, so ``tests/`` is excluded too.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[2] / "app"

PRODUCTION_FILES = sorted(APP.rglob("*.py"))

# Deprecated compatibility artifacts kept for import-compat only; they are not
# the live transport bundle (see CLAUDE.md "Transport Precision").
_EXCLUDED = {"chassis/router.py", "chassis/envelope.py", "chassis/registry.py"}


def _sources() -> list[tuple[Path, str]]:
    out = []
    for path in PRODUCTION_FILES:
        rel = path.relative_to(APP.parent).as_posix()
        if any(rel.endswith(suffix) for suffix in _EXCLUDED):
            continue
        out.append((path, path.read_text(encoding="utf-8")))
    return out


def _rel(path: Path) -> str:
    return path.relative_to(APP.parent).as_posix()


def test_no_gate_admin_register_endpoint_in_production_code():
    """No EIE module may name Gate's admin-registration endpoint."""
    offenders = [_rel(p) for p, src in _sources() if "/v1/admin/register" in src]
    assert offenders == [], (
        "Gate registration transport belongs to Gate_SDK register_node(). "
        f"EIE production code references /v1/admin/register in: {offenders}"
    )


def test_no_admin_token_header_construction_in_production_code():
    """The X-Admin-Token header is the SDK's to build, not EIE's."""
    offenders = [
        _rel(p) for p, src in _sources() if re.search(r"X-Admin-Token", src, re.IGNORECASE)
    ]
    assert offenders == [], (
        "Registration auth headers are constructed by Gate_SDK register_node(). "
        f"Found X-Admin-Token construction in: {offenders}"
    )


def test_no_registration_module_reintroduced():
    """`app/services/gate_registration.py` stays deleted, not renamed back."""
    assert not (APP / "services" / "gate_registration.py").exists(), (
        "app/services/gate_registration.py was deleted when registration moved to "
        "Gate_SDK register_node(); reintroducing it re-forks the control plane."
    )


def test_registration_uses_sdk_register_node():
    """The one registration call site is the SDK's, and it is actually wired."""
    main_src = (APP / "main.py").read_text(encoding="utf-8")
    assert "register_node" in main_src, "app/main.py must call Gate_SDK register_node()"
    tree = ast.parse(main_src)
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "constellation_node_sdk"
        for alias in node.names
    }
    assert {"NodeRegistration", "register_node"} <= imported, (
        "NodeRegistration and register_node must come from constellation_node_sdk, "
        f"got {sorted(imported)}"
    )


@pytest.mark.parametrize(
    ("pattern", "what"),
    [
        (r"class\s+\w*TransportPacket\w*\s*\(", "a TransportPacket model"),
        (r"def\s+\w*(validate|verify)_transport_packet\w*\s*\(", "transport validation"),
        (r"def\s+\w*sign_transport_packet\w*\s*\(", "transport signing"),
        (r"def\s+\w*compute_transport_hash\w*\s*\(", "transport hashing"),
        (r"@(app|router)\.post\(\s*[\"']/v1/execute[\"']", "a /v1/execute server route"),
    ],
)
def test_no_locally_implemented_transport(pattern: str, what: str):
    """EIE owns handlers; the SDK owns the transport runtime (C-21)."""
    offenders = [_rel(p) for p, src in _sources() if re.search(pattern, src)]
    assert offenders == [], (
        f"EIE production code must not implement {what} — that is Gate_SDK's "
        f"create_node_app() runtime. Found in: {offenders}"
    )
