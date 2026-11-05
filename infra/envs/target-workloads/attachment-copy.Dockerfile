# One-shot attachment copy for the target cluster. Credentials arrive only at run time.
# Build from this directory; the Job is rendered by attachment_job.py with the resulting digest.
FROM python:3.12-slim-bookworm@sha256:da54eb86849fe19be7584d8b3f291fcf0c53c196760ab3d70dc6382f681ef9c0
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
