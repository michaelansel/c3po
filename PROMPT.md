# C3PO Plugin Installation Test Plan

## Objective

Test the complete plugin installation and setup flow from a clean environment. The current plugin has multiple issues that prevent successful installation from the GitHub marketplace.

## Prerequisites

- A machine WITHOUT the c3po plugin installed (clean slate)
- Claude Code CLI installed
- Access to coordinator at a known URL (not localhost)
- GitHub repo: https://github.com/michaelansel/c3po

## Test Procedure

Execute each step. Document exact errors encountered. Do NOT proceed past failures - fix and re-test.

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

Check plugin info output. Should NOT show:
```
Failed to load hooks from .../hooks/hooks.json: Duplicate hooks file detected
```

**Test 2.2: Verify no hardcoded MCP server**

Check MCP status. Should NOT show:
```
Plugin:c3po:c3po MCP Server
URL: http://localhost:8420/mcp
```

The MCP server should only exist after running `/coordinate setup`.

### Phase 3: Setup Flow

**Test 3.1: Verify /coordinate skill is available**
```
/coordinate
```

**Expected**: Shows usage information for coordinate skill
**Actual**: _document result_

**Test 3.2: Run setup**
```
/coordinate setup
```

**Expected**:
- Prompts for coordinator URL
- Prompts for agent ID
- Tests connectivity
- Configures MCP server
- Shows success message

**Actual**: _document result_

**Test 3.3: Verify MCP server configured**

After setup, check MCP status. Should show c3po server with:
- Custom URL (not localhost, unless that's what you entered)
- X-Agent-ID header set

### Phase 4: Connection Validation

**Test 4.1: Restart Claude Code**

Exit and restart to pick up MCP changes.

**Test 4.2: Check connection**
```
/coordinate status
```

**Expected**: Shows connected status, agent count
**Actual**: _document result_

**Test 4.3: List agents**

Use the `list_agents` MCP tool.

**Expected**: Returns list including your agent
**Actual**: _document result_

### Phase 5: Alternative Setup (claude --init)

Uninstall and reinstall plugin, then test the Setup hook:

**Test 5.1: Trigger setup hook**
```bash
claude --init
```

**Expected**: C3PO setup wizard runs, same flow as `/coordinate setup`
**Actual**: _document result_

## Known Issues Found

Document each issue with:
1. Test step where it failed
2. Exact error message
3. Root cause (if identified)
4. Fix applied

### Issue Template
```
### Issue N: [Short description]

**Test**: [Test ID]
**Error**:
[exact error message]

**Root Cause**:
[explanation]

**Fix**:
[code changes made]

**Verification**:
[re-test result]
```

## Files to Examine

If tests fail, check these files:

| File | Purpose |
|------|---------|
| `.claude-plugin/marketplace.json` | Marketplace definition |
| `plugin/.claude-plugin/plugin.json` | Plugin manifest |
| `plugin/hooks/hooks.json` | Hook definitions |
| `plugin/skills/coordinate/SKILL.md` | Coordinate skill |
| `plugin/setup.py` | Setup hook script |

## Success Criteria

All of the following must pass:
- [ ] Plugin installs from marketplace without errors
- [ ] No duplicate hooks error
- [ ] No hardcoded localhost MCP server on install
- [ ] `/coordinate` skill is available
- [ ] `/coordinate setup` configures MCP server correctly
- [ ] `claude --init` triggers setup wizard
- [ ] After setup, MCP tools work (list_agents, ping)
- [ ] Agent appears in coordinator's agent list

## Coordinator Setup (if needed)

If you need a coordinator running:

```bash
# On a server/NAS:
git clone https://github.com/michaelansel/c3po.git
cd c3po/coordinator
docker-compose up -d

# Verify:
curl http://[server]:8420/api/health
```
