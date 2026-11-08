# SOVA Console on the web (sova.mangowhois.ai): the desktop renderer for the browser plus its per-person server.
# Build context: services/console/desktop (self-contained). One image for the platform node (linux/amd64).
FROM node:22-trixie-slim@sha256:be1e2a3dc2c4fd68cdfadad2a696a280e0d2749098bee509230174d72fb768c6 AS build
ENV ELECTRON_SKIP_BINARY_DOWNLOAD=1 CI=1
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
WORKDIR /src
RUN corepack enable && corepack prepare pnpm@10.28.2 --activate
COPY package.json pnpm-lock.yaml .npmrc ./
# No install scripts: the Electron binary download is the desktop app's, not the web image's.
RUN pnpm install --frozen-lockfile --ignore-scripts
COPY . .
RUN pnpm build:web \
    && runtime=$(dirname "$(dirname "$(find node_modules/@openai -path '*/vendor/*linux*/bin/codex' -type f | head -1)")") \
    && test -x "$runtime/bin/codex" && mkdir -p /out && cp -r "$runtime" /out/codex

FROM node:22-trixie-slim@sha256:be1e2a3dc2c4fd68cdfadad2a696a280e0d2749098bee509230174d72fb768c6
ENV NODE_ENV=production STATIC_DIR=/app/web DATA_DIR=/data CODEX_BIN=/app/codex/bin/codex CODEX_PATH=/app/codex/codex-path
# The base digest still ships zlib1g 1:1.3.dfsg+really1.3.1-1+b1: CVE-2026-76844 and CVE-2026-85091 (HIGH) are fixed
# in deb13u1 and the publish scan blocks the image until it is installed. Drop this once the digest carries it.
# The runtime only runs node: the package managers bundled in the base (npm, corepack, yarn) are not shipped.
RUN apt-get update && apt-get install --no-install-recommends -y zlib1g=1:1.3.dfsg+really1.3.1-1+deb13u1 \
    && rm -rf /var/lib/apt/lists/* /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/corepack /opt/yarn-* \
    && rm -f /usr/local/bin/npm /usr/local/bin/npx /usr/local/bin/corepack /usr/local/bin/yarn /usr/local/bin/yarnpkg
WORKDIR /app
COPY --from=build /src/dist/web /app/web
COPY --from=build /src/dist/server /app/server
COPY --from=build /out/codex /app/codex
USER 10001:10001
EXPOSE 8080
CMD ["node", "/app/server/server.cjs"]
