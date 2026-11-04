# T37 콘솔용 관측 조회 API. T36 창 수신은 같은 이미지의 다른 진입점(bravo-observability-intake)이다.
# 판정 라이브러리와 같은 패키지를 api extra로 설치한다.
# 빌드 컨텍스트는 저장소 루트다(계약 스키마 정본 docs/observability/sli.schema.json을 패키지에 넣기 위해):
#   docker build -f services/observability/Dockerfile .
# 컨텍스트에 들어가는 파일은 Dockerfile.dockerignore의 허용 목록뿐이다.
FROM ghcr.io/astral-sh/uv:0.13.6@sha256:af22a52f5eafef3d78e551599d405c02df61919d18028f0cea8eda93bc9986d0 AS uv
FROM python:3.12.16-slim-bookworm@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337
COPY --from=uv /uv /usr/local/bin/uv
COPY docs/observability/sli.schema.json /src/docs/observability/sli.schema.json
COPY services/observability/pyproject.toml services/observability/uv.lock services/observability/README.md /src/services/observability/
COPY services/observability/src /src/services/observability/src
WORKDIR /src/services/observability
RUN uv sync --frozen --no-dev --extra api --no-editable --no-cache && chmod -R a-w /src && rm /usr/local/bin/uv
ENV PATH="/src/services/observability/.venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1
USER 10001:10001
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2)"]
ENTRYPOINT ["/src/services/observability/.venv/bin/bravo-observability-api"]
