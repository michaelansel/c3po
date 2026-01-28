# C3PO Implementation Plan

## Implementation Checklist

- [x] **Step 1**: Project scaffolding and coordinator skeleton
- [x] **Step 2**: Redis integration and agent registration
- [x] **Step 3**: Basic messaging (send_request, get_pending_requests)
- [x] **Step 4**: Response handling (respond_to_request, wait_for_response)
- [x] **Step 5**: Blocking wait_for_request tool
- [x] **Step 6**: REST API for hooks (/api/pending, /api/health)
- [x] **Step 7**: Docker packaging for coordinator
- [x] **Step 8**: Plugin skeleton and MCP configuration
- [x] **Step 9**: Stop hook implementation
- [x] **Step 10**: SessionStart hook for registration
- [x] **Step 11**: End-to-end two-agent test
- [x] **Step 12**: Error handling and graceful degradation
- [x] **Step 13**: /coordinate skill and documentation

---

## Step 1: Project Scaffolding and Coordinator Skeleton

### Objective
Set up the project structure and create a minimal FastMCP coordinator that responds to a simple health check tool.

### Implementation Guidance

Create the directory structure:
```
c3po/
├── coordinator/
│   ├── __init__.py
│   ├── server.py          # FastMCP server entry point
│   ├── requirements.txt   # fastmcp, redis, uvicorn
│   └── tests/
│       └── test_server.py
├── plugin/
│   └── (empty for now)
└── README.md
```

Implement `coordinator/server.py`:
- Create FastMCP instance with name "c3po"
- Add a simple `ping` tool that returns `{"pong": true, "timestamp": "..."}`
- Add `list_agents` tool that returns empty list for now
- Run with HTTP transport on port 8420

### Test Requirements
- Unit test: `ping` tool returns expected response
- Manual test: Start server, connect with MCP client, call ping

### Integration
This is the foundation. All subsequent steps build on this server.

### Demo
Start the coordinator and verify it responds:
```bash
cd coordinator
python server.py
# In another terminal:
curl -X POST http://localhost:8420/mcp -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
# Should return list including "ping" tool
```

---

## Step 2: Redis Integration and Agent Registration

### Objective
Connect to Redis and implement agent registration so agents can join the network and be discovered.

### Implementation Guidance

Add Redis client initialization in `server.py`:
- Connect to Redis (configurable via `REDIS_URL` env var)
- Default to `redis://localhost:6379`

Create `coordinator/agents.py`:
- `register_agent(agent_id, capabilities=None)` → stores in Redis hash `agents`
- `list_agents()` → returns all agents from hash
- `get_agent(agent_id)` → returns single agent or None
- `update_heartbeat(agent_id)` → updates `last_seen` timestamp

Add middleware to extract `X-Agent-ID` header and auto-register on each request.

Implement MCP tools:
- `register_agent(name?: string, capabilities?: string[])` → explicit registration
- `list_agents()` → returns all registered agents with status

### Test Requirements
- Unit test: Agent registration stores correct data in Redis (use fakeredis)
- Unit test: list_agents returns registered agents
- Unit test: Duplicate registration updates rather than errors

### Integration
Builds on Step 1's server. Adds Redis dependency.

### Demo
```bash
# Terminal 1: Start Redis
docker run --rm -p 6379:6379 redis:7-alpine

# Terminal 2: Start coordinator
python server.py

# Terminal 3: Test registration via curl or MCP client
# Call list_agents - should show agent based on X-Agent-ID header
```

---

## Step 3: Basic Messaging (send_request, get_pending_requests)

### Objective
Enable agents to send requests to other agents and retrieve pending requests from their inbox.

### Implementation Guidance

Create `coordinator/messaging.py`:
- `send_request(from_agent, to_agent, message, context=None)` → generates request ID, pushes to Redis list `inbox:{to_agent}`
- `get_pending_requests(agent_id)` → pops all from Redis list, returns as array
- Request format: `{id, from_agent, to_agent, message, context, timestamp, status}`

Implement MCP tools:
- `send_request(target: string, message: string, context?: string)` → uses middleware to get sender ID
- `get_pending_requests()` → returns pending requests for calling agent

Handle edge cases:
- Target agent doesn't exist → return error with available agents
- Empty inbox → return empty array

### Test Requirements
- Unit test: send_request creates properly formatted message in Redis
- Unit test: get_pending_requests retrieves and removes messages
- Unit test: Multiple messages queue correctly (FIFO order)
- Unit test: Unknown target returns helpful error

### Integration
Uses agents module from Step 2 to verify target exists.

### Demo
```bash
# With coordinator and Redis running:
# 1. Send request as "agent-a" to "agent-b"
# 2. Get pending requests as "agent-b" - should see the message
# 3. Get pending requests again - should be empty (consumed)
```

