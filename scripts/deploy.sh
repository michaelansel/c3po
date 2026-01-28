#!/bin/bash
set -euo pipefail

# C3PO Deployment Script
# Deploys coordinator and Redis to Synology NAS

# Configuration
NAS_HOST="${NAS_HOST:-admin@mkansel-nas.home.qerk.be}"
NAS_DATA_DIR="${NAS_DATA_DIR:-/volume1/enc-containers/c3po}"
COORDINATOR_PORT="${COORDINATOR_PORT:-8420}"
REDIS_PORT="${REDIS_PORT:-6380}"  # Non-standard to avoid conflicts

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

# Build the coordinator image locally using finch (Docker alternative on macOS)
cmd_build() {
    log "Building coordinator image..."
    cd "$PROJECT_DIR/coordinator"

    if command -v finch &>/dev/null; then
        finch build -t c3po-coordinator:latest .
    elif command -v docker &>/dev/null; then
        docker build -t c3po-coordinator:latest .
    else
        error "Neither finch nor docker found"
        exit 1
    fi

    log "Build complete"
}

# Save and copy image to NAS
cmd_push() {
    log "Saving image to tarball..."
    local tmpfile="/tmp/c3po-coordinator.tar"

    if command -v finch &>/dev/null; then
        finch save c3po-coordinator:latest -o "$tmpfile"
    else
        docker save c3po-coordinator:latest -o "$tmpfile"
    fi

    log "Copying to NAS..."
    scp "$tmpfile" "${NAS_HOST}:/tmp/c3po-coordinator.tar"

    log "Loading image on NAS..."
    ssh "$NAS_HOST" "docker load -i /tmp/c3po-coordinator.tar && rm /tmp/c3po-coordinator.tar"

    rm -f "$tmpfile"
    log "Push complete"
}

# Deploy containers on NAS
cmd_deploy() {
    log "Deploying to NAS..."

    # Create data directory
    ssh "$NAS_HOST" "mkdir -p ${NAS_DATA_DIR}/redis"

    # Stop existing containers
    ssh "$NAS_HOST" "docker stop c3po-coordinator c3po-redis 2>/dev/null || true"
    ssh "$NAS_HOST" "docker rm c3po-coordinator c3po-redis 2>/dev/null || true"

    # Create network if not exists
    ssh "$NAS_HOST" "docker network create c3po-net 2>/dev/null || true"

    # Start Redis
    log "Starting Redis..."
    ssh "$NAS_HOST" "docker run -d \
        --name c3po-redis \
        --network c3po-net \
        --restart unless-stopped \
        -v ${NAS_DATA_DIR}/redis:/data \
        -p ${REDIS_PORT}:6379 \
        redis:7-alpine \
        redis-server --appendonly yes"

    # Start Coordinator
    log "Starting Coordinator..."
    ssh "$NAS_HOST" "docker run -d \
        --name c3po-coordinator \
        --network c3po-net \
        --restart unless-stopped \
        -e REDIS_URL=redis://c3po-redis:6379 \
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
        ssh "$NAS_HOST" "docker logs -f c3po-coordinator"
    else
        ssh "$NAS_HOST" "docker logs --tail 50 c3po-coordinator"
    fi
}

# Show status
cmd_status() {
    log "Container status:"
    ssh "$NAS_HOST" "docker ps --filter name=c3po --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'"

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
    ssh "$NAS_HOST" "docker stop c3po-coordinator c3po-redis 2>/dev/null || true"
    log "Stopped"
}

# SSH shell
cmd_shell() {
    ssh "$NAS_HOST"
}

# Full deployment
cmd_full() {
    cmd_build
    cmd_push
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
