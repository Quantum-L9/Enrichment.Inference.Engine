FROM python:3.14-slim AS base

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl git && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml .
RUN pip install --no-cache-dir ".[dev]"

COPY . .

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -f http://localhost:8000/api/v1/health || exit 1

EXPOSE 8000

# EIE-003: ONE worker per container, deliberately. Each uvicorn worker is a
# separate OS process with its own Python memory, and GraphReturnChannel is a
# per-process in-memory singleton (app/services/graph_return_channel.py). With
# more than one worker, a graph-inference-result delivered to one process is
# invisible to the convergence loop draining the channel in another, so results
# are dropped as a function of which worker the load balancer happened to pick —
# and registration runs once per worker on top of that.
# Scale this service horizontally (more replicas), not with --workers.
# Lifting this requires moving the channel to shared state first; the invariant
# is asserted by tests/unit/test_worker_singleton_invariant.py.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
