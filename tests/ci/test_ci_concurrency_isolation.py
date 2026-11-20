# --- L9_META ---
# l9_schema: 1
# origin: native
# engine: enrichment
# layer: [ci]
# tags: [ci, concurrency, regression]
# owner: platform
# status: active
# --- /L9_META ---
"""ci.yml concurrency: a deep workflow_dispatch run is never cancelled by a push.

The group was ``ci-${{ github.ref }}`` with ``cancel-in-progress: true``, so a
push to main cancelled a running deep apply dispatched on main (the full
catalog, integration and e2e, up to 90 minutes). The group is now keyed on the
event, and each dispatch has its own group: GitHub keeps one pending run per
group and a newer one replaces it, so a shared dispatch group would drop a
queued deep run. This test evaluates the group and cancel expressions per event,
ref and run, so a regression to a shared group fails here.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/ci.yml"
MAIN = "refs/heads/main"
RUN_A, RUN_B = "1001", "1002"
EXPRESSION = re.compile(r"\$\{\{\s*(.*?)\s*\}\}")


def _concurrency() -> dict:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return workflow["concurrency"]


def _evaluate(template: object, event: str, ref: str, run_id: str = RUN_A) -> object:
    """Evaluate the expression forms ci.yml's concurrency block may use.

    Supported operands: ``github.event_name``, ``github.ref``,
    ``github.run_id`` and ``github.event_name ==/!= '<literal>'``, joined by
    ``&&`` / ``||`` with GitHub's value semantics (no parentheses). Anything
    else fails the test instead of being guessed.
    """
    if isinstance(template, bool):
        return template
    assert isinstance(template, str)
    context = {"github.event_name": event, "github.ref": ref, "github.run_id": run_id}

    def operand(expression: str) -> object:
        comparison = re.fullmatch(r"github\.event_name\s*(==|!=)\s*'([^']*)'", expression)
        if comparison:
            equal = event == comparison.group(2)
            return equal if comparison.group(1) == "==" else not equal
        if expression in context:
            return context[expression]
        raise AssertionError(f"unsupported concurrency expression: {expression}")

    def value(expression: str) -> object:
        result: object = False
        for alternative in expression.split("||"):
            term: object = True
            for part in alternative.split("&&"):
                term = operand(part.strip()) if term else term
            if term:
                return term
            result = term
        return result

    whole = EXPRESSION.fullmatch(template.strip())
    if whole:
        return value(whole.group(1))
    return EXPRESSION.sub(lambda match: str(value(match.group(1))), template)


@pytest.mark.parametrize("event", ["push", "pull_request"])
def test_dispatch_never_shares_a_group_with_push_or_pull_request(event: str) -> None:
    concurrency = _concurrency()
    dispatch_group = _evaluate(concurrency["group"], "workflow_dispatch", MAIN)
    other_group = _evaluate(concurrency["group"], event, MAIN)
    assert dispatch_group != other_group, (
        f"a workflow_dispatch run on {MAIN} shares concurrency group {dispatch_group!r} "
        f"with {event}; a {event} would cancel the deep run"
    )


def test_each_dispatch_has_its_own_group_so_a_queued_run_is_never_replaced() -> None:
    group = _concurrency()["group"]
    first = _evaluate(group, "workflow_dispatch", MAIN, RUN_A)
    second = _evaluate(group, "workflow_dispatch", MAIN, RUN_B)
    assert first != second, f"two dispatches on {MAIN} share {first!r}; a third would drop one"


def test_dispatch_runs_are_not_cancelled_in_flight() -> None:
    concurrency = _concurrency()
    assert _evaluate(concurrency["cancel-in-progress"], "workflow_dispatch", MAIN) is False


@pytest.mark.parametrize("event", ["push", "pull_request"])
def test_push_and_pull_request_still_cancel_superseded_runs(event: str) -> None:
    concurrency = _concurrency()
    assert _evaluate(concurrency["cancel-in-progress"], event, MAIN) is True
    group = concurrency["group"]
    # Two runs of the same event on the same ref share one group (supersession),
    # whatever their run ids.
    assert _evaluate(group, event, MAIN, RUN_A) == _evaluate(group, event, MAIN, RUN_B)
    # Distinct refs (two pull requests, two branches) never share a group.
    other = "refs/pull/2/merge" if event == "pull_request" else "refs/heads/feature"
    first = "refs/pull/1/merge" if event == "pull_request" else MAIN
    assert _evaluate(group, event, first) != _evaluate(group, event, other)
