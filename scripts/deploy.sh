#!/bin/bash
set -euo pipefail

# C3PO Deployment Script
# Deploys coordinator and Redis to Synology NAS

# Configuration
NAS_HOST="${NAS_HOST:-admin@mkansel-nas.home.qerk.be}"
NAS_DATA_DIR="${NAS_DATA_DIR:-/volume1/enc-containers/c3po}"
COORDINATOR_PORT="${COORDINATOR_PORT:-8420}"
REDIS_PORT="${REDIS_PORT:-6380}"  # Non-standard to avoid conflicts
# Docker path on Synology NAS (not in default SSH PATH)
NAS_DOCKER="/usr/local/bin/docker"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log() { echo -e "${GREEN}[c3po]${NC} $1"; }
warn() { echo -e "${YELLOW}[c3po]${NC} $1"; }
error() { echo -e "${RED}[c3po]${NC} $1" >&2; }

usage() {
    cat <<EOF
Usage: $0 <command>

Commands:
    build       Build the coordinator Docker image locally
    push        Push the image to the NAS
    deploy      Deploy/restart containers on the NAS
    logs        Show coordinator logs
    status      Show container status
    stop        Stop all c3po containers
    shell       SSH into the NAS
    full        Build, push, and deploy (full deployment)

Environment:
    NAS_HOST        SSH target (default: admin@mkansel-nas.home.qerk.be)
    NAS_DATA_DIR    Data directory on NAS (default: /volume1/enc-containers/c3po)
    COORDINATOR_PORT  Coordinator port (default: 8420)
    REDIS_PORT      Redis port (default: 6380)

EOF
    exit 1
}

# Copy source to NAS and build image there (avoids arm64/x86_64 architecture mismatch)
cmd_build() {
    log "Copying source to NAS..."
    cd "$PROJECT_DIR/coordinator"

    # Create coordinator directory on NAS and copy source
    ssh "$NAS_HOST" "mkdir -p ${NAS_DATA_DIR}/coordinator"
    tar czf - --exclude='__pycache__' --exclude='.venv' --exclude='.pytest_cache' --exclude='tests' . | \
        ssh "$NAS_HOST" "cd ${NAS_DATA_DIR}/coordinator && tar xzf -"

    log "Building image on NAS..."
    ssh "$NAS_HOST" "cd ${NAS_DATA_DIR}/coordinator && ${NAS_DOCKER} build -t c3po-coordinator:latest ."

    log "Build complete"
}

# Alias for backwards compatibility (build now happens on NAS)
cmd_push() {
    warn "The 'push' command is no longer needed - 'build' now builds directly on NAS"
    log "Run 'deploy' to start containers"
}

# Secrets file location on NAS
SECRETS_FILE="${NAS_DATA_DIR}/.secrets"

# Generate or load secrets
ensure_secrets() {
    log "Checking secrets..."

    # Check if secrets file exists on NAS
    if ssh "$NAS_HOST" "test -f ${SECRETS_FILE}"; then
        log "Loading existing secrets"
        # Check if new auth keys exist, add them if missing (migration from v0.5.x)
        local has_server_secret
        has_server_secret=$(ssh "$NAS_HOST" "grep -c C3PO_SERVER_SECRET ${SECRETS_FILE} || true")
        if [[ "$has_server_secret" == "0" ]]; then
            log "Migrating secrets: adding C3PO_SERVER_SECRET and C3PO_ADMIN_KEY..."
            local server_secret admin_key
            server_secret=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
            admin_key=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
            ssh "$NAS_HOST" "echo 'C3PO_SERVER_SECRET=${server_secret}' >> ${SECRETS_FILE} && echo 'C3PO_ADMIN_KEY=${admin_key}' >> ${SECRETS_FILE}"
            warn "New secrets added. Admin key: ${admin_key}"
            warn "Save the admin key — it is needed for client enrollment."
        fi
    else
        log "Generating new secrets..."
        local redis_password proxy_bearer_token server_secret admin_key
        redis_password=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
        proxy_bearer_token=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
        server_secret=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
        admin_key=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")

        ssh "$NAS_HOST" "mkdir -p ${NAS_DATA_DIR} && cat > ${SECRETS_FILE} << 'SECRETS_EOF'
REDIS_PASSWORD=${redis_password}
C3PO_PROXY_BEARER_TOKEN=${proxy_bearer_token}
C3PO_SERVER_SECRET=${server_secret}
C3PO_ADMIN_KEY=${admin_key}
SECRETS_EOF
chmod 600 ${SECRETS_FILE}"

        log "Secrets generated and stored at ${SECRETS_FILE}"
        warn "Admin key: ${admin_key}"
        warn "Save the admin key — it is needed for client enrollment."
    fi
}

