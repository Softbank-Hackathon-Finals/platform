# Platform public entry reconciler. Standard library only. Build context: services/public-entry
FROM python:3.12.16-slim-bookworm@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337
COPY pyproject.toml README.md /src/
COPY src /src/src
RUN pip install --no-cache-dir --no-deps /src && rm -rf /src
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 10001:10001
ENTRYPOINT ["bravo-public-entry"]
