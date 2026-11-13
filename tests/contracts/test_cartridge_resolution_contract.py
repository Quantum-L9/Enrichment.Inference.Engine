"""Cartridge resolution at the app boundary names no industry (Campaign A, lane A1).

Generic runtime code used to carry a built-in cartridge: `Settings.default_domain`
defaulted to `plasticos`, so any request that named no domain was recorded as a
PlasticOS one whatever tenant sent it. Resolution is now explicit: the request's
cartridge, else the operator's configured default, else the tenant's own.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from app.core.config import Settings
from app.engines.domain_yaml_reader import resolve_cartridge_id

REPO = Path(__file__).resolve().parents[2]
# Generic runtime modules on the converge path. Cartridge content lives under
# domains/; these files must never name one.
GENERIC_RUNTIME = (
    "app/core/config.py",
    "app/engines/handlers.py",
    "app/engines/domain_yaml_reader.py",
    "app/engines/convergence_controller.py",
)
INDUSTRY_NAMES = ("plasticos", "plastics")


@pytest.mark.unit
@pytest.mark.parametrize(
    ("requested", "configured", "expected"),
    [
        ("freight-carriers", "plasticos", "freight-carriers"),
        (None, "plasticos", "plasticos"),
        ("", "", "tenant-a"),
        (None, None, "tenant-a"),
        (42, ["x"], "tenant-a"),
    ],
)
def test_resolution_order(requested: Any, configured: Any, expected: str) -> None:
    resolved = resolve_cartridge_id("tenant-a", requested=requested, configured_default=configured)
    assert resolved == expected


@pytest.mark.unit
def test_no_cartridge_is_built_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEFAULT_DOMAIN", raising=False)
    assert Settings(_env_file=None).default_domain == ""


@pytest.mark.unit
@pytest.mark.parametrize("module", GENERIC_RUNTIME)
def test_generic_runtime_code_names_no_industry(module: str) -> None:
    """No string a generic runtime module can act on names an industry.

    Docstrings are prose, not behavior, and are exempt; every other string
    constant is checked.
    """
    tree = ast.parse((REPO / module).read_text(encoding="utf-8"))
    prose = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }
    offenders = [
        (node.lineno, node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in prose
        and any(name in node.value.lower() for name in INDUSTRY_NAMES)
    ]
    assert offenders == []


requires_app_runtime = pytest.mark.skipif(
    importlib.util.find_spec("perplexity") is None,
    reason="app.engines.handlers needs the provider runtime; not installed in the contract gate env",
)


@requires_app_runtime
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload_domain", "configured", "expected"),
    [(None, "", "tenant-a"), (None, "pinned", "pinned"), ("named", "pinned", "named")],
)
async def test_persistence_records_the_explicitly_resolved_cartridge(
    payload_domain: str | None, configured: str, expected: str
) -> None:
    from app.engines import handlers

    payload: dict[str, Any] = {"entity": {"id": "e:1"}}
    if payload_domain:
        payload["domain"] = payload_domain
    settings = Settings(_env_file=None, default_domain=configured)
    with (
        patch("app.engines.handlers.get_settings", return_value=settings),
        patch("app.services.side_effect_coordinator.get_side_effect_coordinator") as coordinator,
    ):
        coordinator.return_value.commit_after_enrich = AsyncMock()
        await handlers._persist_and_sync("tenant-a", payload, {"state": "completed"}, "Account")

    assert coordinator.return_value.commit_after_enrich.await_args.kwargs["domain"] == expected


@requires_app_runtime
@pytest.mark.asyncio
async def test_simulate_assumes_no_cartridge() -> None:
    from app.engines import handlers

    with pytest.raises(ValueError, match="domain_id is required"):
        await handlers.handle_simulate(
            "tenant-a", {"crm_field_names": ["name"], "customer_name": "Acme"}
        )
