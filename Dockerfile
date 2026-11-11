# Dev image. Same digest-pinned python:3.12-slim base as Dockerfile.prod (the
# project requires Python 3.12, which uv.lock is resolved for).
FROM python:3.12-slim@sha256:aa943ce411423c7582288f18707ff66eddcbaeb510502d747a72aeb34f30d159 AS base

WORKDIR /app

# git: uv fetches the Gate SDK from its git source at the commit uv.lock records.
RUN apt-get update && apt-get install -y --no-install-recommends curl git && rm -rf /var/lib/apt/lists/*

# uv.lock is the single lock: install_locked.sh bootstraps a hash-pinned uv and
# syncs the full profile (the project, editable, plus every dependency group)
# into /opt/venv. The editable install points at /app, so the tree is copied first.
COPY . .
RUN UV_PROJECT_ENVIRONMENT=/opt/venv PIP_NO_CACHE_DIR=1 bash tools/deps/install_locked.sh full
ENV PATH="/opt/venv/bin:$PATH"

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
# The same split-brain applies across pods (EIE-212-F001): every supported
# deployment runs exactly one replica of this image. Lifting either limit
# requires moving the channel to shared state first; the invariants are asserted
# by tests/unit/test_worker_singleton_invariant.py and
# tests/unit/test_deployment_topology.py.
# Dev image: apply the schema (alembic upgrade head) before serving; see the script.
ENTRYPOINT ["sh", "/app/scripts/docker-entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
