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


# ---------------------------------------------------------------------------
# Seam guards (2026-09-02 forensic audit): EIE talks to peers only via Gate.
# ---------------------------------------------------------------------------

ROOT = APP.parent
CHASSIS = ROOT / "chassis"

_RETIRED_SIDE_DOORS = (
    "chassis/node_client.py",
    "app/services/graph_sync_hooks.py",
    "app/services/workers/graph_inference_consumer.py",
)

_RETIRED_PEER_SETTINGS = (
    "ceg_base_url",
    "graph_node_url",
    "score_node_url",
    "route_node_url",
    "inter_node_secret",
)


def _production_sources() -> list[tuple[Path, str]]:
    out = list(_sources())
    for path in sorted(CHASSIS.rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        if rel in _EXCLUDED:
            continue
        out.append((path, path.read_text(encoding="utf-8")))
    return out


@pytest.mark.parametrize("relative_path", _RETIRED_SIDE_DOORS)
def test_retired_side_door_modules_stay_deleted(relative_path: str):
    assert not (ROOT / relative_path).exists(), (
        f"{relative_path} was removed as a Gate-bypassing side door; reintroducing it "
        "restores node-to-node traffic outside Gate authority (SEAM-002)."
    )


def test_settings_carry_no_peer_url_or_shared_peer_secret():
    """No peer awareness: EIE knows GATE_URL and nothing about CEG's address."""
    src = (APP / "core" / "config.py").read_text(encoding="utf-8")
    offenders = [name for name in _RETIRED_PEER_SETTINGS if re.search(rf"\b{name}\b", src)]
    assert offenders == [], f"peer URL / shared-secret settings reintroduced: {offenders}"


def test_no_peer_ingress_routes_outside_sdk_runtime():
    """/v1/outcomes (and any other bespoke POST route) is not an EIE peer ingress."""
    offenders = [
        _rel(p)
        for p, src in _sources()
        if (
            ("/v1/outcomes" in src and not src.lstrip().startswith('"""'))
            or re.search(r"@router\.post\(\s*[\"']/v1/(outcomes|sync|match|execute)", src)
        )
    ]
    assert offenders == [], f"peer ingress routes outside the SDK runtime: {offenders}"


def test_every_authored_transport_packet_is_addressed_to_gate():
    """Every create_transport_packet(...) call in EIE production code targets 'gate'."""
    offenders: list[str] = []
    for path, src in _production_sources():
        if "create_transport_packet" not in src:
            continue
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and getattr(node.func, "id", getattr(node.func, "attr", ""))
                == "create_transport_packet"
            ):
                continue
            dest = next((kw.value for kw in node.keywords if kw.arg == "destination_node"), None)
            if dest is None or not (isinstance(dest, ast.Constant) and dest.value == "gate"):
                offenders.append(f"{_rel(path)}:{node.lineno}")
    assert offenders == [], f"TransportPackets addressed to something other than Gate: {offenders}"


def test_no_raw_http_post_to_execute_outside_sdk():
    """Outbound /v1/execute traffic is the SDK's GateClient, never a hand-rolled httpx call."""
    offenders = [
        _rel(p)
        for p, src in _production_sources()
        if re.search(r"httpx\.(Async)?Client|requests\.(post|Session)", src)
        and re.search(r"[\"']/v1/execute[\"']", src)
    ]
    assert offenders == [], f"raw HTTP transport to /v1/execute: {offenders}"


def test_outbound_gate_config_has_a_single_factory():
    """GateClientConfig(...) is constructed in exactly one production module."""
    sites = sorted(
        {_rel(p) for p, src in _production_sources() if re.search(r"\bGateClientConfig\s*\(", src)}
    )
    assert sites == ["app/services/gate_client.py"], (
        "Outbound Gate configuration (URL, identity, signing) must be built only by "
        f"app/services/gate_client.py; found GateClientConfig(...) in {sites}"
    )