---

## Step 4: Response Handling (respond_to_request, wait_for_response)

### Objective
Enable agents to respond to requests and for requesting agents to receive those responses.

### Implementation Guidance

Extend `coordinator/messaging.py`:
- `respond_to_request(request_id, from_agent, response, status="success")` → pushes to Redis list `responses:{original_sender}`
- `wait_for_response(agent_id, request_id, timeout=60)` → uses Redis BLPOP on responses list

Response format: `{request_id, from_agent, to_agent, response, status, timestamp}`

Implement MCP tools:
- `respond_to_request(request_id: string, response: string, status?: string)`
- `wait_for_response(request_id: string, timeout?: int)` → blocking call

Parse request_id to determine original sender (format: `{sender}-{receiver}-{timestamp}`).

### Test Requirements
- Unit test: respond_to_request creates properly formatted response
- Unit test: wait_for_response returns when response arrives
- Unit test: wait_for_response times out correctly and returns timeout indicator
- Integration test: Full send → receive → respond → wait cycle

### Integration
Builds on Step 3 messaging. Completes the request/response cycle.

### Demo
```bash
# Two terminal windows simulating two agents:
# Terminal A (agent-a): send_request to agent-b, then wait_for_response
# Terminal B (agent-b): get_pending_requests, then respond_to_request
# Terminal A should receive the response
```

---

## Step 5: Blocking wait_for_request Tool

### Objective
Enable agents to actively listen for incoming requests by blocking until one arrives.

### Implementation Guidance

Extend `coordinator/messaging.py`:
- `wait_for_request(agent_id, timeout=60)` → uses Redis BLPOP on `inbox:{agent_id}`

Implement MCP tool:
- `wait_for_request(timeout?: int)` → blocks until request arrives or timeout

This provides an alternative to the Stop hook pattern for agents that want to actively listen.

### Test Requirements
- Unit test: wait_for_request returns when request arrives
- Unit test: wait_for_request times out correctly
- Unit test: Multiple queued requests return in order

### Integration
Alternative to Stop hook polling. Uses same inbox as Step 3.

### Demo
```bash
# Terminal A (agent-a): wait_for_request with 30s timeout
# Terminal B (agent-b): send_request to agent-a
# Terminal A immediately receives the request (before timeout)
```

---

## Step 6: REST API for Hooks (/api/pending, /api/health)

### Objective
Add REST endpoints that hooks can call to check for pending requests without going through MCP.

### Implementation Guidance

Add REST routes to `server.py` (FastMCP uses Starlette internally):
- `GET /api/health` → `{"status": "ok", "agents_online": N}`
- `GET /api/pending` → requires `X-Agent-ID` header, returns `{"count": N, "requests": [...]}`

The `/api/pending` endpoint should NOT consume messages (just peek). Hooks use this for quick checks.

### Test Requirements
- Unit test: /api/health returns correct format
- Unit test: /api/pending returns count without consuming
- Unit test: /api/pending with unknown agent returns empty

### Integration
Used by Stop hook (Step 9). Non-MCP access for lightweight checks.

### Demo
```bash
# With pending request in agent-a's inbox:
curl http://localhost:8420/api/pending -H "X-Agent-ID: agent-a"
# Returns: {"count": 1, "requests": [...]}

# Call again - still returns same count (not consumed)
```

---

## Step 7: Docker Packaging for Coordinator

### Objective
Package the coordinator and Redis into Docker containers for easy deployment on NAS.

### Implementation Guidance

Create `coordinator/Dockerfile`:
```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["python", "server.py"]
```

Create `coordinator/docker-compose.yml`:
```yaml
version: '3.8'
services:
  coordinator:
    build: .
    ports:
      - "8420:8420"
    environment:
      - REDIS_URL=redis://redis:6379
    depends_on:
      - redis
  redis:
    image: redis:7-alpine
    volumes:
      - redis_data:/data
    command: redis-server --appendonly yes
volumes:
  redis_data:
```

### Test Requirements
- Docker build succeeds
- docker-compose up starts both services
- Coordinator connects to Redis successfully
- All previous demos work with containerized version

### Integration
Packages Steps 1-6 for deployment.

### Demo
```bash
cd coordinator
docker-compose up -d
curl http://localhost:8420/api/health
# Returns: {"status": "ok", ...}
```

---

## Step 8: Plugin Skeleton and MCP Configuration

### Objective
Create the Claude Code plugin structure with MCP server configuration.

### Implementation Guidance

Create plugin structure:
```
plugin/
├── .claude-plugin/
│   └── plugin.json
├── .mcp.json
├── hooks/
│   └── (placeholder files)
└── README.md
```

`.claude-plugin/plugin.json`:
```json
{
  "name": "c3po",
  "description": "Multi-agent coordination for Claude Code",
  "version": "0.1.0"
}
```

