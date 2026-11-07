# 런타임 베이스 이미지: 실행기만 들어 있다. 핸들러는 이 위에 얹은 함수 이미지에 둔다(ADR-0041).
FROM python:3.12-slim
WORKDIR /opt/runtime
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir . && useradd --uid 10001 --no-create-home runtime
USER 10001
ENV LAMBDA_TASK_ROOT=/var/task
ENTRYPOINT ["bravo-function-runtime", "run", "--config", "/etc/function-runtime/config.json"]
