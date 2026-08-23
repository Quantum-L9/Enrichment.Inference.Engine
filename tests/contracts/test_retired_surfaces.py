"""
Retired Contract-Bound Surfaces
Source: this PR — five superseded modules removed from app/.
Markers: unit

Deleting a module under a contract-bound prefix (``app/api/v1/``,
``app/engines/``, ``app/services/``) is itself a contract change: the surface
stops existing. Nothing else in the contract corpus records that, because none
of these five was ever named in ``docs/contracts/`` — they were superseded
in-tree and left behind.

This file is the record. Each entry pairs a retired path with the live surface
that replaced it, so a future reader finds the replacement instead of
resurrecting the original, and so re-adding a path silently fails CI.

These are NOT deprecation shims. Nothing imports them and nothing may.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.parent

# retired path -> the live surface that supersedes it
RETIRED_SURFACES = {
    "app/services/score/scorer.py": (
        "app/score/score_engine.py. The retired module also defined a "
        "ScoreDimension(BaseModel) that shadowed the live "
        "ScoreDimension(StrEnum) in app/score/score_models.py."
    ),
    "app/services/score/__init__.py": (
        "app/score/. The package held only scorer.py and was itself empty."
    ),
    "app/services/convergence_helpers.py": (
        "confidence extraction inlined at app/services/convergence_controller.py; "
        "return channel owned by app/services/graph_return_channel.py; "
        "proposals by app/engines/convergence/schema_proposer.py."
    ),
    "app/api/v1/intake.py": (
        "app/api/v1/converge.py and app/api/v1/discover.py. The retired module "
        "declared no APIRouter and was never mountable."
    ),
    "app/engines/inference_bridge.py": (
        "app/engines/inference_bridge_v2.py via inference_bridge_adapter.py, "
        "whose docstring names this deletion as its own step 4."
    ),
    "app/engines/inference_unlock_scorer.py": (
        "app/engines/inference/rule_loader.py — build_unlock_index, "
        "score_unlock_potential, rank_fields_by_unlock."
    ),
}


@pytest.mark.parametrize(("path", "replacement"), sorted(RETIRED_SURFACES.items()))
def test_retired_surface_stays_retired(path: str, replacement: str) -> None:
    assert not (REPO_ROOT / path).exists(), (
        f"{path} was retired and must not come back. Use instead: {replacement}"
    )


@pytest.mark.parametrize(("path", "replacement"), sorted(RETIRED_SURFACES.items()))
def test_replacement_is_named(path: str, replacement: str) -> None:
    """An entry without a real replacement is a note, not a contract."""
    assert len(replacement) > 30, f"{path} must name the surface that supersedes it"


def test_no_module_imports_a_retired_surface() -> None:
    """A retired path must not be referenced from app/ or tests/, even as a string."""
    dotted = {
        path.removesuffix(".py").removesuffix("/__init__").replace("/", ".")
        for path in RETIRED_SURFACES
    }
    this_file = Path(__file__).resolve()

    offenders: list[str] = []
    for source in (REPO_ROOT / "app", REPO_ROOT / "tests"):
        for py in source.rglob("*.py"):
            if "__pycache__" in py.parts or py.resolve() == this_file:
                continue
            text = py.read_text(encoding="utf-8", errors="ignore")
            for module in dotted:
                # `import x.y.z` / `from x.y.z import ...` — not a bare substring,
                # so app.engines.inference_bridge_v2 is not matched by
                # app.engines.inference_bridge.
                if f"import {module}\n" in text or f"from {module} import" in text:
                    offenders.append(f"{py.relative_to(REPO_ROOT)} -> {module}")

    assert offenders == [], "Retired surfaces are still imported:\n  " + "\n  ".join(offenders)
