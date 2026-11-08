# Build context: repository root (docker build -f services/sova-mcp/Dockerfile .)
FROM python:3.12.16-slim-trixie@sha256:4840d514c583a4d3d8cad3940e68dee6f0f6f3635c12fcd0a3d74c21a5b93a54 AS build
# git: platform-events pins bravo-observability by commit (uv git source).
RUN apt-get update && apt-get install --no-install-recommends -y git=1:2.47.3-0+deb13u1 && rm -rf /var/lib/apt/lists/* \
    && pip install --disable-pip-version-check --no-cache-dir uv==0.10.2
COPY services/platform-events /src/services/platform-events
COPY services/sova-mcp /src/services/sova-mcp
WORKDIR /src/services/sova-mcp
RUN rm -rf .venv && UV_PROJECT_ENVIRONMENT=/opt/venv uv sync --frozen --no-dev --extra host --no-editable --compile-bytecode

FROM python:3.12.16-slim-trixie@sha256:4840d514c583a4d3d8cad3940e68dee6f0f6f3635c12fcd0a3d74c21a5b93a54
ENV PATH=/opt/venv/bin:$PATH PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
# The base digest still ships zlib1g 1:1.3.dfsg+really1.3.1-1+b1: CVE-2026-76844 and CVE-2026-85091 (HIGH) are fixed
# in deb13u1 and the publish scan blocks the image until it is installed. Drop this once the digest carries it.
RUN apt-get update && apt-get install --no-install-recommends -y zlib1g=1:1.3.dfsg+really1.3.1-1+deb13u1 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=build /opt/venv /opt/venv
USER 10001:10001
EXPOSE 8080
ENTRYPOINT ["sova-mcp"]
