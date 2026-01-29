# C3PO Plugin Installation Acceptance Test

## Objective

Create an acceptance test that validates the **complete plugin installation and setup workflow** from a user's perspective. This test MUST use actual Claude Code CLI - not direct HTTP calls that bypass the plugin.

## CRITICAL REQUIREMENTS

1. **You MUST test the actual plugin installation flow** - not bypass it
2. **You MUST use Claude Code CLI** - not direct MCP HTTP calls
3. **You MUST run in a clean container** - no pre-existing configuration
4. **You MUST test each step a real user would follow**

The existing `scripts/acceptance-test.sh` tests the coordinator with direct HTTP calls. That is NOT what this task is about. This task is about testing the PLUGIN.

---

## What Must Be Tested

### Step 1: Plugin Marketplace Installation
```bash
# Inside Claude Code:
/plugin marketplace add michaelansel/c3po
/plugin install c3po
/plugin info c3po
```

**Verify:**
- No errors during installation
- Plugin shows as enabled
- Skills listed: `coordinate`
- Hooks listed: `Setup`, `SessionStart`, `Stop`, `SessionEnd`
- NO "duplicate hooks" error
- NO pre-configured MCP server (should only exist after setup)

### Step 2: MCP Configuration via Setup
```bash
# Inside Claude Code:
/coordinate setup
```

**Verify:**
- Skill is available and runs
- User is prompted for coordinator URL
- User is prompted for agent ID
- Connectivity test runs
- MCP server is configured via `claude mcp add`

**Alternative test:**
```bash
claude mcp add c3po http://coordinator:8420/mcp -t http -s user -H "X-Agent-ID: test-agent"
```

### Step 3: Verify MCP Connection Works
After restarting Claude Code:
```bash
# Check MCP server is configured
claude mcp list
# Should show c3po with correct URL

# Inside Claude Code, use the tools:
# - list_agents should work
# - ping should work
```

---

## Test Infrastructure

### Container Setup

Create `tests/acceptance/Dockerfile.plugin-test`:
```dockerfile
FROM node:20-bookworm

# Install system dependencies
RUN apt-get update && apt-get install -y \
    curl \
    git \
    python3 \
    && rm -rf /var/lib/apt/lists/*

# Install Claude Code CLI globally
RUN npm install -g @anthropic-ai/claude-code

# Create test user (Claude Code doesn't like running as root)
RUN useradd -m -s /bin/bash testuser
USER testuser
WORKDIR /home/testuser

# Verify clean state
RUN claude mcp list 2>&1 || echo "No MCP servers (expected)"
```

### Test Script

