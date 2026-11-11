"""
Waterfall Provider Order Contract (TODO-05)
Source: app/services/enrichment/waterfall_engine.py, config/waterfall_config.yaml
Contract: docs/contracts/dependencies/_index.yaml `waterfall`
Markers: unit

The documented order is only worth having if it is the order the engine runs.
These tests rebuild it from the shipped config through the engine's own
`_get_source_order` and fail when the contract and the code disagree.
"""

from __future__ import annotations

import pytest

from app.services.enrichment.sources import SOURCE_REGISTRY
from app.services.enrichment.waterfall_engine import WaterfallEngine
from tests.contracts.conftest_contracts import DEPS_DIR, REPO_ROOT, load_yaml

WATERFALL_CONFIG = REPO_ROOT / "config" / "waterfall_config.yaml"


@pytest.fixture(scope="module")
def documented() -> dict:
    index = load_yaml(DEPS_DIR / "_index.yaml")
    waterfall = index.get("waterfall")
    assert isinstance(waterfall, dict), "dependencies/_index.yaml has no `waterfall` section"
    return waterfall


@pytest.fixture(scope="module")
def engine() -> WaterfallEngine:
    return WaterfallEngine(sources_config_path=str(WATERFALL_CONFIG))


@pytest.mark.unit
def test_documented_domains_match_config(documented: dict, engine: WaterfallEngine) -> None:
    assert set(documented["strategies"]) == set(engine._waterfall_cfg)


@pytest.mark.unit
def test_documented_order_is_engine_order(documented: dict, engine: WaterfallEngine) -> None:
    for domain, strategy in documented["strategies"].items():
        steps = strategy["order"]
        assert [s["sequence"] for s in steps] == list(range(1, len(steps) + 1)), domain
        expected = engine._get_source_order(domain, engine._waterfall_cfg[domain])
        assert [s["source"] for s in steps] == expected, domain


@pytest.mark.unit
def test_documented_stop_parameters_match_config(documented: dict, engine: WaterfallEngine) -> None:
    for domain, strategy in documented["strategies"].items():
        config = engine._waterfall_cfg[domain]
        assert strategy["max_attempts"] == config["max_attempts"], domain
        assert strategy["quality_threshold"] == config["quality_threshold"], domain


@pytest.mark.unit
def test_unregistered_sources_are_flagged(documented: dict) -> None:
    """A listed source the engine cannot register must say so, not look runnable."""
    declared = {d["name"] for d in load_yaml(DEPS_DIR / "_index.yaml")["dependencies"]}
    for domain, strategy in documented["strategies"].items():
        for step in strategy["order"]:
            if step["source"] not in SOURCE_REGISTRY:
                assert step.get("dependency") is None, (domain, step)
                assert "not in SOURCE_REGISTRY" in step.get("note", ""), (domain, step)
            else:
                assert step.get("dependency") in declared, (domain, step)
