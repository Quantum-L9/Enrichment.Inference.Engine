# L9-layer: chassis/adapter
"""FastAPI OpenTelemetry bootstrap adapter.

The concrete HTTP framework instrumentation belongs at the bootstrap chassis
boundary. Core telemetry callers retain their public ``setup_telemetry``
compatibility import, but this adapter owns the FastAPI type and the
FastAPI-specific instrumentor.
"""

from __future__ import annotations

import os

import structlog
from fastapi import FastAPI
from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.redis import RedisInstrumentor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

logger = structlog.get_logger(__name__)


def setup_telemetry(app: FastAPI, service_name: str | None = None) -> None:
    """Configure OpenTelemetry at the FastAPI composition boundary.

    Sets up distributed tracing, metrics collection, and context propagation
    across service boundaries. Instruments HTTPX and Redis clients automatically.
    """
    resolved_name = service_name or os.getenv("OTEL_SERVICE_NAME", "enrichment-engine")
    environment = os.getenv("ENVIRONMENT", "development")

    resource = Resource.create(
        {
            "service.name": resolved_name,
            "service.version": os.getenv("VERSION", "unknown"),
            "deployment.environment": environment,
        }
    )

    _setup_tracing(resource)
    _setup_metrics(resource)

    FastAPIInstrumentor.instrument_app(app)
    HTTPXClientInstrumentor().instrument()

    try:
        RedisInstrumentor().instrument()
    except Exception as exc:
        logger.warning("Redis instrumentation skipped: %s", exc)

    logger.info(
        "otel_initialized",
        service=resolved_name,
        environment=environment,
        endpoint=os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector:4317"),
    )


def _setup_tracing(resource: Resource) -> None:
    """Configure distributed tracing with an OTLP exporter."""
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector:4317")
    span_exporter = OTLPSpanExporter(endpoint=endpoint, insecure=True)
    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(BatchSpanProcessor(span_exporter))
    trace.set_tracer_provider(tracer_provider)


def _setup_metrics(resource: Resource) -> None:
    """Configure metrics with a 60-second OTLP export interval."""
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector:4317")
    metric_exporter = OTLPMetricExporter(endpoint=endpoint, insecure=True)
    metric_reader = PeriodicExportingMetricReader(metric_exporter, export_interval_millis=60_000)
    meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
    metrics.set_meter_provider(meter_provider)
