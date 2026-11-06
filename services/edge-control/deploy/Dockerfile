FROM python:3.12.16-slim-trixie@sha256:4840d514c583a4d3d8cad3940e68dee6f0f6f3635c12fcd0a3d74c21a5b93a54
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --disable-pip-version-check --no-cache-dir --require-hashes --only-binary=:all: -r requirements.txt
COPY src /usr/local/lib/python3.12/site-packages
USER 10001:10001
RUN --network=none python -c "import os,sys; from bravo_edge_control.daemon import serve; from bravo_gate.contract import T5Contract; T5Contract(); assert os.getuid()==10001; assert not any(n in sys.modules for n in ('openai','bravo_agent','rehearsal_runner'))"
ENTRYPOINT ["python", "-m", "bravo_edge_control.daemon"]