`.mcp.json`:
```json
{
  "mcpServers": {
    "c3po": {
      "type": "http",
      "url": "${C3PO_COORDINATOR_URL:-http://localhost:8420}/mcp",
      "headers": {
        "X-Agent-ID": "${C3PO_AGENT_ID:-default}"
      }
    }
  }
}
```

### Test Requirements
- Plugin structure validates against CC plugin schema
- MCP config connects to coordinator when installed

### Integration
Prepares for hooks in Steps 9-10. Can be tested with CC.

### Demo
```bash
# With coordinator running:
# In a project directory:
export C3PO_COORDINATOR_URL=http://localhost:8420
export C3PO_AGENT_ID=test-agent

# Install plugin locally for testing
claude mcp add c3po --config /path/to/plugin/.mcp.json

# In Claude Code:
# Use list_agents tool - should work
```

---

## Step 9: Stop Hook Implementation

### Objective
Implement the Stop hook that checks for pending requests and blocks Claude if any exist.

### Implementation Guidance

Create `plugin/hooks/check_inbox.py`:
```python
#!/usr/bin/env python3
import json
import os
import sys
import urllib.request
import urllib.error

COORDINATOR = os.environ.get("C3PO_COORDINATOR_URL", "http://localhost:8420")
AGENT_ID = os.environ.get("C3PO_AGENT_ID", os.path.basename(os.getcwd()))

def main():
    # Check if stop hook is already active (prevent loops)
    stdin_data = json.load(sys.stdin)
    if stdin_data.get("stop_hook_active"):
        sys.exit(0)

    try:
        req = urllib.request.Request(
            f"{COORDINATOR}/api/pending",
            headers={"X-Agent-ID": AGENT_ID}
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())

        if data.get("count", 0) > 0:
            output = {
                "decision": "block",
                "reason": f"You have {data['count']} pending coordination request(s). Use get_pending_requests to retrieve and process them, then respond with respond_to_request."
            }
            print(json.dumps(output))
    except Exception:
        pass  # Fail open

    sys.exit(0)

if __name__ == "__main__":
    main()
```

Update `plugin.json` to include hook configuration.

### Test Requirements
- Hook script runs without error when no pending requests
- Hook returns block decision when requests pending
- Hook fails open when coordinator unreachable
- Hook respects stop_hook_active flag

### Integration
Uses /api/pending from Step 6. Triggers CC to process requests.

### Demo
```bash
# 1. Start CC in a project directory with plugin installed
# 2. From another terminal, send a request to that agent via coordinator
# 3. Complete a task in CC - when Claude tries to stop, hook triggers
# 4. Claude receives instruction to process pending request
```

---

## Step 10: SessionStart Hook for Registration

### Objective
Implement hook that registers the agent and injects coordination context on session start.

### Implementation Guidance

Create `plugin/hooks/register_agent.py`:
```python
#!/usr/bin/env python3
import json
import os
import sys
import urllib.request

COORDINATOR = os.environ.get("C3PO_COORDINATOR_URL", "http://localhost:8420")
AGENT_ID = os.environ.get("C3PO_AGENT_ID", os.path.basename(os.getcwd()))

def main():
    try:
        # Check coordinator health
        req = urllib.request.Request(f"{COORDINATOR}/api/health")
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())

        # Output context for Claude
        print(f"[c3po] Connected to coordinator. Agent ID: {AGENT_ID}")
        print(f"[c3po] {data.get('agents_online', 0)} agent(s) online.")
        print("[c3po] Use list_agents to see available agents, send_request to collaborate.")
    except Exception as e:
        print(f"[c3po] Coordinator not available ({e}). Running in local mode.")

    sys.exit(0)

if __name__ == "__main__":
    main()
```

Update `plugin.json` with SessionStart hook.

### Test Requirements
- Hook outputs connection status
- Hook gracefully handles coordinator unavailable
- Context appears in Claude's session

### Integration
Complements Stop hook. Provides initial context to Claude.

### Demo
```bash
# Start CC with plugin installed
# On startup, see "[c3po] Connected to coordinator..." message
# Claude now knows coordination is available
```

---

## Step 11: End-to-End Two-Agent Test

### Objective
Validate the complete flow with two actual Claude Code instances communicating.

### Implementation Guidance

Create test script `test_e2e.sh` that:
1. Starts coordinator (docker-compose)
2. Opens two terminal sessions with different agent IDs
3. Provides test instructions for each terminal

Test scenarios:
1. **Simple request/response**: Agent A asks question, Agent B answers
2. **Multi-turn**: Agent A asks, B responds, A follows up, B responds again
3. **Timeout**: Agent A asks offline agent, gets timeout
4. **Interrupt**: Human interrupts Agent B mid-processing

