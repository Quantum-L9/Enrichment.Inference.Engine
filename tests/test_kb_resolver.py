"""Tests for app/services/kb_resolver.py

Covers: YAML KB loading, fragment resolve, empty-domain safety, caching.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from app.services.kb_resolver import KBResolver


def _write_kb(kb_dir: Path, name: str, payload: dict) -> None:
    (kb_dir / f"{name}.yaml").write_text(yaml.dump(payload), encoding="utf-8")


@pytest.fixture
def kb_dir(tmp_path: Path) -> Path:
    _write_kb(
        tmp_path,
        "hdpe",
        {
            "metadata": {"polymertype": "HDPE", "version": "1.2.0"},
            "materialgrades": [
                {
                    "gradeid": "hdpe-premium",
                    "description": "Premium HDPE bottle flake for food-contact uses",
                }
            ],
            "inferencerules": [
                {
                    "ruleid": "premium_hdpe_grade",
                    "confidence": 0.95,
                    "conclusion": "Premium HDPE when contamination is low",
                }
            ],
        },
    )
    return tmp_path


class TestKBResolver:
    """Tests for domain KB injection against the live KBResolver(kb_dir) API."""

    @pytest.fixture
    def resolver(self, kb_dir: Path) -> KBResolver:
        return KBResolver(kb_dir)

    def test_load_returns_domain_spec(self, resolver: KBResolver):
        assert resolver.index.is_loaded is True
        assert "HDPE" in resolver.index.raw
        spec = resolver.index.raw["HDPE"]
        assert spec["metadata"]["polymertype"] == "HDPE"

    def test_load_returns_none_for_unknown_domain(self, resolver: KBResolver):
        result = resolver.resolve(kb_context="nonexistent_domain")
        assert result["context_text"] == ""
        assert result["fragment_ids"] == []

    def test_resolve_matches_entity_polymer(self, resolver: KBResolver):
        fragments = resolver.resolve(kb_context="HDPE", entity={"polymer": "HDPE"})
        assert fragments is not None
        assert fragments["fragment_ids"]

    def test_resolve_empty_entity(self, resolver: KBResolver):
        fragments = resolver.resolve(kb_context="HDPE", entity={})
        assert fragments is not None
        assert "context_text" in fragments

    def test_kb_fragment_id_generation(self, resolver: KBResolver):
        fragments = resolver.resolve(kb_context="HDPE", entity={"polymer": "HDPE"})
        for fid in fragments["fragment_ids"]:
            assert isinstance(fid, str)
            assert fid

    def test_caching_second_load_from_memory(self, resolver: KBResolver):
        first = resolver.index.raw["HDPE"]
        second = resolver.index.raw["HDPE"]
        assert first is second

    def test_version_tracking(self, resolver: KBResolver):
        spec = resolver.index.raw["HDPE"]
        assert spec["metadata"]["version"] == "1.2.0"