# Deploy containers on NAS
cmd_deploy() {
    log "Deploying to NAS..."

    # Create data directory
    ssh "$NAS_HOST" "mkdir -p ${NAS_DATA_DIR}/redis"

    # Ensure secrets exist
    ensure_secrets

    # Load secrets (don't log them)
    local secrets_content
    secrets_content=$(ssh "$NAS_HOST" "cat ${SECRETS_FILE}")
    local redis_password proxy_bearer_token server_secret admin_key
    redis_password=$(echo "$secrets_content" | grep REDIS_PASSWORD | cut -d= -f2 || true)
    proxy_bearer_token=$(echo "$secrets_content" | grep C3PO_PROXY_BEARER_TOKEN | cut -d= -f2 || true)
    server_secret=$(echo "$secrets_content" | grep C3PO_SERVER_SECRET | cut -d= -f2 || true)
    admin_key=$(echo "$secrets_content" | grep C3PO_ADMIN_KEY | cut -d= -f2 || true)

    # Stop existing containers
    ssh "$NAS_HOST" "${NAS_DOCKER} stop c3po-coordinator c3po-redis 2>/dev/null || true"
    ssh "$NAS_HOST" "${NAS_DOCKER} rm c3po-coordinator c3po-redis 2>/dev/null || true"

    # Create network if not exists
    ssh "$NAS_HOST" "${NAS_DOCKER} network create c3po-net 2>/dev/null || true"

    # Start Redis
    log "Starting Redis..."
    # Note: Not using --appendonly to avoid permission issues on Synology NAS
    # Data persistence is not critical for the coordinator
    ssh "$NAS_HOST" "${NAS_DOCKER} run -d \
        --name c3po-redis \
        --network c3po-net \
        --restart unless-stopped \
        -p ${REDIS_PORT}:6379 \
        -e REDIS_PASSWORD='${redis_password}' \
        redis:7-alpine \
        redis-server ${redis_password:+--requirepass '${redis_password}'}"

    # Start Coordinator
    log "Starting Coordinator..."
    local redis_url="redis://c3po-redis:6379"
    if [[ -n "$redis_password" ]]; then
        redis_url="redis://:${redis_password}@c3po-redis:6379"
    fi

    ssh "$NAS_HOST" "${NAS_DOCKER} run -d \
        --name c3po-coordinator \
        --network c3po-net \
        --restart unless-stopped \
        -e REDIS_URL='${redis_url}' \
        -e C3PO_PROXY_BEARER_TOKEN='${proxy_bearer_token}' \
        -e C3PO_SERVER_SECRET='${server_secret}' \
        -e C3PO_ADMIN_KEY='${admin_key}' \
        -p ${COORDINATOR_PORT}:8420 \
        c3po-coordinator:latest"

    log "Deployment complete"
    log "Coordinator available at: http://mkansel-nas.home.qerk.be:${COORDINATOR_PORT}"

    # Show status
    sleep 2
    cmd_status
}

# Show logs
cmd_logs() {
    local follow="${1:-}"
    if [[ "$follow" == "-f" ]]; then
        ssh "$NAS_HOST" "${NAS_DOCKER} logs -f c3po-coordinator"
    else
        ssh "$NAS_HOST" "${NAS_DOCKER} logs --tail 50 c3po-coordinator"
    fi
}

# Show status
cmd_status() {
    log "Container status:"
    ssh "$NAS_HOST" "${NAS_DOCKER} ps --filter name=c3po --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'"

    echo ""
    log "Health check:"
    if curl -s --connect-timeout 2 "http://mkansel-nas.home.qerk.be:${COORDINATOR_PORT}/api/health" 2>/dev/null; then
        echo ""
    else
        warn "Coordinator not responding (may still be starting)"
    fi
}

# Stop containers
cmd_stop() {
    log "Stopping containers..."
    ssh "$NAS_HOST" "${NAS_DOCKER} stop c3po-coordinator c3po-redis 2>/dev/null || true"
    log "Stopped"
}

# SSH shell
cmd_shell() {
    ssh "$NAS_HOST"
}

# Full deployment
cmd_full() {
    cmd_build
    cmd_deploy
}

# Main
case "${1:-}" in
    build)  cmd_build ;;
    push)   cmd_push ;;
    deploy) cmd_deploy ;;
    logs)   cmd_logs "${2:-}" ;;
    status) cmd_status ;;
    stop)   cmd_stop ;;
    shell)  cmd_shell ;;
    full)   cmd_full ;;
    *)      usage ;;
esac
