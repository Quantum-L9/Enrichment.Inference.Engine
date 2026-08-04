"""
OpenTelemetry auto-instrumentation for FastAPI.

Sets up distributed tracing, metrics, and context propagation.
Call setup_telemetry(app) once in app/main.py lifespan startup.

Usage:
    from app.core.telemetry import setup_telemetry
    setup_telemetry(app)

Environment Variables:
    OTEL_SERVICE_NAME            Service name (default: enrichment-engine)
    OTEL_EXPORTER_OTLP_ENDPOINT  Collector gRPC endpoint (default: http://otel-collector:4317)
    ENVIRONMENT                  Deployment env (default: development)
    VERSION                      Service version tag (default: unknown)
"""

from __future__ import annotations

import os

import structlog
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


def setup_telemetry(app: object, service_name: str | None = None) -> None:
    """
    Configure OpenTelemetry auto-instrumentation for FastAPI.

    Sets up distributed tracing, metrics collection, and context propagation
    across service boundaries. Instruments httpx and Redis clients automatically.
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
        msg = f"Redis instrumentation skipped: {exc}"
        logger.warning(msg)

    logger.info(
        "otel_initialized",
        service=resolved_name,
        environment=environment,
        endpoint=os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector:4317"),
    )


def _setup_tracing(resource: Resource) -> None:
    """Configure distributed tracing with OTLP export."""
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector:4317")
    span_exporter = OTLPSpanExporter(endpoint=endpoint, insecure=True)
    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(BatchSpanProcessor(span_exporter))
    trace.set_tracer_provider(tracer_provider)


def _setup_metrics(resource: Resource) -> None:
    """Configure metrics collection with 60s OTLP export interval."""
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector:4317")
    metric_exporter = OTLPMetricExporter(endpoint=endpoint, insecure=True)
    metric_reader = PeriodicExportingMetricReader(metric_exporter, export_interval_millis=60_000)
    meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
    metrics.set_meter_provider(meter_provider)