Create `tests/TESTING.md` with manual test procedures.

### Test Requirements
- All four scenarios complete successfully
- Latency < 10 seconds for simple request/response
- Human interrupt works as expected

### Integration
Validates all previous steps working together.

### Demo
Full demonstration of two agents collaborating:
```
Terminal 1 (homeassistant):
> "Ask the meshtastic agent what MQTT topics are available"
[Sends request, waits...]
[Receives response]
> "The meshtastic agent says topics are: mesh/node/#, mesh/stat/#"

Terminal 2 (meshtastic):
[Stop hook fires when finishing other work]
[Claude sees pending request, processes it]
[Sends response automatically]
```

---

## Step 12: Error Handling and Graceful Degradation

### Objective
Ensure robust error handling throughout the system.

### Implementation Guidance

Coordinator improvements:
- Add request validation (missing fields, invalid agent IDs)
- Add rate limiting (max 10 requests/minute per agent)
- Add request expiration (24h TTL on inbox items)
- Improve error messages with actionable suggestions

Plugin improvements:
- Hooks fail silently (don't break CC)
- MCP tools return structured errors: `{"error": "...", "code": "...", "suggestion": "..."}`
- Add `/coordinate status` skill to check connection

Error codes:
- `COORD_UNAVAILABLE`: Coordinator not reachable
- `AGENT_NOT_FOUND`: Target agent doesn't exist
- `AGENT_BUSY`: Target agent is busy/away
- `TIMEOUT`: Request timed out
- `INVALID_REQUEST`: Malformed request

### Test Requirements
- Each error code is returned appropriately
- Hooks don't crash on any error condition
- Rate limiting works correctly
- Old messages expire

### Integration
Hardens all previous steps for production use.

### Demo
```bash
# Test graceful degradation:
# 1. Stop coordinator
# 2. Try to use c3po tools in CC
# 3. Get clear error message, CC continues to work
# 4. Restart coordinator
# 5. Tools work again without restarting CC
```

---

## Step 13: /coordinate Skill and Documentation

### Objective
Add user-facing skill for common coordination tasks and complete documentation.

### Implementation Guidance

Create `plugin/skills/coordinate/SKILL.md`:
```markdown
# /coordinate

Manage agent coordination.

## Usage

- `/coordinate status` - Check connection and list online agents
- `/coordinate agents` - List all agents with their capabilities
- `/coordinate send <agent> <message>` - Send a quick message to another agent

## Examples

Check who's online:
> /coordinate status

Ask another agent for help:
> /coordinate send homeassistant "What automations are running?"
```

Create/update documentation:
- `README.md` - Quick start guide
- `docs/SETUP.md` - Detailed setup instructions
- `docs/USAGE.md` - User guide with examples
- `docs/TROUBLESHOOTING.md` - Common issues and solutions

### Test Requirements
- Skill parses arguments correctly
- Status shows accurate information
- Documentation is accurate and complete

### Integration
Final user-facing polish on all previous work.

### Demo
```bash
# In Claude Code with plugin:
> /coordinate status
c3po Status:
  Coordinator: http://nas:8420 (connected)
  Agent ID: homeassistant
  Online agents: homeassistant, meshtastic, mediaserver

> /coordinate send meshtastic "What nodes are online?"
Sent request to meshtastic. Waiting for response...
Response from meshtastic: "Nodes online: node-1234, node-5678, node-9abc"
```

---

## Implementation Notes

### Development Environment

```bash
# Prerequisites
python 3.11+
docker & docker-compose
Claude Code with plugin support

# Quick start
cd coordinator
pip install -r requirements.txt
docker run -d -p 6379:6379 redis:7-alpine
python server.py

# In another terminal
cd plugin
export C3PO_COORDINATOR_URL=http://localhost:8420
export C3PO_AGENT_ID=test-agent
# Test with Claude Code
```

### Key Files to Create

| Step | Files |
|------|-------|
| 1 | `coordinator/server.py`, `requirements.txt` |
| 2 | `coordinator/agents.py` |
| 3-5 | `coordinator/messaging.py` |
| 6 | Update `server.py` with REST routes |
| 7 | `Dockerfile`, `docker-compose.yml` |
| 8 | `plugin/.claude-plugin/plugin.json`, `plugin/.mcp.json` |
| 9 | `plugin/hooks/check_inbox.py` |
| 10 | `plugin/hooks/register_agent.py` |
| 11 | `tests/TESTING.md`, `test_e2e.sh` |
| 12 | Updates across all files |
| 13 | `plugin/skills/coordinate/SKILL.md`, `docs/*` |

### Recommended Order

Follow the steps sequentially. Each step produces a working, testable increment. Don't skip ahead - the demos at each step validate your work before building more.
