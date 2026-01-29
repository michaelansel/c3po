#!/bin/bash
set -e

# C3PO Plugin Installation Acceptance Test
# Tests the complete plugin installation workflow using Claude Code CLI
# Must be run inside a clean container with Claude Code installed

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log() { echo -e "${GREEN}[plugin-test]${NC} $1"; }
warn() { echo -e "${YELLOW}[plugin-test]${NC} $1"; }
error() { echo -e "${RED}[plugin-test]${NC} $1" >&2; }
fail() { error "$1"; exit 1; }

COORDINATOR_URL="${C3PO_COORDINATOR_URL:-http://c3po-coordinator:8420}"
AGENT_ID="${C3PO_AGENT_ID:-plugin-test-agent}"

log "=== C3PO Plugin Installation Test ==="
log "Coordinator URL: $COORDINATOR_URL"
log "Agent ID: $AGENT_ID"

# Step 0: Verify we're in a clean state
log "Step 0: Verifying clean state..."
if claude mcp list 2>&1 | grep -q "c3po"; then
    fail "c3po MCP server already exists - not a clean environment!"
fi
log "Clean state verified"

# Step 0.5: Verify coordinator is reachable
log "Step 0.5: Verifying coordinator is reachable..."
if ! curl -sf "$COORDINATOR_URL/api/health" | grep -q '"status":"ok"'; then
    fail "Coordinator not reachable at $COORDINATOR_URL"
fi
log "Coordinator is reachable"

# Step 1: Add marketplace
log "Step 1: Adding marketplace..."
claude plugin marketplace add michaelansel/c3po || fail "Failed to add marketplace"
log "Marketplace added"

# Step 2: Install plugin
log "Step 2: Installing plugin..."
claude plugin install c3po || fail "Failed to install plugin"
log "Plugin installed"

# Step 3: Verify plugin is listed
log "Step 3: Verifying plugin is installed..."
PLUGIN_LIST=$(claude plugin list 2>&1)
echo "$PLUGIN_LIST"

if ! echo "$PLUGIN_LIST" | grep -qi "c3po"; then
    fail "c3po plugin not found in plugin list"
fi

if echo "$PLUGIN_LIST" | grep -qi "error\|invalid"; then
    fail "Plugin has errors: $PLUGIN_LIST"
fi

if echo "$PLUGIN_LIST" | grep -qi "duplicate hooks"; then
    fail "Duplicate hooks error detected"
fi
log "Plugin is installed and listed"

# Step 4: Verify no MCP server pre-configured
log "Step 4: Verifying no pre-configured MCP server..."
MCP_LIST=$(claude mcp list 2>&1)
if echo "$MCP_LIST" | grep -q "c3po"; then
    fail "c3po MCP server was auto-configured - this is a bug!"
fi
log "No pre-configured MCP server"

# Step 5: Configure MCP server manually (simulating /coordinate setup)
log "Step 5: Configuring MCP server..."
claude mcp add c3po "$COORDINATOR_URL/mcp" -t http -s user -H "X-Agent-ID: $AGENT_ID" || fail "Failed to add MCP server"
log "MCP server configured"

# Step 6: Verify MCP server is configured
log "Step 6: Verifying MCP configuration..."
MCP_LIST=$(claude mcp list 2>&1)
echo "$MCP_LIST"

if ! echo "$MCP_LIST" | grep -q "c3po"; then
    fail "c3po MCP server not found after configuration"
fi
log "MCP server is configured"

# Step 7: Test MCP connection by calling list_agents
log "Step 7: Testing MCP connection..."
# Check if we have an API key before trying to use Claude
if [ -z "$ANTHROPIC_API_KEY" ]; then
    warn "ANTHROPIC_API_KEY not set - skipping MCP connection test"
    log "MCP configuration complete (connection test skipped)"
else
    # Use claude in non-interactive mode to test the MCP tools
    RESULT=$(echo "Use the list_agents tool and tell me what agents are online" | claude -p --allowedTools "mcp__c3po__list_agents" 2>&1) || true
    echo "$RESULT"

    if echo "$RESULT" | grep -qi "error\|failed\|unavailable"; then
        warn "MCP connection test had issues - check output above"
    else
        log "MCP connection works"
    fi
fi

log "=== Plugin installation test completed ==="
