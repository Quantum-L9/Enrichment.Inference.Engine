FROM python:3.14-slim AS base

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl git && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml .
# constellation-node-sdk is a git+https dependency (Quantum-L9/Gate_SDK); git is
# required for the install. Gate_SDK is public (as of 2026-07-01), so pip clones
# anonymously with no SDK_TOKEN. If the repo is ever made private again, add a
# BuildKit secret (--mount=type=secret,id=sdk_token) + git insteadOf rewrite here.
RUN pip install --no-cache-dir ".[dev]"

COPY . .

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -f http://localhost:8000/api/v1/health || exit 1

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4"]
