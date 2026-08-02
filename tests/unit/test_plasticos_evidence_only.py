"""TASK-020: PlasticOS domain config must not own ranking."""

from __future__ import annotations

from pathlib import Path

import yaml

from app.engines.domain_yaml_reader import DomainYamlReader

ROOT = Path(__file__).resolve().parents[2]
SPEC = ROOT / "domains" / "plasticos" / "spec.yaml"


def _load() -> dict:
    return yaml.safe_load(SPEC.read_text())


def test_no_ranking_dimensions() -> None:
    data = _load()
    dims = (data.get("scoring") or {}).get("dimensions") or []
    assert dims == [], f"expected empty scoring.dimensions, got {dims}"


def test_no_match_gates() -> None:
    data = _load()
    assert data.get("gates") == []


def test_forbidden_ranking_actions() -> None:
    data = _load()
    actions = {
        a["name"] if isinstance(a, dict) else a
        for a in ((data.get("chassisbinding") or {}).get("handleractions") or [])
    }
    forbidden = set((data.get("chassisbinding") or {}).get("forbidden_actions") or [])
    assert "match" not in actions
    assert "outcomes" not in actions
    assert {"match", "outcomes"} <= forbidden


def test_domain_reader_still_loads_ontology() -> None:
    cfg = DomainYamlReader(ROOT / "domains").load("plasticos")
    assert "Facility" in cfg.node_schemas
    assert cfg.node_schemas["Facility"].properties
    assert cfg.version == "0.3.0"
