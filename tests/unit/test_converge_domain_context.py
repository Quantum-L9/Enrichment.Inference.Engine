"""`_load_domain_convergence_context`: node-label default (#238) and rules path base (#242)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from app.engines.domain_yaml_reader import DomainYamlReader, default_node_label
from app.engines.handlers import _load_domain_convergence_context

pytestmark = pytest.mark.unit

_REPO_DOMAINS = Path(__file__).resolve().parents[2] / "domains"


def _plasticos_spec() -> dict:
    return yaml.safe_load((_REPO_DOMAINS / "plasticos" / "spec.yaml").read_text())


async def _load(domains: Path, node_label: str | None) -> tuple[dict, object]:
    with patch("app.engines.handlers._domain_reader", DomainYamlReader(domains)):
        return await _load_domain_convergence_context(domain_id="plasticos", node_label=node_label)


class TestDefaultNodeLabel:
    async def test_unnamed_node_uses_the_specs_only_node(self) -> None:
        """PlasticOS defines only Facility; no label must not look up `Partner`."""
        hints, _rules = await _load(_REPO_DOMAINS, None)

        assert hints["min_confidence"] == pytest.approx(0.75)
        assert hints["priority_fields"][:2] == ["processed_polymers", "contamination_tolerance"]

    async def test_unnamed_node_matches_explicit_facility(self) -> None:
        unnamed, _ = await _load(_REPO_DOMAINS, None)
        named, _ = await _load(_REPO_DOMAINS, "Facility")
        assert unnamed == named != {}

    async def test_a_label_the_spec_does_not_define_yields_no_hints(self) -> None:
        hints, rules = await _load(_REPO_DOMAINS, "Partner")
        assert hints == {}
        assert rules  # rules are domain-wide; only the hints are per node

    async def test_multi_node_spec_without_a_label_has_no_implicit_owner(
        self, tmp_path: Path
    ) -> None:
        spec = _plasticos_spec()
        second = dict(spec["ontology"]["nodes"][0], label="Broker")
        spec["ontology"]["nodes"].append(second)
        (tmp_path / "plasticos").mkdir()
        (tmp_path / "plasticos" / "spec.yaml").write_text(yaml.safe_dump(spec))

        reader = DomainYamlReader(tmp_path)
        assert default_node_label(reader.load("plasticos")) is None
        hints, _ = await _load(tmp_path, None)
        assert hints == {}
        named, _ = await _load(tmp_path, "Broker")
        assert named["min_confidence"] == pytest.approx(0.75)


class TestRelativeRulesPath:
    def _write(self, root: Path) -> list:
        spec = _plasticos_spec()
        rules = spec.pop("inference_rules")
        spec["inference_rules_path"] = "rules.yaml"
        domain_dir = root / "plasticos"
        domain_dir.mkdir(parents=True)
        (domain_dir / "spec.yaml").write_text(yaml.safe_dump(spec))
        (domain_dir / "rules.yaml").write_text(yaml.safe_dump({"inference_rules": rules}))
        return rules

    async def test_relative_path_resolves_against_the_spec_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rules = self._write(tmp_path / "domains")
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        # A decoy beside the working directory must not be what loads.
        (elsewhere / "rules.yaml").write_text(yaml.safe_dump({"inference_rules": []}))
        monkeypatch.chdir(elsewhere)

        _hints, loaded = await _load(tmp_path / "domains", None)

        assert loaded == rules

    async def test_missing_relative_file_names_the_spec_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._write(tmp_path / "domains")
        (tmp_path / "domains" / "plasticos" / "rules.yaml").unlink()
        monkeypatch.chdir(tmp_path)

        _hints, loaded = await _load(tmp_path / "domains", None)

        assert isinstance(loaded, str)  # InferenceBridge refuses it as invalid_rules
        assert str(tmp_path / "domains" / "plasticos" / "rules.yaml") in loaded
