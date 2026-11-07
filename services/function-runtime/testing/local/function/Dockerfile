# 함수 이미지: 런타임 베이스 위에 핸들러와 그 의존성만 얹는다(ADR-0041).
ARG RUNTIME_IMAGE=t46-function-runtime:local
FROM ${RUNTIME_IMAGE}
USER root
RUN pip install --no-cache-dir "psycopg[binary]>=3.2"
COPY pii_check.py /var/task/
USER 10001
