FROM python:3.14-slim AS base

WORKDIR /app

# The app runs from /app on PYTHONPATH; nothing is pip-installed from the tree.
ENV PYTHONPATH=/app

RUN apt-get update && apt-get install -y --no-install-recommends curl git && rm -rf /var/lib/apt/lists/*

# Supply chain: install exactly what requirements.lock pins, hash-verified,
# wheels only. The single carve-out is constellation-node-sdk: the lock pins it
# as the GitHub source archive of the commit the `v1` channel resolved to (with
# that archive's sha256), and a source archive has no wheel to choose.
# Regenerate the lock with `bash tools/lock_requirements.sh`.
# Unprivileged runtime user (created in the same layer as the install).
COPY requirements.lock .
RUN pip install --no-cache-dir --require-hashes --only-binary=:all: \
        --no-binary=constellation-node-sdk --no-deps -r requirements.lock \
    && adduser --disabled-password --gecos "" appuser

# Copy only what the runtime reads (no recursive copy of the build context):
# the app package; scripts/l9_contract_control.py (imported by
# app/bootstrap/l9_contract_runtime.py) and the entrypoint; the contract corpus
# the startup validation hashes (docs/contracts, contracts); configuration,
# domain specs and KB; and the alembic schema step the entrypoint runs.
# Everything is copied root-owned so the runtime user cannot modify its own
# code; only config/ (the schema-promotion worker writes config/domain_kb) is
# handed to appuser.
COPY app/ ./app/
COPY config/ ./config/
COPY domains/ ./domains/
COPY kb/ ./kb/
COPY contracts/ ./contracts/
COPY docs/contracts/ ./docs/contracts/
COPY migrations/ ./migrations/
COPY alembic.ini ./
COPY scripts/l9_contract_control.py scripts/docker-entrypoint.sh ./scripts/
RUN chown -R appuser:appuser /app/config
USER appuser

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
