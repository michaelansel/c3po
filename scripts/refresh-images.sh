#!/bin/bash
set -euo pipefail

# Keep the running stack on current images without waiting for a deploy.
# Runs daily on the VM from c3po-refresh.timer (deploy/systemd/).
#
# 1. Pull the pre-built images (nginx, mcp-auth-proxy, redis) and rebuild the
#    coordinator against a freshly pulled python base. On Sundays the rebuild
#    skips the layer cache, so pip also picks up patch releases within
#    coordinator/requirements.txt's ranges.
# 2. If any running container's image differs from what its tag now points at,
#    restart c3po.service (which recreates the stack; long-polls reconnect).
# 3. Wait for health and re-probe the perimeter. A failure exits non-zero, so
#    it shows as a failed unit: `systemctl status c3po-refresh`.
#
# nginx and mcp-auth-proxy are the components unauthenticated requests reach,
# so they track floating tags (see docker-compose.prod.yml) and are refreshed
# here daily.

cd "$(dirname "$0")/.."
COMPOSE=(docker compose -f docker-compose.prod.yml)
HEALTH_URL="${C3PO_HEALTH_URL:-https://mcp.qerk.be/api/health}"

build_args=(--pull)
if [ "$(date +%u)" = 7 ]; then
    build_args+=(--no-cache)
fi

echo "Pulling images..."
"${COMPOSE[@]}" pull --quiet --ignore-buildable
echo "Building coordinator (${build_args[*]})..."
"${COMPOSE[@]}" build --quiet "${build_args[@]}"

stale=()
for container in $("${COMPOSE[@]}" ps -q); do
    read -r name ref running < <(docker inspect --format '{{.Name}} {{.Config.Image}} {{.Image}}' "$container")
    current=$(docker image inspect --format '{{.Id}}' "$ref")
    if [ "$running" != "$current" ]; then
        stale+=("${name#/}(${ref})")
    fi
done

if [ "${#stale[@]}" -eq 0 ]; then
    echo "All containers are on current images; nothing to do."
    exit 0
fi

echo "Newer images for: ${stale[*]} — restarting c3po.service"
systemctl restart c3po

for _ in $(seq 1 45); do
    if curl -sf -m 5 "$HEALTH_URL" | grep -q '"ok"'; then
        echo "Healthy after restart."
        exec bash scripts/probe-perimeter.sh "${HEALTH_URL%/api/health}"
    fi
    sleep 4
done
echo "Coordinator not healthy 3 minutes after restart" >&2
exit 1
