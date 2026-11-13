"""Evidence-state tie-break for acquisition order (Campaign B, B3).

The tie-break orders only fields the planner's own ranking leaves equal. It
changes no mode, budget, variation count or threshold.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from app.engines.convergence.acquisition_order import (
    CONFLICTING,
    NO_EVIDENCE,
    WITHHELD_CLAIM,
    evidence_tiebreak,
    latest_withheld,
)
from app.engines.meta_prompt_planner import MetaPromptPlanner, PromptPlan
from app.models.loop_schemas import PassContext
from app.models.schemas import EnrichRequest, EnrichResponse

FIELDS = ["alpha", "bravo", "charlie", "delta"]
ONE = {"candidates": [{"value": "x", "confidence": 0.4}]}
TWO = {"candidates": [{"value": "x", "confidence": 0.4}, {"value": "y", "confidence": 0.4}]}


def _plan(planner: MetaPromptPlanner, withheld: Any = None, **overrides: Any) -> PromptPlan:
    args: dict[str, Any] = {
        "entity_state": dict.fromkeys(FIELDS),
        "pass_context": PassContext(
            pass_number=2, budget_remaining=20000, previous_uncertainty=6.0
        ),
        "field_confidences": {},
        "uncertainty_score": 6.0,
        "required_fields": FIELDS,
    }
    return planner.plan_pass(**{**args, **overrides}, withheld_fields=withheld)


def test_ranks_are_positions_by_how_little_is_known() -> None:
    ranks = evidence_tiebreak(FIELDS, {"bravo": ONE, "charlie": TWO, "delta": {"candidates": []}})
    assert ranks == {
        "alpha": NO_EVIDENCE,
        "bravo": WITHHELD_CLAIM,
        "charlie": CONFLICTING,
        "delta": NO_EVIDENCE,  # a record with no candidate says nothing
    }
    assert NO_EVIDENCE > WITHHELD_CLAIM > CONFLICTING
    assert evidence_tiebreak(FIELDS, {"zulu": TWO}) == dict.fromkeys(FIELDS, NO_EVIDENCE)
    assert evidence_tiebreak([], {"alpha": ONE}) == {}


def test_latest_withheld_takes_the_most_recent_pass_per_field() -> None:
    assert latest_withheld([{"alpha": ONE, "bravo": ONE}, {}, {"alpha": TWO}]) == {
        "alpha": TWO,
        "bravo": ONE,
    }
    assert latest_withheld([]) == {}


def test_tiebreak_orders_only_fields_the_ranking_leaves_equal() -> None:
    planner = MetaPromptPlanner()  # no domain: every field ties on gate and weight
    baseline = _plan(planner).priority_fields
    assert sorted(baseline) == FIELDS
    # Nothing withheld, or nothing about these fields: the order is untouched.
    assert _plan(planner, {}).priority_fields == baseline
    assert _plan(planner, {"zulu": TWO}).priority_fields == baseline
    ordered = _plan(planner, {"delta": TWO, "charlie": ONE}).priority_fields
    # No evidence first (in the baseline's own order), then the claim, then the conflict.
    assert ordered == [n for n in baseline if n in ("alpha", "bravo")] + ["charlie", "delta"]


def test_tiebreak_never_outranks_the_planners_own_ordering() -> None:
    spec = {
        "ontology": {
            "nodes": {
                "Thing": {
                    "properties": {
                        "delta": {"type": "string", "gate_critical": True},
                        "charlie": {"type": "string"},
                        "alpha": {"type": "string"},
                    }
                }
            }
        }
    }
    planner = MetaPromptPlanner(spec)
    fields = ["alpha", "charlie", "delta"]
    baseline = _plan(planner, entity_state=dict.fromkeys(fields), required_fields=fields)
    assert baseline.priority_fields[0] == "delta"  # the planner's own rule, before any tie-break
    conflicted = _plan(
        planner, {"delta": TWO}, entity_state=dict.fromkeys(fields), required_fields=fields
    )
    assert conflicted.priority_fields[0] == "delta"  # a conflict does not demote a gate field


def test_tiebreak_changes_no_mode_budget_or_target_set() -> None:
    planner = MetaPromptPlanner()
    plain, tied = _plan(planner), _plan(planner, {"alpha": TWO, "bravo": ONE})
    assert (tied.mode, tied.variation_count, tied.kb_fragment_ids) == (
        plain.mode,
        plain.variation_count,
        plain.kb_fragment_ids,
    )
    assert sorted(tied.priority_fields) == sorted(plain.priority_fields)
    # Domain priority hints still lead, whatever the evidence says.
    hinted = MetaPromptPlanner(priority_fields=["bravo"])
    assert _plan(hinted, {"bravo": TWO}).priority_fields[0] == "bravo"


@pytest.mark.asyncio
async def test_loop_hands_the_planner_what_earlier_passes_withheld() -> None:
    from app.engines.convergence.convergence_config import ConvergenceConfig
    from app.engines.convergence_controller import run_convergence_loop

    async def enricher(pass_request, settings, kb_resolver, idem_store, sonar_config=None):
        return EnrichResponse(
            fields={"alpha": "a"},
            confidence=0.5,
            tokens_used=10,
            state="completed",
            feature_vector={
                "per_field_confidence": {"alpha": 0.5},
                "withheld_fields": {"bravo": TWO},
            },
        )

    settings = MagicMock(max_budget_tokens=30000, default_timeout_seconds=30)
    seen: list[Any] = []
    original = MetaPromptPlanner.plan_pass

    def recording(self: MetaPromptPlanner, *args: Any, **kwargs: Any) -> PromptPlan:
        seen.append(kwargs["withheld_fields"])
        return original(self, *args, **kwargs)

    with patch.object(MetaPromptPlanner, "plan_pass", recording):
        await run_convergence_loop(
            request=EnrichRequest(
                entity={"id": "rec:1"},
                object_type="Account",
                objective="Fill the record",
                schema=dict.fromkeys(["alpha", "bravo"], "string"),
            ),
            settings=settings,
            kb_resolver=MagicMock(),
            enricher=enricher,
            inference_rules=[],
            convergence_config=ConvergenceConfig(max_passes=2),
        )
    assert seen[0] == {}  # nothing is known before the first pass
    assert seen[1] == {"bravo": TWO}
