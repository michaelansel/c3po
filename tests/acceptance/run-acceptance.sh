#!/bin/bash
set -e

# C3PO Acceptance Test Runner
# Orchestrates coordinator + redis + test agent containers, then runs
# the acceptance test phases defined in ACCEPTANCE_SPEC.md.
#
# Supports both docker and finch (finch lacks healthchecks/TTY in compose,
# so we use manual container management for compatibility).

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# Use finch if docker is not available
if command -v docker &>/dev/null; then
    DOCKER=docker
elif command -v finch &>/dev/null; then
    DOCKER=finch
else
    echo "Error: neither docker nor finch found" >&2
    exit 1
fi

# Network and container names
NET="c3po-accept-test"
REDIS_CTR="c3po-accept-redis"
COORD_CTR="c3po-accept-coordinator"
HOSTA_CTR="c3po-accept-host-a"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

log()   { echo -e "${GREEN}[acceptance]${NC} $1"; }
warn()  { echo -e "${YELLOW}[acceptance]${NC} $1"; }
error() { echo -e "${RED}[acceptance]${NC} $1" >&2; }
phase() { echo -e "\n${CYAN}=== Phase $1: $2 ===${NC}"; }

# Cleanup on exit
cleanup() {
    if [ "$SKIP_CLEANUP" = "true" ]; then
        warn "Skipping cleanup (--no-cleanup). Containers still running."
        return
    fi
    log "Tearing down environment..."
    $DOCKER rm -f "$HOSTA_CTR" "$COORD_CTR" "$REDIS_CTR" 2>/dev/null || true
    $DOCKER network rm "$NET" 2>/dev/null || true
}
trap cleanup EXIT

# Parse args
SKIP_CLEANUP=false
PHASES=""
while [[ $# -gt 0 ]]; do
    case $1 in
        --no-cleanup) SKIP_CLEANUP=true; shift ;;
        --phase) PHASES="$PHASES --phase $2"; shift 2 ;;
        *) error "Unknown option: $1"; exit 1 ;;
    esac
done

log "=== C3PO Acceptance Test ==="
log "Repository: $REPO_ROOT"
log "Docker runtime: $DOCKER"

# -------------------------------------------------------------------
# Phase 0: Start environment
# -------------------------------------------------------------------
phase 0 "Prerequisites"

# Create network
log "Creating test network..."
$DOCKER network create "$NET" 2>/dev/null || true

# Start Redis
log "Starting Redis..."
$DOCKER run -d \
    --name "$REDIS_CTR" \
    --network "$NET" \
    redis:7-alpine \
    redis-server --appendonly no

# Wait for Redis
log "Waiting for Redis..."
for i in $(seq 1 30); do
    if $DOCKER exec "$REDIS_CTR" redis-cli ping 2>/dev/null | grep -q PONG; then
        log "Redis is ready"
        break
    fi
    if [ "$i" -eq 30 ]; then
        error "Redis failed to start"
        exit 1
    fi
    sleep 1
done

# Build coordinator image
log "Building coordinator image..."
$DOCKER build -t c3po-accept-coordinator \
    -f "$REPO_ROOT/coordinator/Dockerfile" \
    "$REPO_ROOT/coordinator" 2>&1 | tail -5

# Start coordinator
log "Starting coordinator..."
$DOCKER run -d \
    --name "$COORD_CTR" \
    --network "$NET" \
    -e "REDIS_URL=redis://$REDIS_CTR:6379" \
    c3po-accept-coordinator

# Wait for coordinator health
log "Waiting for coordinator..."
for i in $(seq 1 60); do
    if $DOCKER exec "$COORD_CTR" python -c \
        "import urllib.request; urllib.request.urlopen('http://localhost:8420/api/health')" 2>/dev/null; then
        log "Coordinator is ready"
        break
    fi
    if [ "$i" -eq 60 ]; then
        error "Coordinator failed to start"
        $DOCKER logs "$COORD_CTR" 2>&1 | tail -20
        exit 1
    fi
    sleep 2
done

# Build host image (using Dockerfile.cc-agent)
log "Building host agent image..."
$DOCKER build -t c3po-accept-host \
    -f "$REPO_ROOT/tests/acceptance/Dockerfile.cc-agent" \
    "$REPO_ROOT" 2>&1 | tail -5

# Start host-a
log "Starting host-a..."
$DOCKER run -d \
    --name "$HOSTA_CTR" \
    --network "$NET" \
    -e "C3PO_COORDINATOR_URL=http://$COORD_CTR:8420" \
    c3po-accept-host

# Verify coordinator reachable from host-a
log "Verifying coordinator reachable from host-a..."
for i in $(seq 1 10); do
    if $DOCKER exec "$HOSTA_CTR" curl -sf "http://$COORD_CTR:8420/api/health" 2>/dev/null; then
        echo
        log "Phase 0: PASSED"
        break
    fi
    if [ "$i" -eq 10 ]; then
        error "Coordinator not reachable from host-a"
        exit 1
    fi
    sleep 2
done

# -------------------------------------------------------------------
# Install test dependencies and run acceptance tests in host-a
# -------------------------------------------------------------------
log "Installing test dependencies in host-a..."
$DOCKER exec "$HOSTA_CTR" pip install --quiet --break-system-packages mcp httpx httpx-sse 2>&1 | tail -3

log "Copying acceptance test into host-a..."
$DOCKER cp "$SCRIPT_DIR/test_acceptance.py" "$HOSTA_CTR":/tmp/test_acceptance.py

log "Running acceptance test phases..."
$DOCKER exec \
    -e "C3PO_COORDINATOR_URL=http://$COORD_CTR:8420" \
    "$HOSTA_CTR" \
    python3 /tmp/test_acceptance.py $PHASES

EXIT_CODE=$?

if [ $EXIT_CODE -eq 0 ]; then
    log "=== All acceptance tests PASSED ==="
else
    error "=== Some acceptance tests FAILED ==="
fi

exit $EXIT_CODE
