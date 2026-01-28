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

## CURRENT TASK: Two-Agent End-to-End Validation

**Status:** Coordinator deployed and running at http://mkansel-nas.home.qerk.be:8420

### Problem

The plugin provides MCP tools (`list_agents`, `send_request`, etc.) but they're not available in headless mode (`claude -p`) because the plugin's `.mcp.json` isn't being loaded.

### Task: Make MCP Tools Work in Headless Mode

Investigate and fix why the c3po MCP server isn't connecting when using `claude -p`.

**Debug steps:**

1. Check if plugin is installed:
   ```bash
   ls ~/.claude/plugins/c3po/
   ```

2. Check plugin MCP config:
   ```bash
   cat ~/.claude/plugins/c3po/.mcp.json
   ```

3. Test coordinator is reachable:
   ```bash
   curl http://mkansel-nas.home.qerk.be:8420/api/health
   ```

4. Try adding MCP server directly to Claude Code config instead of via plugin:
   ```bash
   claude mcp add c3po --transport http http://mkansel-nas.home.qerk.be:8420/mcp
   ```

5. Test with headless mode:
   ```bash
   C3PO_AGENT_ID=test-agent claude -p "Use list_agents to see online agents"
   ```

**If MCP config approach doesn't work**, try:
- Check Claude Code docs for how plugins load MCP servers
- Check if environment variables are being passed correctly
- Check if there's a different way to specify MCP servers for headless mode

### Once MCP Works: Two-Agent Test

1. **Start Agent B in background** (listening for requests):
   ```bash
   cd /tmp/agent-b
   C3PO_AGENT_ID=agent-b C3PO_COORDINATOR_URL=http://mkansel-nas.home.qerk.be:8420 \
     claude -p "You are agent-b. Use wait_for_request with a 120 second timeout to wait for incoming requests. When you receive a request, process it and use respond_to_request to reply." &
   ```

2. **Run Agent A** (sends request):
   ```bash
   cd /tmp/agent-a
   C3PO_AGENT_ID=agent-a C3PO_COORDINATOR_URL=http://mkansel-nas.home.qerk.be:8420 \
     claude -p "Use send_request to ask agent-b 'What is 2+2?', then use wait_for_response to get the answer. Report the response you received."
   ```

3. **Verify** Agent A reports receiving a response from Agent B.

### Success Criteria

- [ ] MCP tools available in headless mode
- [ ] Agent A can send request to Agent B
- [ ] Agent B receives and responds
- [ ] Agent A receives response
- [ ] Document how to properly configure MCP for headless mode
