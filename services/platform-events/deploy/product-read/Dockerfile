FROM python:3.12.16-slim-bookworm@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --disable-pip-version-check --no-cache-dir --require-hashes --only-binary=:all: -r requirements.txt
COPY src /usr/local/lib/python3.12/site-packages
RUN chmod -R a+rX /app /usr/local/lib/python3.12/site-packages/bravo_platform_events /usr/local/lib/python3.12/site-packages/bravo_agent /usr/local/lib/python3.12/site-packages/rehearsal_runner /usr/local/lib/python3.12/site-packages/bravo_observability
USER 10001:10001
EXPOSE 8443
ENTRYPOINT ["python", "-m", "bravo_platform_events.product_read_server"]
