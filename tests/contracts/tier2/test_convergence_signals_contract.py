"""
tests/contracts/tier2/test_convergence_signals_contract.py

Contract tests for the bidirectional ENRICH↔GRAPH convergence loop signals.
Validates that signal models conform to the expected schema and that
workers can be instantiated.
"""

from __future__ import annotations


def test_inferred_triple_signal_model_fields():
    """InferredTripleSignal must have required fields for GRAPH→ENRICH loop."""
    from app.engines.convergence.convergence_signals import InferredTripleSignal

    signal = InferredTripleSignal(
        subject_id="entity-001",
        predicate="HAS_MATERIAL_AFFINITY",
        object_value="HDPE",
        confidence=0.85,
        source_rule="material_inference_v1",
        domain="plasticos",
        run_id="run-abc123",
    )

    assert signal.subject_id == "entity-001"
    assert signal.predicate == "HAS_MATERIAL_AFFINITY"
    assert signal.confidence == 0.85
    assert signal.domain == "plasticos"


def test_convergence_exit_reason_enum_values():
    """ConvergenceExitReason must define expected exit conditions."""
    from app.engines.convergence.convergence_signals import ConvergenceExitReason

    expected = {
        "delta_below_threshold",
        "no_new_fields",
        "max_passes_reached",
        "external_signal_empty",
        "budget_exhausted",
        "error",
    }
    actual = {e.value for e in ConvergenceExitReason}
    assert expected == actual


def test_graph_inference_event_model():
    """GraphInferenceEvent must parse GRAPH service payloads."""
    from app.engines.convergence.convergence_signals import (
        GraphInferenceEvent,
        InferredTripleSignal,
    )

    event = GraphInferenceEvent(
        entity_id="ent-001",
        domain="plasticos",
        run_id="run-xyz",
        inferred_triples=[
            InferredTripleSignal(
                subject_id="ent-001",
                predicate="PROCESSES",
                object_value="injection_molding",
                confidence=0.92,
                source_rule="process_inference",
                domain="plasticos",
                run_id="run-xyz",
            )
        ],
        materialization_pass=2,
        graph_confidence=0.88,
    )

    assert event.entity_id == "ent-001"
    assert len(event.inferred_triples) == 1
    assert event.materialization_pass == 2
    assert event.graph_confidence == 0.88


def test_graph_inference_results_arrive_through_gate_not_a_shared_stream():
    """Seam audit 2026-09-02: the Redis-stream consumer of GRAPH inference
    output was a shared-cache message bus between the two domain nodes. The
    governed path is the Gate-routed `graph-inference-result` action."""
    from app.services import workers
    from app.services.chassis_handlers import handle_graph_inference_result

    assert not hasattr(workers, "GraphInferenceConsumer")
    assert callable(handle_graph_inference_result)


def test_schema_promotion_worker_instantiates():
    """SchemaPromotionWorker must instantiate with Settings."""
    from pathlib import Path
    from unittest.mock import MagicMock

    from app.services.workers import SchemaPromotionWorker

    mock_settings = MagicMock()
    mock_settings.redis_url = "redis://localhost:6379/0"

    worker = SchemaPromotionWorker(settings=mock_settings, kb_root=Path("/tmp/test_kb"))
    assert worker is not None
    assert worker._running is False


def test_workers_module_exports():
    """Workers module must export expected classes."""
    from app.services import workers

    assert hasattr(workers, "SchemaPromotionWorker")
    assert not hasattr(workers, "GraphInferenceConsumer")
