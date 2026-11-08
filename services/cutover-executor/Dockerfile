FROM ghcr.io/astral-sh/uv:0.10.2 AS uv
FROM python:3.12-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df AS build
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable --no-cache

FROM python:3.12-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df
RUN python -m pip uninstall -y pip setuptools && rm -rf /usr/local/lib/python3.12/ensurepip
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 10001:10001
EXPOSE 8080
CMD ["uvicorn", "bravo_cutover.server:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080", "--no-access-log", "--ssl-keyfile", "/run/executor/tls.key", "--ssl-certfile", "/run/executor/tls.crt"]
