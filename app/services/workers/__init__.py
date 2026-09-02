"""
Workers module — background Redis Streams consumers.

- SchemaPromotionWorker: Auto-promotes discovered schema fields

Retired 2026-09-02 (seam audit, finding EIE-SIDE-DOOR-03): GraphInferenceConsumer,
a Redis Streams consumer of `graph.inference.complete`. No producer for that
stream exists in Cognitive.Engine.Graphs and the consumer was never started; it
was a shared-cache message bus between the two domain nodes that bypassed Gate.
GRAPH -> ENRICH inference results arrive as the Gate-routed
`graph-inference-result` action (app/services/graph_return_channel.py).
"""

from __future__ import annotations

from .schema_promotion_worker import SchemaPromotionWorker

__all__ = [
    "SchemaPromotionWorker",
]
