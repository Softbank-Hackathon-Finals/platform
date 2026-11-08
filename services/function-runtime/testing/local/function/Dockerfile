# 함수 이미지: 런타임 베이스 위에 핸들러와 그 의존성만 얹는다(ADR-0045).
ARG RUNTIME_IMAGE=t46-function-runtime:local
# hadolint ignore=DL3006
FROM ${RUNTIME_IMAGE}
USER root
# 핸들러 의존성은 Lambda zip처럼 핸들러 옆(/var/task)에 넣는다. 런타임 가상환경은 읽기 전용이다.
RUN pip install --no-cache-dir --target /var/task "psycopg[binary]==3.2.*"
COPY pii_check.py slow.py /var/task/
USER 10001
