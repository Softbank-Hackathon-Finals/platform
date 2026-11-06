# Disposable local qualification, never a team workload image.
FROM postgres:18.4@sha256:a02db8cac496f15b094798a38254f14d6e00741f709360e5e00bb6668ea31636
RUN apt-get update -qq && apt-get install -y --no-install-recommends python3=3.13.5-1 python3-venv=3.13.5-1 && rm -rf /var/lib/apt/lists/*
COPY rotation-requirements.txt /tmp/rotation-requirements.txt
RUN python3 -m venv /opt/qualification && /opt/qualification/bin/pip install --no-cache-dir -r /tmp/rotation-requirements.txt
RUN mkdir -p /tmp/setup /tmp/logs && chown postgres:postgres /tmp/setup /tmp/logs
USER postgres
