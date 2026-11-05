# One-shot attachment copy for the target cluster. Credentials arrive only at run time.
# Build from this directory; the Job is rendered by attachment_job.py with the resulting digest.
FROM python:3.12-slim-bookworm@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337
COPY attachment-copy.requirements.txt /tmp/requirements.txt
RUN python -m pip install --no-cache-dir --require-hashes --only-binary=:all: -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt \
    && python -m pip uninstall -y pip setuptools \
    && rm -rf /usr/local/lib/python3.12/ensurepip
# WORKDIR first: COPY --chmod would otherwise create /app without the search bit a non-root user needs.
WORKDIR /app
COPY --chmod=0444 attachment_copy.py attachment_stores.py sync_attachments.py /app/
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/nonexistent
USER 10001:10001
