# pi + the C-3PO extension, for running a pi agent in a container.
#   docker build -f pi-extension/container/Dockerfile -t c3po-pi pi-extension
# No credentials, API keys or project files are baked in; mount them at run time (see ../README.md).
FROM node:22-slim
ARG PI_VERSION=1.5.2
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates curl ripgrep \
 && rm -rf /var/lib/apt/lists/* \
 && npm install -g --silent "@earendil-works/pi-coding-agent@${PI_VERSION}" \
 && npm cache clean --force
COPY extensions /opt/c3po-pi/extensions
COPY src /opt/c3po-pi/src
COPY package.json /opt/c3po-pi/package.json
COPY container/entrypoint.sh /usr/local/bin/c3po-pi-entrypoint
RUN chmod 0755 /usr/local/bin/c3po-pi-entrypoint && mkdir -p /work /home/node/.pi-agent && chown -R node:node /work /home/node
USER node
ENV HOME=/home/node PI_CODING_AGENT_DIR=/home/node/.pi-agent C3PO_CREDENTIALS_FILE=/run/c3po/credentials.json
WORKDIR /work
ENTRYPOINT ["c3po-pi-entrypoint"]