Create `tests/acceptance/test-plugin-install.sh`:
```bash
#!/bin/bash
set -e

# This script tests the plugin installation workflow
# It must be run inside a clean container with Claude Code installed

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

log() { echo -e "${GREEN}[plugin-test]${NC} $1"; }
error() { echo -e "${RED}[plugin-test]${NC} $1" >&2; }
fail() { error "$1"; exit 1; }

COORDINATOR_URL="${C3PO_COORDINATOR_URL:-http://c3po-coordinator:8420}"
AGENT_ID="${C3PO_AGENT_ID:-plugin-test-agent}"

# Verify we're in a clean state
log "Step 0: Verifying clean state..."
if claude mcp list 2>&1 | grep -q "c3po"; then
    fail "c3po MCP server already exists - not a clean environment!"
fi
log "✓ Clean state verified"

# Test coordinator is reachable
log "Step 0.5: Verifying coordinator is reachable..."
if ! curl -sf "$COORDINATOR_URL/api/health" | grep -q '"status":"ok"'; then
    fail "Coordinator not reachable at $COORDINATOR_URL"
fi
log "✓ Coordinator is reachable"

# Step 1: Add marketplace
log "Step 1: Adding marketplace..."
claude plugin marketplace add michaelansel/c3po || fail "Failed to add marketplace"
log "✓ Marketplace added"

# Step 2: Install plugin
log "Step 2: Installing plugin..."
claude plugin install c3po || fail "Failed to install plugin"
log "✓ Plugin installed"

# Step 3: Verify plugin info
log "Step 3: Verifying plugin info..."
PLUGIN_INFO=$(claude plugin info c3po 2>&1)
echo "$PLUGIN_INFO"

if echo "$PLUGIN_INFO" | grep -qi "error"; then
    fail "Plugin has errors: $PLUGIN_INFO"
fi

if echo "$PLUGIN_INFO" | grep -qi "duplicate hooks"; then
    fail "Duplicate hooks error detected"
fi

if ! echo "$PLUGIN_INFO" | grep -qi "coordinate"; then
    fail "coordinate skill not found in plugin"
fi
log "✓ Plugin info looks good"

# Step 4: Verify no MCP server pre-configured
log "Step 4: Verifying no pre-configured MCP server..."
MCP_LIST=$(claude mcp list 2>&1)
if echo "$MCP_LIST" | grep -q "c3po"; then
    fail "c3po MCP server was auto-configured - this is a bug!"
fi
log "✓ No pre-configured MCP server"

# Step 5: Configure MCP server manually (simulating /coordinate setup)
log "Step 5: Configuring MCP server..."
claude mcp add c3po "$COORDINATOR_URL/mcp" -t http -s user -H "X-Agent-ID: $AGENT_ID" || fail "Failed to add MCP server"
log "✓ MCP server configured"

# Step 6: Verify MCP server is configured
log "Step 6: Verifying MCP configuration..."
MCP_LIST=$(claude mcp list 2>&1)
echo "$MCP_LIST"

if ! echo "$MCP_LIST" | grep -q "c3po"; then
    fail "c3po MCP server not found after configuration"
fi
log "✓ MCP server is configured"

# Step 7: Test MCP connection by calling list_agents
log "Step 7: Testing MCP connection..."
# Use claude in non-interactive mode to test the MCP tools
RESULT=$(echo "Use the list_agents tool and tell me what agents are online" | claude -p --allowedTools "mcp__c3po__list_agents" 2>&1) || true
echo "$RESULT"

if echo "$RESULT" | grep -qi "error\|failed\|unavailable"; then
    warn "MCP connection test had issues - check output above"
else
    log "✓ MCP connection works"
fi

log "=== Plugin installation test completed ==="
```

### Main Test Runner

Update `scripts/acceptance-test.sh` to include plugin testing:
```bash
# After starting coordinator and verifying it works...

# Step N: Run plugin installation test
log "Running plugin installation test..."
finch run --rm \
    --network c3po-test-net \
    -e C3PO_COORDINATOR_URL=http://c3po-coordinator:8420 \
    -e C3PO_AGENT_ID=plugin-test-agent \
    -e ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" \
    c3po-plugin-test \
    /home/testuser/test-plugin-install.sh
```

---

## Files to Create/Modify

| File | Action | Purpose |
|------|--------|---------|
| `tests/acceptance/Dockerfile.plugin-test` | Create | Container with Claude Code CLI |
| `tests/acceptance/test-plugin-install.sh` | Create | Plugin installation test script |
| `scripts/acceptance-test.sh` | Modify | Add plugin test to existing test |

---

## Success Criteria

All of these must pass:

- [ ] Container builds with Claude Code CLI installed
- [ ] `claude plugin marketplace add michaelansel/c3po` succeeds
- [ ] `claude plugin install c3po` succeeds
- [ ] `claude plugin info c3po` shows no errors
- [ ] No "duplicate hooks" error
- [ ] No pre-configured MCP server after install
- [ ] `claude mcp add c3po ...` succeeds
- [ ] `claude mcp list` shows c3po configured
- [ ] MCP tools work (list_agents returns data)

---

## Debugging

If tests fail:

1. Build and run container interactively:
   ```bash
   finch build -t c3po-plugin-test -f tests/acceptance/Dockerfile.plugin-test .
   finch run -it --network c3po-test-net \
       -e C3PO_COORDINATOR_URL=http://c3po-coordinator:8420 \
       c3po-plugin-test bash
   ```

2. Run commands manually inside container to see exact errors

3. Check plugin cache location:
   ```bash
   ls -la ~/.claude/plugins/
   ```

4. Check MCP config:
   ```bash
   cat ~/.claude/settings.json
   ```

---

## Important Notes

- The test MUST use `claude` CLI commands, not direct HTTP calls
- The test MUST start from a clean state (no pre-existing config)
- The test MUST verify each step a real user would take
- If `/coordinate setup` skill doesn't work non-interactively, test the manual `claude mcp add` flow instead
- You need `ANTHROPIC_API_KEY` set for Claude Code to work
