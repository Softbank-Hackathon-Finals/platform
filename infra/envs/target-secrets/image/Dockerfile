FROM quay.io/openbao/openbao:2.7.2@sha256:a78eb12a9fc7a3ebb4bec1724e2bdd6a28e33394820641d061166919a3f1866b
COPY --chmod=555 bao /usr/bin/bao
LABEL org.opencontainers.image.title="Bravo target OpenBao" org.opencontainers.image.version="2.7.2-bravo.3-nonroot" org.opencontainers.image.revision="9e0a0d3c3504a0a00b45e61355afac5e35c78379+go-jose-4.1.6"
USER 100:1000
