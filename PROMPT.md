# C3PO Implementation

## Objective

Build a multi-agent coordination system for Claude Code that enables CC instances on different hosts to send requests to each other, have multi-turn conversations, and collaborate on cross-cutting problems—without requiring the human to relay messages.

## Key Requirements

### Coordinator (Python/FastMCP + Redis)
- HTTP MCP server on port 8420 accepting connections from multiple CC instances
- Agent identification via `X-Agent-ID` header (defaults to folder name)
- Redis-backed message queues (`inbox:{agent}`, `responses:{agent}`)
- MCP tools: `list_agents`, `send_request`, `get_pending_requests`, `respond_to_request`, `wait_for_response`, `wait_for_request`
- REST endpoints: `GET /api/health`, `GET /api/pending` (for hooks)
- Docker packaging with docker-compose (coordinator + Redis)

### Plugin (Claude Code Plugin format)
- MCP config pointing to coordinator with X-Agent-ID header
- Stop hook that checks `/api/pending` and blocks if requests pending
- SessionStart hook that confirms connection and shows online agents
- `/coordinate` skill for status checks and quick messaging

### Behavior
- Human-initiated collaboration only (no automatic agent suggestions)
- Async request/response with multi-turn support
- Graceful degradation when coordinator unavailable
- Human interrupt (Esc/Ctrl-C) works normally

## Acceptance Criteria

1. Two CC instances can exchange messages via coordinator (< 10s latency)
2. Stop hook triggers Claude to process pending requests
3. Multi-turn back-and-forth conversations work
4. System continues working locally if coordinator is down
5. New host setup: install plugin + set env vars

## Environment Variables

- `C3PO_COORDINATOR_URL` - Coordinator URL (default: `http://localhost:8420`)
- `C3PO_AGENT_ID` - Agent identifier (default: folder name)

## Implementation Guide

Follow the step-by-step implementation plan:
**`.sop/planning/implementation/plan.md`**

Each of the 13 steps produces a working, demoable increment. Complete the demo for each step before proceeding.

## Reference Documents

- **Detailed Design**: `.sop/planning/design/detailed-design.md`
- **Requirements**: `.sop/planning/idea-honing.md`
- **Research**: `.sop/planning/research/`

## Tech Stack

- **Coordinator**: Python 3.11+, FastMCP, Redis, uvicorn
- **Plugin**: Claude Code plugin format (.claude-plugin/, .mcp.json, hooks/)
- **Deployment**: Docker, docker-compose

## Deployment

### Production (Synology NAS)

```bash
# Full deployment (build, push, deploy)
./scripts/deploy.sh full

# Individual commands
./scripts/deploy.sh build    # Build image locally with finch/docker
./scripts/deploy.sh push     # Copy image to NAS
./scripts/deploy.sh deploy   # Start containers on NAS
./scripts/deploy.sh status   # Check container status
./scripts/deploy.sh logs     # View coordinator logs
./scripts/deploy.sh stop     # Stop containers
```

**NAS Details:**
- Host: `admin@mkansel-nas.home.qerk.be`
- Data directory: `/volume1/enc-containers/c3po`
- Coordinator port: 8420
- Production URL: `http://mkansel-nas.home.qerk.be:8420`

### Local Testing

```bash
# Start local Redis + coordinator
./scripts/test-local.sh start

# Launch test agents (in separate terminals)
./scripts/test-local.sh agent-a
./scripts/test-local.sh agent-b

# Check status
./scripts/test-local.sh status

# Stop everything
./scripts/test-local.sh stop
```

---

## Headless Mode Configuration

Plugin `.mcp.json` files are not automatically loaded in headless mode (`claude -p`). MCP servers must be added directly to Claude Code configuration.

### Adding MCP Server for Headless Mode

**User scope (recommended)**: Available from any directory:
```bash
claude mcp add c3po http://mkansel-nas.home.qerk.be:8420/mcp \
  -t http -s user -H "X-Agent-ID: your-agent-name"
```

**Project scope**: Only available in the current project:
```bash
claude mcp add c3po http://mkansel-nas.home.qerk.be:8420/mcp \
  -t http -H "X-Agent-ID: your-agent-name"
```

