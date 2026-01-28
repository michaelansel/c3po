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

## CURRENT TASK: Validation

Implementation is complete (all 13 steps done, 70 tests passing). Now need to validate end-to-end.

### Task 1: Fix Deployment to NAS

The deploy script doesn't work due to architecture mismatch (arm64 Mac → x86_64 NAS).

**Fix approach:** Build Docker image directly on NAS.

1. Copy source to NAS via tar:
   ```bash
   cd coordinator
   tar czf - --exclude='__pycache__' --exclude='.venv' --exclude='.pytest_cache' --exclude='tests' . | \
     ssh admin@mkansel-nas.home.qerk.be "mkdir -p /volume1/enc-containers/c3po/coordinator && cd /volume1/enc-containers/c3po/coordinator && tar xzf -"
   ```

2. Build on NAS:
   ```bash
   ssh admin@mkansel-nas.home.qerk.be "cd /volume1/enc-containers/c3po/coordinator && docker build -t c3po-coordinator:latest ."
   ```

3. Start containers:
   ```bash
   ssh admin@mkansel-nas.home.qerk.be "cd /volume1/enc-containers/c3po/coordinator && docker-compose up -d"
   ```

4. Verify:
   ```bash
   curl http://mkansel-nas.home.qerk.be:8420/api/health
   # Expected: {"status":"ok","agents_online":0}
   ```

5. Update `scripts/deploy.sh` to use this approach instead of local build + push.

### Task 2: Two-Agent End-to-End Test

Prove two Claude Code agents can communicate.

1. Install plugin locally:
   ```bash
   cp -r plugin ~/.claude/plugins/c3po
   ```

2. **Terminal 1 - Agent A:**
   ```bash
   mkdir -p /tmp/agent-a && cd /tmp/agent-a
   export C3PO_AGENT_ID=agent-a
   export C3PO_COORDINATOR_URL=http://mkansel-nas.home.qerk.be:8420
   claude
   ```
   Run `/coordinate status` - should show 1 agent online.

3. **Terminal 2 - Agent B:**
   ```bash
   mkdir -p /tmp/agent-b && cd /tmp/agent-b
   export C3PO_AGENT_ID=agent-b
   export C3PO_COORDINATOR_URL=http://mkansel-nas.home.qerk.be:8420
   claude
   ```
   Run `/coordinate status` - should show 2 agents online.

4. **Agent A → Agent B:** In Terminal 1, ask Claude to send a message to agent-b.

5. **Agent B responds:** Complete any task in Terminal 2; Stop hook should trigger and show pending request.

6. **Agent A receives:** The `wait_for_response` should return with the answer.

### Success Criteria

- [ ] Coordinator running at http://mkansel-nas.home.qerk.be:8420
- [ ] Both agents visible via `list_agents`
- [ ] Request sent A → B
- [ ] Request received by B (Stop hook or manual check)
- [ ] Response sent B → A
- [ ] Response received by A
- [ ] Round-trip < 10 seconds
