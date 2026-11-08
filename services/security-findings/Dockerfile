# T33 보안 탐지 수집 · 조회 API. 온프렘 수집 에이전트(bravo_findings.agent_run)도 같은 이미지의 다른 진입점이다.
# 빌드 컨텍스트는 저장소 루트다(정규화 규칙 정본 docs/security/detection을 이미지에 넣기 위해):
#   docker build -f services/security-findings/Dockerfile .
# 컨텍스트에 들어가는 파일은 Dockerfile.dockerignore의 허용 목록뿐이다.
FROM ghcr.io/astral-sh/uv:0.13.6@sha256:af22a52f5eafef3d78e551599d405c02df61919d18028f0cea8eda93bc9986d0 AS uv
FROM python:3.12.16-slim-trixie@sha256:4840d514c583a4d3d8cad3940e68dee6f0f6f3635c12fcd0a3d74c21a5b93a54
# 기반 이미지 이후 나온 보안 수정판(검사의 fixed 항목)을 빌드 때 받는다
RUN apt-get update && apt-get -y upgrade --no-install-recommends && rm -rf /var/lib/apt/lists/*
COPY --from=uv /uv /usr/local/bin/uv
COPY docs/security/detection /src/docs/security/detection
COPY services/security-findings/pyproject.toml services/security-findings/uv.lock services/security-findings/README.md /src/services/security-findings/
COPY services/security-findings/src /src/services/security-findings/src
WORKDIR /src/services/security-findings
RUN uv sync --frozen --no-dev --no-editable --no-cache && chmod -R a-w /src && rm /usr/local/bin/uv
ENV PATH="/src/services/security-findings/.venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1 \
    BRAVO_FINDINGS_RULES_DIR=/src/docs/security/detection
USER 10001:10001
EXPOSE 8443
# TLS는 앱이 직접 한다(서버 인증서 Secret을 /etc/bravo-findings-tls에 둔다). 평문 포트는 열지 않는다.
ENTRYPOINT ["/src/services/security-findings/.venv/bin/uvicorn", "bravo_findings.api:create_app", "--factory", \
            "--host", "0.0.0.0", "--port", "8443", \
            "--ssl-certfile", "/etc/bravo-findings-tls/tls.crt", "--ssl-keyfile", "/etc/bravo-findings-tls/tls.key", \
            "--no-access-log"]