**Per-invocation**: For different agent IDs in testing, use `--mcp-config` with a JSON file:
```bash
# Create config file
cat > /tmp/agent-a-mcp.json << 'EOF'
{
  "mcpServers": {
    "c3po": {
      "type": "http",
      "url": "http://mkansel-nas.home.qerk.be:8420/mcp",
      "headers": { "X-Agent-ID": "agent-a" }
    }
  }
}
EOF

# Use with --strict-mcp-config to only use this config
echo "Your prompt" | claude -p --mcp-config /tmp/agent-a-mcp.json --strict-mcp-config \
  --allowedTools "mcp__c3po__list_agents,mcp__c3po__send_request"
```

### Pre-approving MCP Tools

In headless mode, use `--allowedTools` to pre-approve MCP tools:
```bash
--allowedTools "mcp__c3po__list_agents,mcp__c3po__send_request,mcp__c3po__wait_for_response,mcp__c3po__wait_for_request,mcp__c3po__respond_to_request"
```

### Verify MCP Configuration

```bash
claude mcp list  # Shows configured MCP servers and connection status
```

---

## Two-Agent Test (Validated 2026-01-28)

### Test Results

All success criteria met:
- [x] MCP tools available in headless mode (via `claude mcp add` with user scope)
- [x] Agent A can send request to Agent B
- [x] Agent B receives and responds
- [x] Agent A receives response

**Test log:**
1. Agent A sent request `agent-a::agent-b::aa9f50dd`: "What is the capital of France?"
2. Agent B received request and responded: "The capital of France is Paris."
3. Agent A successfully retrieved response with status: success

### Running Two-Agent Tests

1. **Create MCP configs for each agent** (see above)

2. **Start Agent B** (listener):
   ```bash
   cd /tmp/agent-b
   echo "Use mcp__c3po__wait_for_request with timeout 120. When you receive a request, respond using mcp__c3po__respond_to_request." | \
     claude -p --mcp-config /tmp/agent-b-mcp.json --strict-mcp-config \
     --allowedTools "mcp__c3po__wait_for_request,mcp__c3po__respond_to_request" &
   ```

3. **Wait for Agent B to register** (10-20 seconds)

4. **Run Agent A** (sender):
   ```bash
   cd /tmp/agent-a
   echo "Use mcp__c3po__send_request to ask agent-b 'What is 2+2?', then use mcp__c3po__wait_for_response with timeout 60." | \
     claude -p --mcp-config /tmp/agent-a-mcp.json --strict-mcp-config \
     --allowedTools "mcp__c3po__send_request,mcp__c3po__wait_for_response"
   ```

---

## CURRENT TASKS: Production Readiness

**STATUS: INCOMPLETE** - The following tasks have NOT been done yet.

---

### TASK 1: Clean Room Validation in Fresh Containers [NOT STARTED]

Test the entire setup from scratch in two fresh finch containers to ensure all dependencies are documented and setup steps are complete.

**Steps:**

1. Create two fresh containers:
   ```bash
   finch run -it --name c3po-test-a ubuntu:22.04 bash
   finch run -it --name c3po-test-b ubuntu:22.04 bash
   ```

2. In each container, follow ONLY the documented setup steps:
   - Install prerequisites
   - Install Claude Code
   - Configure c3po MCP server
   - Verify connection to coordinator

3. Test agent communication between containers

4. **Document any missing steps or dependencies discovered**

5. Update `docs/SETUP.md` with complete, tested instructions

**Success Criteria:**
- [ ] Fresh container setup works with documented steps only
- [ ] No undocumented dependencies
- [ ] Both containers can communicate through coordinator
- [ ] Setup time < 15 minutes per host

---

### TASK 2: Comprehensive Test Plan [NOT STARTED]

**REQUIRED DELIVERABLE:** Create file `tests/TEST_PLAN.md`

This file does NOT exist yet. Create it.

**Test Categories:**

1. **Unit Tests** (existing)
   - Coordinator: agents, messaging, errors, REST API
   - Hooks: check_inbox, register_agent

2. **Integration Tests**
   - Coordinator + Redis
   - MCP tool invocation via HTTP
   - Hook execution

3. **End-to-End Tests**
   - Two-agent request/response
   - Multi-turn conversation (3+ exchanges)
   - Timeout handling
   - Agent disconnect/reconnect
   - Coordinator restart recovery

4. **Error Handling Tests**
   - Coordinator unavailable
   - Target agent offline
   - Request timeout
   - Invalid agent ID

5. **Performance Tests**
   - Latency < 10 seconds
   - 10+ concurrent agents

