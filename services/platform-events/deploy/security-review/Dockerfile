FROM python:3.12.16-slim-trixie@sha256:4840d514c583a4d3d8cad3940e68dee6f0f6f3635c12fcd0a3d74c21a5b93a54
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN apt-get update && apt-get install --no-install-recommends -y zlib1g=1:1.3.dfsg+really1.3.1-1+deb13u1 \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt /app/requirements.txt
RUN pip install --disable-pip-version-check --no-cache-dir --require-hashes --only-binary=:all: -r requirements.txt
COPY src /usr/local/lib/python3.12/site-packages
COPY image-import-closure.py /app/image-import-closure.py
RUN --network=none python /app/image-import-closure.py \
    --src /usr/local/lib/python3.12/site-packages \
    --site /usr/local/lib/python3.12/site-packages \
    sova_security.interfaces.review_server
RUN chmod -R a+rX /app /usr/local/lib/python3.12/site-packages/sova_security
USER 10001:10001
EXPOSE 8443
ENTRYPOINT ["python", "-m", "sova_security.interfaces.review_server"]
