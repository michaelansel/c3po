#!/bin/bash
set -euo pipefail

# C3PO Local Testing Script
# Sets up local coordinator and Redis for testing

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

COORDINATOR_PORT="${COORDINATOR_PORT:-8420}"
REDIS_PORT="${REDIS_PORT:-6379}"

# Colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log() { echo -e "${GREEN}[c3po-test]${NC} $1"; }
warn() { echo -e "${YELLOW}[c3po-test]${NC} $1"; }

usage() {
    cat <<EOF
Usage: $0 <command>

Commands:
    start       Start local Redis and coordinator
    stop        Stop local containers
    status      Show status
    logs        Show coordinator logs
    agent       Launch a test Claude Code agent

    agent-a     Launch agent "agent-a" in a temp directory
    agent-b     Launch agent "agent-b" in a temp directory

Environment:
    COORDINATOR_PORT  Coordinator port (default: 8420)
    REDIS_PORT        Redis port (default: 6379)

EOF
    exit 1
}

# Use finch if available, else docker
DOCKER_CMD="docker"
if command -v finch &>/dev/null; then
    DOCKER_CMD="finch"
fi

cmd_start() {
    log "Starting local test environment..."

    # Start Redis
    log "Starting Redis on port ${REDIS_PORT}..."
    $DOCKER_CMD run -d \
        --name c3po-test-redis \
        -p "${REDIS_PORT}:6379" \
        redis:7-alpine 2>/dev/null || {
            warn "Redis container already exists, restarting..."
            $DOCKER_CMD start c3po-test-redis
        }

    # Start coordinator (run in background)
    log "Starting coordinator on port ${COORDINATOR_PORT}..."
    cd "$PROJECT_DIR/coordinator"

    if [[ ! -f "requirements.txt" ]]; then
        warn "Coordinator not yet implemented. Create coordinator/requirements.txt and coordinator/server.py first."
        warn "Redis is running - you can develop against it."
    else
        # Check if venv exists
        if [[ ! -d ".venv" ]]; then
            log "Creating virtual environment..."
            python3 -m venv .venv
            .venv/bin/pip install -r requirements.txt
        fi

        # Run coordinator in background
        export REDIS_URL="redis://localhost:${REDIS_PORT}"
        nohup .venv/bin/python server.py > /tmp/c3po-coordinator.log 2>&1 &
        echo $! > /tmp/c3po-coordinator.pid
        log "Coordinator PID: $(cat /tmp/c3po-coordinator.pid)"
    fi

    log "Local environment ready"
    log "Coordinator: http://localhost:${COORDINATOR_PORT}"
    log "Redis: localhost:${REDIS_PORT}"
}

cmd_stop() {
    log "Stopping local test environment..."

    # Stop coordinator
    if [[ -f /tmp/c3po-coordinator.pid ]]; then
        kill "$(cat /tmp/c3po-coordinator.pid)" 2>/dev/null || true
        rm -f /tmp/c3po-coordinator.pid
    fi

    # Stop Redis
    $DOCKER_CMD stop c3po-test-redis 2>/dev/null || true
    $DOCKER_CMD rm c3po-test-redis 2>/dev/null || true

    log "Stopped"
}

cmd_status() {
    log "Redis:"
    $DOCKER_CMD ps --filter name=c3po-test-redis --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}' 2>/dev/null || echo "Not running"

    echo ""
    log "Coordinator:"
    if [[ -f /tmp/c3po-coordinator.pid ]] && kill -0 "$(cat /tmp/c3po-coordinator.pid)" 2>/dev/null; then
        echo "Running (PID: $(cat /tmp/c3po-coordinator.pid))"
    else
        echo "Not running"
    fi

    echo ""
    log "Health check:"
    curl -s "http://localhost:${COORDINATOR_PORT}/api/health" 2>/dev/null || echo "Not responding"
    echo ""
}

cmd_logs() {
    if [[ -f /tmp/c3po-coordinator.log ]]; then
        tail -f /tmp/c3po-coordinator.log
    else
        warn "No log file found"
    fi
}

# Launch a Claude Code agent with c3po configured
cmd_agent() {
    local agent_name="${1:-test-agent}"
    local agent_dir="/tmp/c3po-test-${agent_name}"

    log "Setting up agent: ${agent_name}"
    mkdir -p "$agent_dir"

    # Set environment
    export C3PO_COORDINATOR_URL="http://localhost:${COORDINATOR_PORT}"
    export C3PO_AGENT_ID="${agent_name}"

    log "Starting Claude Code in ${agent_dir}..."
    log "Agent ID: ${agent_name}"
    log "Coordinator: ${C3PO_COORDINATOR_URL}"

    cd "$agent_dir"

    # Create a minimal project marker
    echo "# Test agent: ${agent_name}" > README.md

    # Launch Claude Code
    # Note: Plugin would need to be installed for full functionality
    claude
}

cmd_agent_a() {
    cmd_agent "agent-a"
}

cmd_agent_b() {
    cmd_agent "agent-b"
}

# Main
case "${1:-}" in
    start)    cmd_start ;;
    stop)     cmd_stop ;;
    status)   cmd_status ;;
    logs)     cmd_logs ;;
    agent)    cmd_agent "${2:-test-agent}" ;;
    agent-a)  cmd_agent_a ;;
    agent-b)  cmd_agent_b ;;
    *)        usage ;;
esac
