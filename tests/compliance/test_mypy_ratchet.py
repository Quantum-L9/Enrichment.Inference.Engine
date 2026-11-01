"""The legacy MyPy ledger must remain empty under the strict type gate."""

from __future__ import annotations

from pathlib import Path

from tools.mypy_ratchet import recorded_entries


def test_repository_mypy_ledger_has_zero_recorded_errors() -> None:
    assert recorded_entries() == []


def test_comments_do_not_count_as_recorded_errors(tmp_path: Path) -> None:
    ledger = tmp_path / "mypy_baseline.txt"
    ledger.write_text("# strict MyPy\n\n", encoding="utf-8")

    assert recorded_entries(ledger) == []


def test_recorded_error_is_detected_as_strict_debt(tmp_path: Path) -> None:
    ledger = tmp_path / "mypy_baseline.txt"
    ledger.write_text("app/services/crm/field_mapper.py: Returning Any\n", encoding="utf-8")

    assert recorded_entries(ledger) == ["app/services/crm/field_mapper.py: Returning Any"]
