"""F-A1-09: the graph-sync reader refuses unpublished specs exactly where startup does."""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.config import Settings
from app.engines import packet_router
from app.engines.packet_router import (
    UNPUBLISHED_SPEC_ENVIRONMENTS,
    get_router,
    requires_published_specs,
)

MAIN = Path(__file__).resolve().parents[2] / "app" / "main.py"


def test_unpublished_environments_match_the_startup_reader() -> None:
    source = MAIN.read_text(encoding="utf-8")
    match = re.search(r"require_published=settings\.l9_environment not in \{([^}]*)\}", source)
    assert match, "app/main.py no longer builds its DomainYamlReader as expected"
    startup = {item.strip().strip("\"'") for item in match.group(1).split(",")}
    assert startup == set(UNPUBLISHED_SPEC_ENVIRONMENTS)


@pytest.mark.parametrize(
    ("environment", "required"),
    [("local", False), ("dev", False), ("test", False), ("staging", True), ("prod", True)],
)
def test_router_reader_requires_publication_outside_development(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, environment: str, required: bool
) -> None:
    monkeypatch.setattr(packet_router, "_router_singleton", None)
    settings = SimpleNamespace(
        gate_url="https://gate:8080", domains_dir=str(tmp_path), l9_environment=environment
    )
    router = get_router(settings)
    assert router._domain_reader is not None
    assert router._domain_reader.require_published is required
    monkeypatch.setattr(packet_router, "_router_singleton", None)


def test_settings_default_and_a_non_string_environment() -> None:
    assert requires_published_specs(SimpleNamespace()) is (
        Settings.model_fields["l9_environment"].default not in UNPUBLISHED_SPEC_ENVIRONMENTS
    )
    assert requires_published_specs(SimpleNamespace(l9_environment=None)) is True
