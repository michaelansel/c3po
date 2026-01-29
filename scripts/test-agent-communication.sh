#!/bin/bash
set -e

# Test that Agent A can send a message to Agent B and receive a response

TEST_PORT="${C3PO_TEST_PORT:-18420}"
COORDINATOR="http://localhost:$TEST_PORT"

log() { echo -e "\033[0;32m[comm-test]\033[0m $1"; }
error() { echo -e "\033[0;31m[comm-test]\033[0m $1" >&2; }

# Helper to call MCP tools
mcp_call() {
    local agent_id=$1
    local tool=$2
    local args=$3

    curl -s -X POST "$COORDINATOR/mcp" \
        -H "Content-Type: application/json" \
        -H "Accept: application/json, text/event-stream" \
        -H "X-Agent-ID: $agent_id" \
        -d "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/call\",\"params\":{\"name\":\"$tool\",\"arguments\":$args}}"
}

# Test 1: Verify both agents can list each other
log "Test 1: Listing agents from agent-a..."
RESULT=$(mcp_call "agent-a" "list_agents" "{}")
echo "$RESULT"

if ! echo "$RESULT" | grep -q "agent-b"; then
    error "Agent A cannot see Agent B"
    exit 1
fi
log "✓ Agent A can see Agent B"

# Test 2: Agent A sends request to Agent B
log "Test 2: Agent A sending request to Agent B..."
RESULT=$(mcp_call "agent-a" "send_request" '{"target":"agent-b","message":"What is 2+2?"}')
echo "$RESULT"

REQUEST_ID=$(echo "$RESULT" | grep -o '"id":"[^"]*"' | head -1 | cut -d'"' -f4)
if [ -z "$REQUEST_ID" ]; then
    error "Failed to get request ID"
    exit 1
fi
log "✓ Request sent with ID: $REQUEST_ID"

# Test 3: Agent B receives the request
log "Test 3: Agent B checking pending requests..."
RESULT=$(mcp_call "agent-b" "get_pending_requests" "{}")
echo "$RESULT"

if ! echo "$RESULT" | grep -q "What is 2+2"; then
    error "Agent B did not receive the request"
    exit 1
fi
log "✓ Agent B received the request"

# Test 4: Agent B responds
log "Test 4: Agent B responding..."
RESULT=$(mcp_call "agent-b" "respond_to_request" "{\"request_id\":\"$REQUEST_ID\",\"response\":\"The answer is 4\"}")
echo "$RESULT"

if ! echo "$RESULT" | grep -q "success"; then
    error "Agent B failed to respond"
    exit 1
fi
log "✓ Agent B sent response"

# Test 5: Agent A receives the response
log "Test 5: Agent A waiting for response..."
RESULT=$(mcp_call "agent-a" "wait_for_response" "{\"request_id\":\"$REQUEST_ID\",\"timeout\":10}")
echo "$RESULT"

if ! echo "$RESULT" | grep -q "The answer is 4"; then
    error "Agent A did not receive the response"
    exit 1
fi
log "✓ Agent A received response: 'The answer is 4'"

log "=== Communication test passed! ==="
