FROM ghcr.io/astral-sh/uv:0.10.2 AS uv
FROM python:3.12.12-slim-bookworm
COPY --from=uv /uv /usr/local/bin/uv
# Build from the repository root (docker build -f services/agent-worker/Dockerfile .):
# the T44 planning library is a path dependency (../iac) of the worker.
WORKDIR /app/services/agent-worker
COPY services/iac/pyproject.toml /app/services/iac/
COPY services/iac/src /app/services/iac/src
COPY services/agent-worker/pyproject.toml services/agent-worker/uv.lock services/agent-worker/README.md ./
COPY services/agent-worker/src ./src
RUN uv sync --frozen --no-dev --no-editable --no-cache && chmod -R a-w /app
ENV PATH="/app/services/agent-worker/.venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1
USER 10001:10001
ENTRYPOINT ["/app/services/agent-worker/.venv/bin/bravo-agent"]
