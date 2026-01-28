#!/bin/bash
set -euo pipefail

# C3PO End-to-End Test Script
# Validates REST API endpoints and provides manual test instructions for MCP tools

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

COORDINATOR_PORT="${COORDINATOR_PORT:-8420}"
REDIS_PORT="${REDIS_PORT:-6379}"
BASE_URL="http://localhost:${COORDINATOR_PORT}"

# Colors
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

pass() { echo -e "${GREEN}✓${NC} $1"; }
fail() { echo -e "${RED}✗${NC} $1"; FAILURES=$((FAILURES + 1)); }
info() { echo -e "${YELLOW}→${NC} $1"; }
manual() { echo -e "${CYAN}📋${NC} $1"; }

FAILURES=0

# Check if jq is available
if ! command -v jq &>/dev/null; then
    echo "Error: jq is required but not installed"
    exit 1
fi

echo "================================"
echo "C3PO End-to-End Tests"
echo "================================"
echo ""

# --- Test 1: Health Check ---
info "Test 1: Health check endpoint"
HEALTH=$(curl -s "${BASE_URL}/api/health" 2>/dev/null || echo '{"error":"not reachable"}')
if echo "$HEALTH" | jq -e '.status == "ok"' >/dev/null 2>&1; then
    pass "Coordinator is healthy"
    AGENTS_ONLINE=$(echo "$HEALTH" | jq -r '.agents_online')
    echo "      agents_online: $AGENTS_ONLINE"
else
    fail "Coordinator health check failed: $HEALTH"
    echo ""
    echo "Make sure to start the test environment first:"
    echo "  ./scripts/test-local.sh start"
    exit 1
fi

# --- Test 2: REST API - Pending endpoint with no agent ---
info "Test 2: /api/pending requires X-Agent-ID header"
PENDING_NO_HEADER=$(curl -s "${BASE_URL}/api/pending")
if echo "$PENDING_NO_HEADER" | jq -e '.error' >/dev/null 2>&1; then
    pass "Missing X-Agent-ID returns error"
else
    fail "Expected error for missing header: $PENDING_NO_HEADER"
fi

# --- Test 3: REST API - Pending endpoint with unknown agent ---
info "Test 3: /api/pending with unknown agent returns empty"
PENDING_UNKNOWN=$(curl -s "${BASE_URL}/api/pending" -H "X-Agent-ID: unknown-test-agent")
PENDING_COUNT=$(echo "$PENDING_UNKNOWN" | jq -r '.count // -1')
if [[ "$PENDING_COUNT" == "0" ]]; then
    pass "Unknown agent returns count: 0"
else
    fail "Unexpected result: $PENDING_UNKNOWN"
fi

# --- Summary for automated tests ---
echo ""
echo "================================"
echo "Automated REST API Tests"
if [[ $FAILURES -eq 0 ]]; then
    echo -e "${GREEN}All automated tests passed!${NC}"
else
    echo -e "${RED}$FAILURES test(s) failed${NC}"
fi
echo "================================"
echo ""

# --- Manual MCP Tool Tests ---
echo "================================"
echo "Manual MCP Tool Tests"
echo "================================"
echo ""
echo "MCP tools require session management and cannot be tested via simple HTTP."
echo "Use the following manual test procedure with two Claude Code instances:"
echo ""

manual "SETUP:"
echo "  Terminal 1: ./scripts/test-local.sh start"
echo "  Terminal 2: ./scripts/test-local.sh agent-a"
echo "  Terminal 3: ./scripts/test-local.sh agent-b"
echo ""

manual "TEST 1: Simple Request/Response"
echo "  In agent-a's Claude:"
echo "    1. Use list_agents tool - should see agent-a and agent-b"
echo "    2. Use send_request(target='agent-b', message='Hello!')"
echo "    3. Note the request_id"
echo "    4. Use wait_for_response(request_id=..., timeout=30)"
echo ""
echo "  In agent-b's Claude:"
echo "    1. Use get_pending_requests - should see the request"
echo "    2. Use respond_to_request(request_id=..., response='Hi back!')"
echo ""
echo "  Expected: agent-a receives the response"
echo ""

manual "TEST 2: Multi-Turn Conversation"
echo "  1. agent-a: send_request → agent-b"
echo "  2. agent-b: get_pending_requests → respond_to_request"
echo "  3. agent-a: wait_for_response → send_request (follow-up)"
echo "  4. agent-b: get_pending_requests → respond_to_request"
echo "  Expected: Both exchanges complete successfully"
echo ""

manual "TEST 3: Timeout Behavior"
echo "  1. agent-a: send_request to agent-b"
echo "  2. agent-a: wait_for_response(timeout=10)"
echo "  3. Do NOT respond from agent-b"
echo "  Expected: wait_for_response returns with status='timeout'"
echo ""

manual "TEST 4: Stop Hook"
echo "  1. Configure c3po plugin with hooks enabled"
echo "  2. agent-a: send_request to agent-b"
echo "  3. In agent-b: complete a task and let Claude try to stop"
echo "  Expected: Stop hook blocks and instructs Claude to process pending requests"
echo ""

manual "TEST 5: Graceful Degradation"
echo "  1. ./scripts/test-local.sh stop"
echo "  2. Try c3po tools in agent-a"
echo "  Expected: Clear error message, Claude continues working"
echo "  3. ./scripts/test-local.sh start"
echo "  4. Try c3po tools again"
echo "  Expected: Tools work again without restart"
echo ""

echo "For detailed test procedures, see: tests/TESTING.md"
echo ""

exit $FAILURES
