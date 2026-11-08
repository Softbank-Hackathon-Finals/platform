# T46 호환 런타임 베이스 이미지: 실행기만 들어 있다. 핸들러는 이 위에 얹은 함수 이미지가 /var/task에 둔다(ADR-0045).
# 핸들러 의존성도 Lambda zip처럼 /var/task에 함께 넣는다(pip install --target /var/task).
# 빌드 컨텍스트는 이 폴더다. 컨텍스트에 들어가는 파일은 .dockerignore의 허용 목록뿐이다.
FROM ghcr.io/astral-sh/uv:0.13.6@sha256:af22a52f5eafef3d78e551599d405c02df61919d18028f0cea8eda93bc9986d0 AS uv
FROM python:3.12.16-slim-trixie@sha256:4840d514c583a4d3d8cad3940e68dee6f0f6f3635c12fcd0a3d74c21a5b93a54
# 기반 이미지 이후 나온 보안 수정판(검사의 fixed 항목)을 빌드 때 받는다
RUN apt-get update && apt-get -y upgrade --no-install-recommends && rm -rf /var/lib/apt/lists/*
COPY --from=uv /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock /opt/function-runtime/
COPY src /opt/function-runtime/src
WORKDIR /opt/function-runtime
RUN uv sync --frozen --no-dev --no-editable --no-cache && chmod -R a-w /opt/function-runtime \
    && rm /usr/local/bin/uv && mkdir -p /var/task
ENV PATH="/opt/function-runtime/.venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1 LAMBDA_TASK_ROOT=/var/task
USER 10001:10001
ENTRYPOINT ["/opt/function-runtime/.venv/bin/bravo-function-runtime", "run", "--config", "/etc/function-runtime/config.json"]
