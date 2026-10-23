#!/bin/bash
set -euo pipefail

# C3PO deployment to the production exe.dev VM (golf-pegasus, mcp.qerk.be).
#
# The VM runs the docker-compose.prod.yml stack under c3po.service, which
# rebuilds on start (`docker compose up --build`). Deploying is: pull the
# pushed commit on the VM, restart the service, wait for health.
#
# Push to origin/main first; the VM pulls from GitHub.
#
# Usage:
#   bash scripts/deploy.sh
#
# Override target with C3PO_DEPLOY_HOST / C3PO_DEPLOY_DIR / C3PO_HEALTH_URL.

REMOTE="${C3PO_DEPLOY_HOST:-exedev@golf-pegasus.exe.xyz}"
REMOTE_DIR="${C3PO_DEPLOY_DIR:-/home/exedev/c3po}"
HEALTH_URL="${C3PO_HEALTH_URL:-https://mcp.qerk.be/api/health}"

GREEN='\033[0;32m'
RED='\033[0;31m'
NC='\033[0m'
log() { echo -e "${GREEN}[c3po]${NC} $1"; }
err() { echo -e "${RED}[c3po]${NC} $1" >&2; }

log "Pulling latest on ${REMOTE}..."
ssh "$REMOTE" "cd ${REMOTE_DIR} && git pull --ff-only && git log --oneline -1"

log "Restarting c3po.service (rebuilds images)..."
ssh "$REMOTE" "sudo systemctl restart c3po"

log "Waiting for ${HEALTH_URL}..."
healthy=0
for _ in $(seq 1 45); do
    if body=$(curl -sf -m 5 "$HEALTH_URL") && echo "$body" | grep -q '"ok"'; then
        log "Healthy: ${body}"
        healthy=1
        break
    fi
    sleep 4
done
if [ "$healthy" -ne 1 ]; then
    err "Coordinator not healthy after 3 minutes. Check: ssh ${REMOTE} 'sudo journalctl -u c3po -n 100'"
    exit 1
fi

# Perimeter probe: the live edge + nginx must answer unauthenticated requests
# exactly as nginx.conf.template intends (see scripts/probe-perimeter.sh).
probe_fail=0
bash "$(dirname "$0")/probe-perimeter.sh" "${HEALTH_URL%/api/health}" || probe_fail=1

if [ "$probe_fail" -ne 0 ]; then
    err "Perimeter probe failed — the live perimeter does not match nginx.conf.template."
    exit 1
fi
log "Deployed and perimeter verified."
