# T37 콘솔용 관측 조회 API. T36 창 수신은 같은 이미지의 다른 진입점(bravo-observability-intake)이다.
# 판정 라이브러리와 같은 패키지를 api extra로 설치한다.
# 빌드 컨텍스트는 저장소 루트다(계약 스키마 정본 docs/observability/sli.schema.json을 패키지에 넣기 위해):
#   docker build -f services/observability/Dockerfile .
# 컨텍스트에 들어가는 파일은 Dockerfile.dockerignore의 허용 목록뿐이다.
FROM ghcr.io/astral-sh/uv:0.13.6@sha256:af22a52f5eafef3d78e551599d405c02df61919d18028f0cea8eda93bc9986d0 AS uv
FROM python:3.12.16-slim-trixie@sha256:4840d514c583a4d3d8cad3940e68dee6f0f6f3635c12fcd0a3d74c21a5b93a54
# 기반 이미지 이후 나온 보안 수정판(검사의 fixed 항목)을 빌드 때 받는다
RUN apt-get update && apt-get -y upgrade --no-install-recommends && rm -rf /var/lib/apt/lists/*
COPY --from=uv /uv /usr/local/bin/uv
COPY docs/observability/sli.schema.json /src/docs/observability/sli.schema.json
COPY services/observability/pyproject.toml services/observability/uv.lock services/observability/README.md /src/services/observability/
COPY services/observability/src /src/services/observability/src
WORKDIR /src/services/observability
RUN uv sync --frozen --no-dev --extra api --no-editable --no-cache && chmod -R a-w /src && rm /usr/local/bin/uv
ENV PATH="/src/services/observability/.venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1
USER 10001:10001
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s CMD ["python", "-m", "bravo_observability.healthcheck"]
ENTRYPOINT ["/src/services/observability/.venv/bin/bravo-observability-api"]
