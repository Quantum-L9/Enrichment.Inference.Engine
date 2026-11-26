"""SDK lifespan contract controls, CEG ownership, retired Odoo addon.

L9_META:
  tier: 2
  domain: architecture
  authority: docs/INVARIANTS.md INV-8 (ADR-010); app/bootstrap/l9_contract_runtime.py;
    docs/contracts/dependencies/odoo-crm.yaml

EIE-005. The node app is built by the SDK with an explicit FastAPI lifespan,
and Starlette does not run `@app.on_event("startup")` handlers when a lifespan
is supplied. `install_l9_contract_controls` registers exactly such a handler,
and `app/main.py` does not call it, so the constitution and attestation checks
never run in the real process. The existing tier2 installer test passes on a
standalone FastAPI object, which is the false green. These tests enter the
*real* SDK-created lifespan.

No Gate, Redis, Odoo, PostgreSQL, Docker or network service is used.
"""

from __future__ import annotations

import ast
import importlib
from collections.abc import Callable, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI

pytestmark = pytest.mark.unit
REPO = Path(__file__).resolve().parents[2]


class _ResourceInitReachedError(Exception):
    """Raised by the first EIE resource initializer: startup got past the checks."""


@pytest.fixture
def lifespan_harness(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """The real node app, SDK preflight stubbed, KBResolver as a tripwire."""
    from app import main as eie_main

    sdk_app_module = importlib.import_module("constellation_node_sdk.runtime.app")
    monkeypatch.setattr(sdk_app_module, "run_preflight", lambda _config: None)

    def first_resource(*_args: object, **_kwargs: object) -> None:
        raise _ResourceInitReachedError

    monkeypatch.setattr(eie_main, "KBResolver", first_resource)
    had_state = hasattr(eie_main.app.state, "l9_contract_control")
    yield eie_main
    if not had_state and hasattr(eie_main.app.state, "l9_contract_control"):
        del eie_main.app.state.l9_contract_control


def _patch_verifiers(
    monkeypatch: pytest.MonkeyPatch, make: Callable[[str], Callable[[], tuple[bool, list[str]]]]
) -> None:
    """Patch the verifier owner and every alias of it, whatever the import style."""
    from app import main as eie_main
    from app.bootstrap import l9_contract_runtime as bootstrap
    from scripts import l9_contract_control as checks

    for owner in (checks, bootstrap, eie_main):
        for name in ("verify_constitution", "verify_attestation"):
            if hasattr(owner, name):
                monkeypatch.setattr(owner, name, make(name.removeprefix("verify_")))


@pytest.mark.asyncio
@pytest.mark.parametrize("broken", ["constitution", "attestation"])
async def test_sdk_lifespan_fails_closed_when_contract_control_fails(
    monkeypatch: pytest.MonkeyPatch, lifespan_harness: Any, broken: str
) -> None:
    """EIE-005: an invalid pack stops startup before any EIE resource exists."""
    eie_main = lifespan_harness
    observed: list[str] = []

    def make(name: str) -> Callable[[], tuple[bool, list[str]]]:
        def verify() -> tuple[bool, list[str]]:
            observed.append(name)
            return (False, [f"injected {name} failure"]) if name == broken else (True, [])

        return verify

    _patch_verifiers(monkeypatch, make)

    with pytest.raises(RuntimeError, match=f"injected {broken} failure"):
        async with eie_main.app.router.lifespan_context(eie_main.app):
            pytest.fail("the SDK admitted a runtime with an invalid contract pack")

    expected = ["constitution"] if broken == "constitution" else ["constitution", "attestation"]
    assert observed == expected, "both checks run once, constitution first"
    assert eie_main.app.state.lifecycle_started is False


@pytest.mark.asyncio
async def test_sdk_lifespan_runs_real_checks_and_installs_state_before_resources(
    monkeypatch: pytest.MonkeyPatch, lifespan_harness: Any
) -> None:
    """Positive control: the shipped pack passes, and state is set before KB/Redis/PG.

    Proves wiring the checks in cannot brick a valid deployment, and that
    `/v1/attestation`'s backing state is installed by the real startup path.
    """
    from app.services.runtime_attestation import build_runtime_attestation
    from scripts import l9_contract_control as checks

    eie_main = lifespan_harness
    real = {"constitution": checks.verify_constitution, "attestation": checks.verify_attestation}
    observed: list[tuple[str, bool]] = []

    def make(name: str) -> Callable[[], tuple[bool, list[str]]]:
        def verify() -> tuple[bool, list[str]]:
            ok, errors = real[name]()
            observed.append((name, ok))
            return ok, errors

        return verify

    _patch_verifiers(monkeypatch, make)

    with pytest.raises(_ResourceInitReachedError):
        async with eie_main.app.router.lifespan_context(eie_main.app):
            pytest.fail("KBResolver tripwire did not fire")

    assert observed == [("constitution", True), ("attestation", True)]
    state = getattr(eie_main.app.state, "l9_contract_control", None)
    assert isinstance(state, dict), "contract state must be installed before resources"
    assert state["contract_digest"] == build_runtime_attestation()["contract_digest"]


def test_installer_registers_no_dead_on_event_startup_hook() -> None:
    """A startup hook the SDK lifespan never runs is decoration, not enforcement."""
    from app.bootstrap.l9_contract_runtime import install_l9_contract_controls

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> Any:
        yield

    app = FastAPI(lifespan=lifespan)
    install_l9_contract_controls(app)
    assert app.router.on_startup == [], (
        "install_l9_contract_controls registered an on_event startup handler; "
        "under the SDK's explicit lifespan it never executes"
    )


# ── EIE-007 / INV-8: EIE authors no CEG match gates or scoring ────────────


@pytest.mark.parametrize("fill_rate", [0.75, 1.0])
@pytest.mark.parametrize("value", [7, 7.5, True], ids=["integer", "float", "boolean"])
def test_schema_discovery_proposes_no_ceg_gates_at_any_fill_rate(
    fill_rate: float, value: object
) -> None:
    """INV-8 (CRITICAL) / ADR-010: the gate-proposal branch must be unreachable.

    `convergence/schema_proposer.py` already returns empty gate and scoring
    proposals under ADR-010. `SchemaDiscoveryEngine` still builds range and
    boolean gates for high-fill numeric fields; the live discover action only
    avoids it because it passes no fill statistics.
    """
    from app.engines.schema_discovery import SchemaDiscoveryEngine

    engine = SchemaDiscoveryEngine(current_schema={}, version="0.3.0")
    proposal = engine.analyze(
        enriched_fields={"unseen_field": value},
        inferred_fields={},
        confidence_map={"unseen_field": 0.95},
        batch_stats={"unseen_field": fill_rate},
    )

    assert [p.name for p in proposal.new_properties] == ["unseen_field"]
    assert proposal.proposed_gates == []


@pytest.mark.asyncio
async def test_live_discover_response_carries_no_ceg_gates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The live discover action proposes EIE fields only."""
    from app.engines import handlers
    from app.models.schemas import EnrichResponse

    async def enriched(*_args: object, **_kwargs: object) -> EnrichResponse:
        return EnrichResponse(state="completed", confidence=0.96, fields={"unseen_field": 7})

    monkeypatch.setattr(handlers, "enrich_entity", enriched)
    monkeypatch.setattr(handlers, "get_settings", lambda: object())
    response = await handlers.handle_discover(
        "acme",
        {
            "entity": {"id": "entity-007"},
            "object_type": "Facility",
            "objective": "Discover schema changes",
            "current_schema": {},
            "schema_version": "0.3.0",
        },
    )

    proposal = response["schema_proposal"]
    assert any(p["name"] == "unseen_field" for p in proposal["new_properties"])
    assert proposal["proposed_gates"] == []
    assert "proposed_scoring" not in proposal


@pytest.mark.parametrize(
    "forbidden",
    [
        {"gates": [{"field": "f", "gate_type": "range"}]},
        {"scoring": {"dimensions": [{"name": "owner-drift"}]}},
        {"ranking": [{"name": "owner-drift"}]},
    ],
    ids=["gates", "scoring", "ranking"],
)
def test_domain_spec_validator_rejects_ceg_owned_keys(forbidden: dict[str, Any]) -> None:
    from app.engines.domain_spec_schema import _reject_ceg

    with pytest.raises(ValueError):
        _reject_ceg(forbidden)


# ── EIE-006: the EIE Odoo addon is retired, not a second installable owner ─


def test_retired_eie_odoo_addon_cannot_be_installable() -> None:
    """The live Odoo consumer is IB-Odoo_19/plasticos_enrichment via plasticos_gate.

    docs/contracts/dependencies/odoo-crm.yaml already records that this addon is
    not the live producer and is not installed. Physical removal is fine; if the
    source stays for reference it must not claim installability. The fix is NOT
    to fabricate the missing view, ACL and cron files.
    """
    addon = REPO / "odoo_modules/plasticos_research_enrichment"
    if not addon.exists():
        return

    manifest_path = addon / "__manifest__.py"
    assert manifest_path.is_file(), "a remaining addon must keep a readable manifest"
    source = ast.parse(manifest_path.read_text(encoding="utf-8"))
    assert len(source.body) == 1 and isinstance(source.body[0], ast.Expr)
    manifest = ast.literal_eval(source.body[0].value)
    assert isinstance(manifest, dict)
    assert manifest.get("installable") is False, (
        "EIE does not own an installable PlasticOS addon; retire it, do not rebuild its assets"
    )
