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
event. This test evaluates the group and cancel expressions for each event
against the same ref, so a regression to a shared group fails here.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/ci.yml"
MAIN = "refs/heads/main"
EXPRESSION = re.compile(r"\$\{\{\s*(.*?)\s*\}\}")


def _concurrency() -> dict:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return workflow["concurrency"]


def _evaluate(template: object, event: str, ref: str) -> object:
    """Evaluate the expression forms ci.yml's concurrency block may use.

    Supported: ``github.event_name``, ``github.ref``, and
    ``github.event_name != '<literal>'`` / ``== '<literal>'``. Anything else
    fails the test instead of being guessed.
    """
    if isinstance(template, bool):
        return template
    assert isinstance(template, str)
    context = {"github.event_name": event, "github.ref": ref}

    def value(expression: str) -> object:
        comparison = re.fullmatch(r"github\.event_name\s*(==|!=)\s*'([^']*)'", expression)
        if comparison:
            equal = event == comparison.group(2)
            return equal if comparison.group(1) == "==" else not equal
        if expression in context:
            return context[expression]
        raise AssertionError(f"unsupported concurrency expression: {expression}")

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


def test_dispatch_runs_are_not_cancelled_in_flight() -> None:
    concurrency = _concurrency()
    assert _evaluate(concurrency["cancel-in-progress"], "workflow_dispatch", MAIN) is False


@pytest.mark.parametrize("event", ["push", "pull_request"])
def test_push_and_pull_request_still_cancel_superseded_runs(event: str) -> None:
    concurrency = _concurrency()
    assert _evaluate(concurrency["cancel-in-progress"], event, MAIN) is True
    # Two runs of the same event on the same ref still share one group.
    assert _evaluate(concurrency["group"], event, MAIN) == _evaluate(
        concurrency["group"], event, MAIN
    )
    assert MAIN in str(_evaluate(concurrency["group"], event, MAIN))
