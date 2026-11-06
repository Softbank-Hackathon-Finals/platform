FROM python:3.12.16-slim-trixie@sha256:4840d514c583a4d3d8cad3940e68dee6f0f6f3635c12fcd0a3d74c21a5b93a54
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --disable-pip-version-check --no-cache-dir --require-hashes --only-binary=:all: -r requirements.txt
COPY src /usr/local/lib/python3.12/site-packages
RUN chmod -R a+rX /app /usr/local/lib/python3.12/site-packages/bravo_platform_events /usr/local/lib/python3.12/site-packages/bravo_agent /usr/local/lib/python3.12/site-packages/rehearsal_runner /usr/local/lib/python3.12/site-packages/bravo_observability
USER 10001:10001
EXPOSE 8443
ENTRYPOINT ["python", "-m", "bravo_platform_events.product_read_server"]
