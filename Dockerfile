FROM python:3.14-slim AS base

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl git && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml .
# constellation-node-sdk is a private git+https dependency. The SDK_TOKEN is passed
# as a BuildKit secret (never a build-arg/layer). Configure the token-authenticated
# git rewrite, install, then remove the gitconfig in the SAME layer so the credential
# is not persisted into the image.
RUN --mount=type=secret,id=sdk_token \
    if [ -s /run/secrets/sdk_token ]; then \
      git config --global url."https://x-access-token:$(cat /run/secrets/sdk_token)@github.com/".insteadOf "https://github.com/"; \
    fi && \
    pip install --no-cache-dir ".[dev]" && \
    rm -f /root/.gitconfig

COPY . .

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -f http://localhost:8000/api/v1/health || exit 1

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4"]
