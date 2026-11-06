FROM python:3.12.16-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --disable-pip-version-check --no-cache-dir --require-hashes --only-binary=:all: -r requirements.txt
COPY src /usr/local/lib/python3.12/site-packages
COPY infra /app/infra
COPY templates /usr/local/lib/python3.12/templates
COPY kubectl /usr/local/bin/kubectl
RUN chmod 0755 /usr/local/bin/kubectl && find /app/infra -name "*.py" -exec chmod 0755 {} +
RUN chmod -R a+rX /app /usr/local/lib/python3.12/templates /usr/local/lib/python3.12/site-packages/bravo_platform_events /usr/local/lib/python3.12/site-packages/bravo_agent /usr/local/lib/python3.12/site-packages/rehearsal_runner /usr/local/lib/python3.12/site-packages/bravo_observability
USER 10001:10001
EXPOSE 8443
ENTRYPOINT ["python", "-m", "bravo_platform_events.rehearsal_server"]
