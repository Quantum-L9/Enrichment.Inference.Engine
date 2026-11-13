"""inference_bridge_v2 logs with structured keywords, so its logger must accept them.

Regression: the module used a stdlib logging.Logger while passing structlog-style
keywords (domain=, edges=, ...). With logging at INFO, as in the deployed
container, every converge that built a derivation graph raised
`TypeError: Logger._log() got an unexpected keyword argument 'domain'` and
answered `failed`. In-process tests run at WARNING, so the info calls never ran
and the defect stayed hidden. Found by the CTO combined-stack e2e on 2026-11-13.
"""

from __future__ import annotations

import logging

import pytest

from app.engines import inference_bridge_v2 as bridge

pytestmark = pytest.mark.unit

_SPEC = {
    "domain": "logging-probe",
    "ontology": {
        "nodes": [
            {
                "label": "Thing",
                "properties": [
                    {"name": "a", "type": "float"},
                    {"name": "b", "type": "float", "derived_from": ["a"]},
                ],
            }
        ]
    },
}


def test_derivation_and_inference_log_at_info_without_raising(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.DEBUG):
        logging.getLogger().setLevel(logging.DEBUG)
        graph = bridge.build_derivation_graph(_SPEC)
        result = bridge.run_inference(graph, {"a": 1.0}, {"a": 0.9})
    assert result is not None
