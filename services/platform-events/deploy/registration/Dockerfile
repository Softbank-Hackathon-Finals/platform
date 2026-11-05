# Build the reviewed product-read base from this exact source revision first.
# The operator supplies its verified, content-bound local image tag.
ARG BASE_IMAGE
FROM ${BASE_IMAGE}
USER 0:0
# SourceRelease.verify_checkout uses Git to read the operator-approved checkout.
RUN apt-get update && apt-get install --no-install-recommends -y git \
    && rm -rf /var/lib/apt/lists/*
USER 10001:10001
EXPOSE 8443
ENTRYPOINT ["python", "-m", "bravo_platform_events.registration_server", "--tls-cert", "/tls/tls.crt", "--tls-key", "/tls/tls.key"]
