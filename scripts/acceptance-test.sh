#!/bin/bash
set -e

# C3PO Acceptance Test
# Runs a full end-to-end test in isolated containers

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

# Use a different port to avoid conflicts with local development
TEST_PORT="${C3PO_TEST_PORT:-18420}"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log() { echo -e "${GREEN}[test]${NC} $1"; }
warn() { echo -e "${YELLOW}[test]${NC} $1"; }
error() { echo -e "${RED}[test]${NC} $1" >&2; }

# Cleanup function
cleanup() {
    log "Cleaning up containers..."
    finch rm -f c3po-coordinator c3po-agent-a c3po-agent-b c3po-plugin-test 2>/dev/null || true
    finch rm -f c3po-redis 2>/dev/null || true
    finch network rm c3po-test-net 2>/dev/null || true
}

# Set trap for cleanup
trap cleanup EXIT

# Parse args
SKIP_CLEANUP=false
while [[ $# -gt 0 ]]; do
    case $1 in
        --no-cleanup) SKIP_CLEANUP=true; shift ;;
        *) error "Unknown option: $1"; exit 1 ;;
    esac
done

if $SKIP_CLEANUP; then
    trap - EXIT
fi

log "=== C3PO Acceptance Test ==="
log "Repository: $REPO_ROOT"
log "Test port: $TEST_PORT"

# Step 1: Create network
log "Creating test network..."
finch network create c3po-test-net 2>/dev/null || true

# Step 2: Start Redis
log "Starting Redis..."
finch run -d \
    --name c3po-redis \
    --network c3po-test-net \
    redis:7-alpine

# Wait for Redis to be ready
log "Waiting for Redis to be ready..."
for i in {1..30}; do
    if finch exec c3po-redis redis-cli ping 2>/dev/null | grep -q "PONG"; then
        log "Redis is ready!"
        break
    fi
    if [ $i -eq 30 ]; then
        error "Redis failed to start"
        finch logs c3po-redis
        exit 1
    fi
    sleep 1
done

# Step 3: Start coordinator
log "Starting coordinator..."
finch run -d \
    --name c3po-coordinator \
    --network c3po-test-net \
    -p "$TEST_PORT:8420" \
    -e REDIS_URL=redis://c3po-redis:6379 \
    -v "$REPO_ROOT/coordinator:/app/coordinator:ro" \
    -w /app \
    python:3.12-slim \
    bash -c "pip install --quiet fastmcp redis uvicorn && python -m coordinator.server"

# Wait for coordinator to be ready
log "Waiting for coordinator to be ready..."
for i in {1..60}; do
    if curl -s http://localhost:$TEST_PORT/api/health 2>/dev/null | grep -q '"status":"ok"'; then
        log "Coordinator is ready!"
        break
    fi
    if [ $i -eq 60 ]; then
        error "Coordinator failed to start"
        finch logs c3po-coordinator
        exit 1
    fi
    sleep 1
done

# Step 4: Build agent image
log "Building agent test image..."
finch build -t c3po-agent-test -f "$REPO_ROOT/tests/acceptance/Dockerfile.agent" "$REPO_ROOT"

# Step 5: Start Agent A
log "Starting Agent A..."
finch run -d \
    --name c3po-agent-a \
    --network c3po-test-net \
    -e C3PO_COORDINATOR_URL=http://c3po-coordinator:8420 \
    -e C3PO_AGENT_ID=agent-a \
    c3po-agent-test

# Step 6: Start Agent B
log "Starting Agent B..."
finch run -d \
    --name c3po-agent-b \
    --network c3po-test-net \
    -e C3PO_COORDINATOR_URL=http://c3po-coordinator:8420 \
    -e C3PO_AGENT_ID=agent-b \
    c3po-agent-test

# Step 7: Wait for agents to register
log "Waiting for agents to register..."
sleep 5

# Step 8: Verify both agents are online
log "Verifying agents are registered..."
AGENTS=$(curl -s http://localhost:$TEST_PORT/api/health)
echo "$AGENTS"

if ! echo "$AGENTS" | grep -q '"agents_online":2'; then
    warn "Expected 2 agents online, checking agent list..."
    curl -s -X POST http://localhost:$TEST_PORT/mcp \
        -H "Content-Type: application/json" \
        -H "Accept: application/json, text/event-stream" \
        -H "X-Agent-ID: test" \
        -H "X-Project-Name: acceptance-test" \
        -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"list_agents","arguments":{}}}'
    echo
    warn "Agent A logs:"
    finch logs c3po-agent-a 2>&1 | tail -20
    warn "Agent B logs:"
    finch logs c3po-agent-b 2>&1 | tail -20
fi

# Step 9: Run communication test (runs in agent container to use MCP client)
log "Running agent communication test..."
finch run --rm \
    --network c3po-test-net \
    -e C3PO_COORDINATOR_URL=http://c3po-coordinator:8420 \
    -v "$REPO_ROOT/tests/acceptance:/tests:ro" \
    c3po-agent-test \
    python /tests/test-communication.py

log "=== Coordinator tests passed! ==="

# Step 10: Run plugin installation test
log "Building plugin test image..."
finch build -t c3po-plugin-test -f "$REPO_ROOT/tests/acceptance/Dockerfile.plugin-test" "$REPO_ROOT"

log "Running plugin installation test..."
# Note: ANTHROPIC_API_KEY is optional - if not set, the MCP connection test is skipped
finch run --rm \
    --name c3po-plugin-test \
    --network c3po-test-net \
    -e C3PO_COORDINATOR_URL=http://c3po-coordinator:8420 \
    -e C3PO_AGENT_ID=plugin-test-agent \
    ${ANTHROPIC_API_KEY:+-e ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY"} \
    c3po-plugin-test

log "=== All tests passed! ==="
