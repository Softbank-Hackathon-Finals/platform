FROM ghcr.io/astral-sh/uv:0.10.2 AS uv
FROM python:3.12-slim-bookworm@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337 AS build
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable --no-cache

FROM python:3.12-slim-bookworm@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337
RUN python -m pip uninstall -y pip setuptools && rm -rf /usr/local/lib/python3.12/ensurepip
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 10001:10001
EXPOSE 8080
CMD ["uvicorn", "bravo_cutover.server:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080", "--no-access-log", "--ssl-keyfile", "/run/executor/tls.key", "--ssl-certfile", "/run/executor/tls.crt"]
