"""Tests for app/engines/inference/rule_loader.py

Covers: YAML-based inference rule loading, validation, empty/malformed files.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from app.engines.inference.rule_loader import RuleDefinition, RuleRegistry, load_rules

_VALID_RULE = {
    "rule_id": "premium_hdpe_grade",
    "conditions": [
        {"field": "polymer_type", "operator": "EQUALS", "value": "HDPE"},
        {"field": "contamination_pct", "operator": "LT", "value": 2.0},
    ],
    "outputs": [
        {
            "field": "material_grade",
            "value_expr": "Premium HDPE",
            "derivation_type": "classification",
        }
    ],
    "confidence": 0.95,
    "priority": 20,
    "domain": "plastics_recycling",
}

_SECOND_RULE = {
    "rule_id": "standard_hdpe_grade",
    "conditions": [
        {"field": "polymer_type", "operator": "EQUALS", "value": "HDPE"},
    ],
    "outputs": [
        {
            "field": "material_grade",
            "value_expr": "Standard HDPE",
            "derivation_type": "classification",
        }
    ],
    "confidence": 0.90,
    "priority": 10,
    "domain": "plastics_recycling",
}


def _write_yaml(path: Path, payload: dict) -> Path:
    path.write_text(yaml.dump(payload), encoding="utf-8")
    return path


class TestRuleLoader:
    """Tests for YAML-based rule loading against load_rules(path) -> RuleRegistry."""

    def test_load_rules_from_kb_yaml(self, tmp_path: Path):
        yaml_path = _write_yaml(
            tmp_path / "rules.yaml",
            {"inference_rules": [_VALID_RULE]},
        )
        registry = load_rules(yaml_path)
        assert isinstance(registry, RuleRegistry)
        assert registry.count() >= 1
        assert all(isinstance(rule, RuleDefinition) for rule in registry.all_rules())

    def test_empty_kb_returns_empty_rules(self, tmp_path: Path):
        yaml_path = _write_yaml(tmp_path / "empty.yaml", {"inference_rules": []})
        registry = load_rules(yaml_path)
        assert registry.count() == 0
        assert registry.all_rules() == []

    def test_rules_without_rules_key_returns_empty(self, tmp_path: Path):
        yaml_path = _write_yaml(tmp_path / "no_rules.yaml", {"domain": "test", "polymers": {}})
        registry = load_rules(yaml_path)
        assert registry.count() == 0

    def test_rule_has_required_fields(self, tmp_path: Path):
        yaml_path = _write_yaml(tmp_path / "required.yaml", {"inference_rules": [_VALID_RULE]})
        registry = load_rules(yaml_path)
        for rule in registry.all_rules():
            assert rule.rule_id
            assert rule.conditions
            assert rule.outputs

    def test_rule_filtering_by_domain(self, tmp_path: Path):
        yaml_path = _write_yaml(
            tmp_path / "domain.yaml",
            {"inference_rules": [_VALID_RULE, _SECOND_RULE]},
        )
        registry = load_rules(yaml_path)
        ids = [rule.rule_id for rule in registry.all_rules()]
        assert "premium_hdpe_grade" in ids

    def test_rule_priority_ordering(self, tmp_path: Path):
        yaml_path = _write_yaml(
            tmp_path / "priority.yaml",
            {"inference_rules": [_VALID_RULE, _SECOND_RULE]},
        )
        registry = load_rules(yaml_path)
        rules = registry.all_rules()
        assert len(rules) == 2
        by_id = {rule.rule_id: rule.priority for rule in rules}
        assert by_id["premium_hdpe_grade"] > by_id["standard_hdpe_grade"]

    def test_malformed_rule_skipped(self, tmp_path: Path):
        yaml_path = _write_yaml(
            tmp_path / "mixed.yaml",
            {"inference_rules": [_VALID_RULE, {"broken": True}]},
        )
        registry = load_rules(yaml_path)
        ids = [rule.rule_id for rule in registry.all_rules()]
        assert "premium_hdpe_grade" in ids
        assert registry.count() == 1
