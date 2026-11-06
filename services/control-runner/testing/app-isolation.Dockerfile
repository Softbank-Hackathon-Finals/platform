# Local qualification image only; never deploy to the team workload.
FROM postgres:18.4@sha256:a02db8cac496f15b094798a38254f14d6e00741f709360e5e00bb6668ea31636
RUN apt-get update -qq && apt-get install -y --no-install-recommends python3=3.13.5-1 python3-venv=3.13.5-1 && rm -rf /var/lib/apt/lists/*
RUN python3 -m venv /opt/qualification && /opt/qualification/bin/pip install --no-cache-dir 'psycopg[binary]==3.3.7' botocore==1.43.123 jsonschema==4.26.0
RUN mkdir -p /tmp/setup /tmp/logs && chown postgres:postgres /tmp/setup /tmp/logs
USER postgres
