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

# Step 4.5: Verify plugin structure (hooks and skills exist)
log "Step 4.5: Verifying plugin structure..."
PLUGIN_CACHE=$(ls -d ~/.claude/plugins/cache/*/c3po/*/  2>/dev/null | head -1)
if [ -z "$PLUGIN_CACHE" ]; then
    fail "Plugin cache directory not found"
fi
log "Plugin cache: $PLUGIN_CACHE"

# Check setup.py exists (for --init hook)
if [ ! -f "$PLUGIN_CACHE/setup.py" ]; then
    fail "setup.py not found in plugin - claude --init won't work"
fi
log "setup.py found"

# Check hooks.json exists and has Setup hook
if [ ! -f "$PLUGIN_CACHE/hooks/hooks.json" ]; then
    fail "hooks/hooks.json not found in plugin"
fi
if ! grep -q '"Setup"' "$PLUGIN_CACHE/hooks/hooks.json"; then
    fail "Setup hook not defined in hooks.json"
fi
log "Setup hook configured"

# Check coordinate skill exists
if [ ! -f "$PLUGIN_CACHE/skills/coordinate/SKILL.md" ]; then
    fail "coordinate skill not found in plugin"
fi
log "coordinate skill found"

# Step 5: Test setup.py directly (non-interactive mode should exit cleanly)
log "Step 5: Testing setup.py (non-interactive mode)..."
SETUP_OUTPUT=$(python3 "$PLUGIN_CACHE/setup.py" 2>&1) || true
echo "$SETUP_OUTPUT"
if echo "$SETUP_OUTPUT" | grep -qi "error\|traceback\|exception"; then
    fail "setup.py has errors"
fi
log "setup.py runs without errors"

# Step 6: Configure MCP server manually (simulating what setup.py would do)
log "Step 6: Configuring MCP server..."
claude mcp add c3po "$COORDINATOR_URL/mcp" -t http -s user -H "X-Agent-ID: $AGENT_ID" || fail "Failed to add MCP server"
log "MCP server configured"

# Step 7: Verify MCP server is configured
log "Step 7: Verifying MCP configuration..."
MCP_LIST=$(claude mcp list 2>&1)
echo "$MCP_LIST"

if ! echo "$MCP_LIST" | grep -q "c3po"; then
    fail "c3po MCP server not found after configuration"
fi
log "MCP server is configured"

# Step 8: Verify MCP connection is healthy
log "Step 8: Verifying MCP connection is healthy..."
# The 'claude mcp list' output includes a health check - look for "✓ Connected"
if ! echo "$MCP_LIST" | grep -q "Connected"; then
    fail "MCP server not connected - health check failed"
fi
log "MCP connection verified (health check passed)"

# Step 9 (optional): Test MCP tools via Claude API
if [ -n "$ANTHROPIC_API_KEY" ]; then
    log "Step 9: Testing MCP tools via Claude API..."
    RESULT=$(echo "Use the list_agents tool and tell me what agents are online" | claude -p --allowedTools "mcp__c3po__list_agents" 2>&1) || true
    echo "$RESULT"

    if echo "$RESULT" | grep -qi "error\|failed\|unavailable"; then
        fail "MCP tools test failed - check output above"
    fi
    log "MCP tools work via Claude API"
else
    log "Step 9: Skipped (ANTHROPIC_API_KEY not set - MCP tools test requires API access)"
fi

log "=== Plugin installation test completed ==="
