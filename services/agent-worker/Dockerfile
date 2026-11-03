FROM ghcr.io/astral-sh/uv:0.10.2 AS uv
FROM python:3.12.12-slim-bookworm
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable --no-cache && chmod -R a-w /app
ENV PATH="/app/.venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1
USER 10001:10001
ENTRYPOINT ["/app/.venv/bin/bravo-agent"]