**Deliverable:** `tests/TEST_PLAN.md` with test ID, description, steps, expected result, automated vs manual status.

---

### TASK 3: Plugin-Based Enrollment [NOT STARTED]

**REQUIRED DELIVERABLES:**
1. Create file `plugin/setup.py` - setup script that configures MCP
2. Update `plugin/.claude-plugin/plugin.json` - add Setup hook
3. Create/update `plugin/skills/setup/SKILL.md` - `/coordinate setup` skill

These files do NOT exist yet or need modification. Create them.

**Goal:** Plugin installation IS the enrollment. No separate scripts needed.

**Target UX:**
```bash
# One-time: Add the c3po marketplace
/plugin marketplace add user/c3po

# Enroll this machine:
/plugin install c3po
```

On install, the plugin should:
1. Prompt for coordinator URL (or use `C3PO_COORDINATOR_URL` env var)
2. Prompt for agent ID (or default to folder name)
3. Configure the MCP server connection
4. Verify connection works
5. Show success message

**Plugin Structure Updates:**

1. **Add setup hook** - `plugin/.claude-plugin/plugin.json` should include a setup/install hook that runs on first install

2. **Create setup script** - `plugin/setup.py` or similar that:
   ```python
   # Runs during plugin install
   # 1. Check for C3PO_COORDINATOR_URL or prompt user
   # 2. Check for C3PO_AGENT_ID or use folder name
   # 3. Run: claude mcp add c3po <url>/mcp -t http -s user -H "X-Agent-ID: <id>"
   # 4. Test connection: curl <url>/api/health
   # 5. Print success/next steps
   ```

3. **Update plugin.json** with install hook:
   ```json
   {
     "name": "c3po",
     "hooks": {
       "Setup": [{
         "hooks": [{
           "type": "command",
           "command": "python3 ${CLAUDE_PLUGIN_ROOT}/setup.py"
         }]
       }]
     }
   }
   ```

4. **Handle reconfiguration** - `/coordinate setup` skill to reconfigure coordinator URL

**Requirements:**
- Plugin install handles all MCP configuration
- Works with env vars OR interactive prompts
- Idempotent (reinstall doesn't break things)
- `/coordinate setup` allows changing coordinator URL later
- Clear success/failure messages

**Success Criteria:**
- [ ] `/plugin install c3po` fully enrolls the machine
- [ ] No manual `claude mcp add` needed
- [ ] Connection verified during install
- [ ] `/coordinate setup` allows reconfiguration
- [ ] Works on fresh CC installation

---

### TASK 4: Documentation Polish [NOT STARTED]

**REQUIRED:** Update documentation to reflect plugin-based enrollment.

Current docs reference manual `claude mcp add` commands. Update them to show plugin install flow.

**README.md** - Quick start:
```markdown
# C3PO - Claude Code Coordination

Connect Claude Code instances across machines.

## Quick Start

1. Deploy coordinator:
   ```bash
   ./scripts/deploy.sh full
   ```

2. Enroll any Claude Code instance:
   ```bash
   curl -sSL .../enroll.sh | bash -s -- http://your-nas:8420
   ```

3. Done! Your CC instance can now communicate with others.
```

**docs/SETUP.md** - Complete setup (validated in clean room)

**docs/USAGE.md** - How to use (sending messages, skills, etc.)

**docs/TROUBLESHOOTING.md** - Common issues and solutions

---

## DEFINITION OF DONE

All tasks complete when:

1. **Clean room validated** - Fresh container setup works with plugin install only
2. **Test plan complete** - `tests/TEST_PLAN.md` exists
3. **Plugin-based enrollment** - `/plugin install c3po` fully configures everything
4. **Documentation updated** - README, SETUP, USAGE, TROUBLESHOOTING
5. **All tests passing** - Unit, integration, e2e

**Final user experience:**
```bash
# Self-host coordinator (one-time on NAS)
git clone https://github.com/user/c3po && ./scripts/deploy.sh full

# Enroll any CC instance (inside Claude Code)
/plugin marketplace add user/c3po
/plugin install c3po
# → Prompts for coordinator URL (or uses C3PO_COORDINATOR_URL)
# → Configures MCP, verifies connection
# → Done! Ready to collaborate.

# Or with env var pre-set:
export C3PO_COORDINATOR_URL=http://nas:8420
/plugin install c3po
# → Auto-configures, no prompts needed
```
