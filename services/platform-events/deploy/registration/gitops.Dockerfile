FROM python:3.12.16-slim-trixie@sha256:4840d514c583a4d3d8cad3940e68dee6f0f6f3635c12fcd0a3d74c21a5b93a54
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
# The base digest still ships zlib1g 1:1.3.dfsg+really1.3.1-1+b1: CVE-2026-76844 and CVE-2026-85091 (HIGH) are fixed
# in deb13u1 and the publish scan blocks the image until it is installed. Drop this once the digest carries it.
RUN apt-get update && apt-get install --no-install-recommends -y git=1:2.47.3-0+deb13u1 \
        zlib1g=1:1.3.dfsg+really1.3.1-1+deb13u1 \
    && rm -rf /var/lib/apt/lists/*
# ADR-0041 ENC-01.1: the T14 pooler TLS probe runs psql 18 (PGDG, pinned). The repository key is accepted only
# with its published fingerprint; the download tools are removed again.
RUN apt-get update && apt-get install --no-install-recommends -y ca-certificates=20250419+deb13u1 \
        curl=8.14.1-2+deb13u7 gnupg=2.4.7-21+deb13u1 \
    && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc -o /tmp/pgdg.asc \
    && gpg --show-keys --with-colons /tmp/pgdg.asc | grep -q '^fpr:::::::::B97B0AFCAA1A47F044F244A07FCC7D46ACCC4CF8:$' \
    && gpg --dearmor -o /usr/share/keyrings/pgdg.gpg /tmp/pgdg.asc \
    && echo "deb [signed-by=/usr/share/keyrings/pgdg.gpg] https://apt.postgresql.org/pub/repos/apt trixie-pgdg main" \
        > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update && apt-get install --no-install-recommends -y postgresql-client-18=18.6-1.pgdg13+2 libpq5=18.6-1.pgdg13+2 \
    && apt-get purge -y curl gnupg && apt-get autoremove -y && rm -rf /var/lib/apt/lists/* /tmp/pgdg.asc
# T31 apply from the workflow host (REHEARSE): the same patched, pinned kubectl as the rehearsal host.
COPY kubectl /usr/local/bin/kubectl
RUN chmod 0755 /usr/local/bin/kubectl
# The kubeconfig exec plugin for the T31 apply identity (infra/envs/rehearsal-access/credentials.py).
COPY bravo-credential /usr/local/bin/bravo-credential
RUN chmod 0755 /usr/local/bin/bravo-credential
COPY requirements.txt /app/requirements.txt
RUN pip install --disable-pip-version-check --no-cache-dir --require-hashes --only-binary=:all: -r requirements.txt
COPY src /usr/local/lib/python3.12/site-packages
COPY image-import-closure.py /app/image-import-closure.py
RUN --network=none python /app/image-import-closure.py --src /usr/local/lib/python3.12/site-packages --site /usr/local/lib/python3.12/site-packages bravo_platform_events.registration_server bravo_platform_events.workflow_host_server bravo_platform_events.workflow_ports bravo_platform_events.initial_proposal_relay bravo_platform_events.plan_state
COPY registration-checkout.py /app/registration-checkout.py
USER 10001:10001
RUN --network=none python -c "import os,sys,subprocess; from bravo_platform_events.registration_server import build_app; from bravo_platform_events.registration_sso import SsoRegistrationVerifier; from bravo_platform_events.gate_record_host import GateRecordHost; from bravo_gate.contract import T5Contract; T5Contract(); from bravo_platform_events.workflow_ports import create_ports; from bravo_platform_events.workflow_host_server import build; from bravo_platform_events.candidate_publication import workflow_diff_hash; from bravo_platform_events.application_host import ApplicationHost; assert os.getuid()==10001; assert not any(n in sys.modules for n in ('openai','bravo_agent.subscription_product','bravo_agent.subscription_admin')); subprocess.run(['git','--version'],check=True); from bravo_platform_events.target_controls import psql_qualification; import bravo_controls.postgres_tls, botocore; psql_qualification('/usr/lib/postgresql/18/bin/psql'); subprocess.run(['kubectl','version','--client'],check=True); r=subprocess.run(['/usr/local/bin/bravo-credential','exec','/nonexistent'],capture_output=True,text=True); assert r.returncode==1 and r.stdout==''"
EXPOSE 8443
ENTRYPOINT ["python", "-m", "bravo_platform_events.registration_server", "--tls-cert", "/tls/tls.crt", "--tls-key", "/tls/tls.key"]
