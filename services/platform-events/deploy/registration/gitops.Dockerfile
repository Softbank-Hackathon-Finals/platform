FROM python:3.12.16-slim-trixie@sha256:4840d514c583a4d3d8cad3940e68dee6f0f6f3635c12fcd0a3d74c21a5b93a54
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN apt-get update && apt-get install --no-install-recommends -y git=1:2.47.3-0+deb13u1 \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt /app/requirements.txt
RUN pip install --disable-pip-version-check --no-cache-dir --require-hashes --only-binary=:all: -r requirements.txt
COPY src /usr/local/lib/python3.12/site-packages
COPY registration-checkout.py /app/registration-checkout.py
USER 10001:10001
RUN --network=none python -c "import os,sys,subprocess; from bravo_platform_events.registration_server import build_app; from bravo_platform_events.registration_sso import SsoRegistrationVerifier; from bravo_platform_events.gate_record_host import GateRecordHost; from bravo_gate.contract import T5Contract; T5Contract(); from bravo_platform_events.workflow_ports import create_ports; from bravo_platform_events.workflow_host_server import build; from bravo_platform_events.candidate_publication import workflow_diff_hash; from bravo_platform_events.application_host import ApplicationHost; assert os.getuid()==10001; assert not any(n in sys.modules for n in ('openai','bravo_agent.subscription_product','bravo_agent.subscription_admin')); subprocess.run(['git','--version'],check=True)"
EXPOSE 8443
ENTRYPOINT ["python", "-m", "bravo_platform_events.registration_server", "--tls-cert", "/tls/tls.crt", "--tls-key", "/tls/tls.key"]
