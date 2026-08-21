"""Tests for app/engines/convergence/pass_telemetry.py

Covers: Pass-over-pass metrics capture, trajectory tracking, ROI analysis.
"""

from __future__ import annotations

import pytest

from app.engines.convergence.pass_telemetry import PassTelemetryCollector
from app.models.loop_schemas import ConvergenceMode, PassResult


def _pass(
    pass_number: int,
    *,
    mode: ConvergenceMode = ConvergenceMode.DISCOVERY,
    fields_enriched: list[str] | None = None,
    fields_inferred: list[str] | None = None,
    field_confidences: dict[str, float] | None = None,
    uncertainty_before: float = 0.0,
    uncertainty_after: float = 0.0,
    tokens_used: int = 0,
    duration_ms: int = 0,
    rules_fired: list[str] | None = None,
) -> PassResult:
    return PassResult(
        pass_number=pass_number,
        mode=mode,
        fields_enriched=fields_enriched or [],
        fields_inferred=fields_inferred or [],
        field_confidences=field_confidences or {},
        uncertainty_before=uncertainty_before,
        uncertainty_after=uncertainty_after,
        tokens_used=tokens_used,
        duration_ms=duration_ms,
        rules_fired=rules_fired or [],
    )


class TestPassTelemetry:
    """Tests for pass-over-pass metrics capture."""

    @pytest.fixture
    def collector(self) -> PassTelemetryCollector:
        return PassTelemetryCollector()

    @pytest.fixture
    def sample_pass_result(self) -> PassResult:
        return _pass(
            1,
            mode=ConvergenceMode.DISCOVERY,
            fields_enriched=["polymer_type"],
            tokens_used=100,
            duration_ms=50,
        )

    @pytest.fixture
    def three_pass_results(self) -> list[PassResult]:
        return [
            _pass(
                1,
                mode=ConvergenceMode.DISCOVERY,
                fields_enriched=["a", "b", "c", "d"],
                fields_inferred=["e"],
                field_confidences={"a": 0.50, "b": 0.55},
                uncertainty_before=8.5,
                uncertainty_after=5.2,
                tokens_used=1200,
                duration_ms=3400,
                rules_fired=["rule_1"],
            ),
            _pass(
                2,
                mode=ConvergenceMode.TARGETED,
                fields_enriched=["f", "g"],
                fields_inferred=["h", "i"],
                field_confidences={"a": 0.70, "b": 0.75, "f": 0.80},
                uncertainty_before=5.2,
                uncertainty_after=3.1,
                tokens_used=800,
                duration_ms=2100,
                rules_fired=["rule_2", "rule_3"],
            ),
            _pass(
                3,
                mode=ConvergenceMode.VERIFICATION,
                fields_enriched=["j"],
                fields_inferred=[],
                field_confidences={"a": 0.90, "b": 0.88, "f": 0.85, "j": 0.82},
                uncertainty_before=3.1,
                uncertainty_after=1.8,
                tokens_used=400,
                duration_ms=1200,
                rules_fired=[],
            ),
        ]

    @pytest.fixture
    def three_passes(
        self, collector: PassTelemetryCollector, three_pass_results: list[PassResult]
    ) -> PassTelemetryCollector:
        for result in three_pass_results:
            collector.record_pass(result)
        return collector

    def test_record_pass_increments_count(
        self, collector: PassTelemetryCollector, sample_pass_result: PassResult
    ) -> None:
        collector.record_pass(sample_pass_result)
        assert collector.pass_count == 1

    def test_uncertainty_trajectory(self, three_passes: PassTelemetryCollector) -> None:
        report = three_passes.convergence_report()
        assert report.uncertainty_trajectory == [5.2, 3.1, 1.8]

    def test_confidence_trajectory_ascending(self, three_passes: PassTelemetryCollector) -> None:
        trajectory = three_passes.convergence_report().confidence_trajectory
        assert trajectory == sorted(trajectory)

    def test_tokens_per_pass(self, three_passes: PassTelemetryCollector) -> None:
        report = three_passes.convergence_report()
        assert report.tokens_per_pass == [1200, 800, 400]

    def test_fields_gained_per_pass(self, three_passes: PassTelemetryCollector) -> None:
        report = three_passes.convergence_report()
        assert report.fields_per_pass == [5, 4, 1]

    def test_diminishing_returns_check_true(self, collector: PassTelemetryCollector) -> None:
        # window=2 requires 3 snapshots; last-window avg improvement must be < 5%.
        collector.record_pass(_pass(1, uncertainty_after=8.0, tokens_used=100, duration_ms=100))
        collector.record_pass(_pass(2, uncertainty_after=2.0, tokens_used=100, duration_ms=100))
        collector.record_pass(_pass(3, uncertainty_after=1.96, tokens_used=100, duration_ms=100))
        assert collector.diminishing_returns_check() is True

    def test_diminishing_returns_check_false(self, collector: PassTelemetryCollector) -> None:
        collector.record_pass(
            _pass(
                1,
                fields_enriched=["a", "b"],
                uncertainty_before=8.0,
                uncertainty_after=6.0,
                tokens_used=1000,
                duration_ms=1000,
            )
        )
        collector.record_pass(
            _pass(
                2,
                fields_enriched=["c", "d", "e", "f"],
                uncertainty_before=6.0,
                uncertainty_after=4.0,
                tokens_used=1000,
                duration_ms=1000,
            )
        )
        collector.record_pass(
            _pass(
                3,
                fields_enriched=["g", "h", "i"],
                uncertainty_before=4.0,
                uncertainty_after=2.0,
                tokens_used=1000,
                duration_ms=1000,
            )
        )
        # (4.0 - 2.0) / 4.0 = 0.50 >= 0.05 → not diminishing
        assert collector.diminishing_returns_check() is False

    def test_total_duration(self, three_pass_results: list[PassResult]) -> None:
        total = sum(p.duration_ms for p in three_pass_results)
        assert total == 6700
