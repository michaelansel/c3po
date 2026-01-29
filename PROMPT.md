# C3PO Plugin Installation Test Plan

## Objective

Test the complete plugin installation and setup flow in a **clean room environment** using a finch container. This ensures no pre-existing configuration interferes with testing.

## CRITICAL: Clean Room Requirement

**You MUST run all tests inside a fresh finch container.** Do NOT test on your host machine where MCP servers may already be configured.

### Phase 0: Set Up Clean Room Environment

**Step 0.1: Create the test container**

```bash
# Start a fresh Ubuntu container with Claude Code
finch run -it --name c3po-test ubuntu:24.04 bash
```

**Step 0.2: Install dependencies inside container**

```bash
# Inside the container:
apt-get update && apt-get install -y curl git nodejs npm

# Install Claude Code CLI
npm install -g @anthropic-ai/claude-code

# Verify clean state - this should show NO MCP servers configured
claude mcp list
# Expected: empty or "No MCP servers configured"

# Verify clean state - no plugins installed
ls -la ~/.claude/plugins/ 2>/dev/null || echo "No plugins directory (good)"
```

**Step 0.3: Verify network access to coordinator**

The coordinator is running at `http://host.finch.internal:8420` (accessible from inside container).

```bash
curl http://host.finch.internal:8420/api/health
# Expected: {"status":"ok","agents_online":N}
```

If this fails, ensure the coordinator is running on your host machine.

---

## Test Procedure

Execute each step **inside the container**. Document exact errors encountered. Do NOT proceed past failures - fix the plugin code and re-test.

### Phase 1: Marketplace Installation

**Test 1.1: Add marketplace**

```bash
claude
# Then inside Claude Code:
/plugin marketplace add michaelansel/c3po
```

**Expected**: Marketplace added successfully
**Actual**: _document result_

**Test 1.2: Install plugin from marketplace**

```
/plugin install c3po
```

**Expected**: Plugin installed, no errors
**Actual**: _document result_

**Test 1.3: Verify plugin status**

```
/plugin info c3po
```

**Expected**:
- Status: Enabled
- No errors listed
- Skills: coordinate
- Hooks: Setup, SessionStart, Stop, SessionEnd

**Actual**: _document result_

### Phase 2: Plugin Structure Validation

**Test 2.1: Verify no duplicate hooks error**

Check plugin info output from Test 1.3. Should NOT show:
```
Failed to load hooks from .../hooks/hooks.json: Duplicate hooks file detected
```

**Test 2.2: Verify no hardcoded MCP server**

Exit Claude Code and check MCP configuration:
```bash
claude mcp list
```

Should NOT show any c3po MCP server yet. The MCP server should only exist AFTER running `/coordinate setup`.

If you see `c3po` with `http://localhost:8420/mcp` already configured, the plugin has a bug - it should not auto-configure the MCP server.

### Phase 3: Setup Flow

**Test 3.1: Verify /coordinate skill is available**

```bash
claude
# Inside Claude Code:
/coordinate
```

**Expected**: Shows usage information for coordinate skill (setup, status, agents, send)
**Actual**: _document result_

**Test 3.2: Run setup**

```
/coordinate setup
```

When prompted:
- Coordinator URL: `http://host.finch.internal:8420`
- Agent ID: `test-agent`

**Expected**:
- Prompts for coordinator URL
- Prompts for agent ID
- Tests connectivity to coordinator
- Configures MCP server using `claude mcp add`
- Shows success message

**Actual**: _document result_

**Test 3.3: Verify MCP server configured**

Exit Claude Code and verify:
```bash
claude mcp list
```

**Expected**: Shows c3po server with:
- URL: `http://host.finch.internal:8420/mcp`
- Header: `X-Agent-ID: test-agent`

**Actual**: _document result_

### Phase 4: Connection Validation

**Test 4.1: Restart Claude Code and verify connection**

```bash
claude
# Inside Claude Code, use the list_agents MCP tool
```

**Expected**:
- MCP server connects successfully
- `list_agents` returns list including `test-agent`

**Actual**: _document result_

**Test 4.2: Check status via skill**

```
/coordinate status
```

**Expected**: Shows connected status, your agent ID, agent count
**Actual**: _document result_

### Phase 5: Alternative Setup (claude --init)

**Test 5.1: Reset environment**

```bash
# Exit Claude Code, then remove the MCP config
claude mcp remove c3po

# Verify it's gone
claude mcp list
```

**Test 5.2: Trigger setup hook via --init**

```bash
claude --init
```

**Expected**: C3PO setup wizard runs automatically, same interactive flow as `/coordinate setup`
**Actual**: _document result_

---

## Clean Up

After testing:
```bash
exit  # Exit container
finch rm c3po-test  # Remove container
```

---

## Known Issues Found

Document each issue with:
1. Test step where it failed
2. Exact error message
3. Root cause (if identified)
4. Fix applied

### Issue Template

```
### Issue N: [Short description]

**Test**: [Test ID, e.g., "Test 1.3"]
**Error**:
[exact error message]

**Root Cause**:
[explanation]

**Fix**:
[file and code changes made]

**Verification**:
[re-run test from Phase 0, document result]
```

---

## Files to Examine

If tests fail, check these files in the repo:

| File | Purpose |
|------|---------|
| `.claude-plugin/marketplace.json` | Marketplace definition (repo root) |
| `plugin/.claude-plugin/plugin.json` | Plugin manifest |
| `plugin/hooks/hooks.json` | Hook definitions |
| `plugin/skills/coordinate/SKILL.md` | Coordinate skill definition |
| `plugin/setup.py` | Setup hook script |

---

## Success Criteria

All of the following must pass in a clean container:

- [ ] Container starts with no pre-existing Claude config
- [ ] Plugin installs from marketplace without errors
- [ ] No duplicate hooks error in plugin info
- [ ] No MCP server auto-configured before setup
- [ ] `/coordinate` skill is available and shows help
- [ ] `/coordinate setup` prompts for URL and agent ID
- [ ] `/coordinate setup` configures MCP server correctly
- [ ] After restart, MCP tools work (list_agents, ping)
- [ ] Agent appears in coordinator's agent list
- [ ] `claude --init` triggers the setup wizard

---

## Coordinator Setup (on host machine)

Ensure coordinator is running on your host before testing:

```bash
cd /path/to/c3po/coordinator
docker-compose up -d

# Verify
curl http://localhost:8420/api/health
```

The container accesses this via `http://host.finch.internal:8420`.
