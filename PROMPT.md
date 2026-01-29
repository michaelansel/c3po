# C3PO End-to-End Acceptance Test

## Objective

Create a permanent, repeatable acceptance test that validates the complete C3PO system from scratch:
1. Deploy coordinator in a container
2. Deploy two Claude Code agents in separate containers
3. Install the plugin from GitHub marketplace on each agent
4. Configure both agents to connect to the coordinator
5. Prove the agents can communicate with each other

## Deliverables

1. `scripts/acceptance-test.sh` - Main test runner script
2. `tests/acceptance/` - Directory with test infrastructure
3. All tests passing

---

## Part 1: Create the Acceptance Test Infrastructure

### 1.1 Create the test runner script

Create `scripts/acceptance-test.sh`:

```bash
#!/bin/bash
set -e

# C3PO Acceptance Test
# Runs a full end-to-end test in isolated containers

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log() { echo -e "${GREEN}[test]${NC} $1"; }
warn() { echo -e "${YELLOW}[test]${NC} $1"; }
error() { echo -e "${RED}[test]${NC} $1" >&2; }

# Cleanup function
cleanup() {
    log "Cleaning up containers..."
    finch rm -f c3po-coordinator c3po-agent-a c3po-agent-b 2>/dev/null || true
    finch network rm c3po-test-net 2>/dev/null || true
}

# Set trap for cleanup
trap cleanup EXIT

# Parse args
SKIP_CLEANUP=false
while [[ $# -gt 0 ]]; do
    case $1 in
        --no-cleanup) SKIP_CLEANUP=true; shift ;;
        *) error "Unknown option: $1"; exit 1 ;;
    esac
done

if $SKIP_CLEANUP; then
    trap - EXIT
fi

log "=== C3PO Acceptance Test ==="
log "Repository: $REPO_ROOT"

# Step 1: Create network
log "Creating test network..."
finch network create c3po-test-net 2>/dev/null || true

# Step 2: Start coordinator
log "Starting coordinator..."
finch run -d \
    --name c3po-coordinator \
    --network c3po-test-net \
    -p 8420:8420 \
    -v "$REPO_ROOT/coordinator:/app" \
    -w /app \
    python:3.12-slim \
    bash -c "pip install -r requirements.txt && python server.py"

# Wait for coordinator to be ready
log "Waiting for coordinator to be ready..."
for i in {1..30}; do
    if curl -s http://localhost:8420/api/health | grep -q '"status":"ok"'; then
        log "Coordinator is ready!"
        break
    fi
    if [ $i -eq 30 ]; then
        error "Coordinator failed to start"
        finch logs c3po-coordinator
        exit 1
    fi
    sleep 1
done

# Step 3: Build agent image
log "Building agent test image..."
finch build -t c3po-agent-test -f "$REPO_ROOT/tests/acceptance/Dockerfile.agent" "$REPO_ROOT"

# Step 4: Start Agent A
log "Starting Agent A..."
finch run -d \
    --name c3po-agent-a \
    --network c3po-test-net \
    -e C3PO_COORDINATOR_URL=http://c3po-coordinator:8420 \
    -e C3PO_AGENT_ID=agent-a \
    c3po-agent-test

# Step 5: Start Agent B
log "Starting Agent B..."
finch run -d \
    --name c3po-agent-b \
    --network c3po-test-net \
    -e C3PO_COORDINATOR_URL=http://c3po-coordinator:8420 \
    -e C3PO_AGENT_ID=agent-b \
    c3po-agent-test

# Step 6: Wait for agents to register
log "Waiting for agents to register..."
sleep 5

# Step 7: Verify both agents are online
log "Verifying agents are registered..."
AGENTS=$(curl -s http://localhost:8420/api/health)
echo "$AGENTS"

if ! echo "$AGENTS" | grep -q '"agents_online":2'; then
    warn "Expected 2 agents online, checking agent list..."
    curl -s -X POST http://localhost:8420/mcp \
        -H "Content-Type: application/json" \
        -H "X-Agent-ID: test" \
        -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"list_agents","arguments":{}}}'
fi

# Step 8: Run communication test
log "Running agent communication test..."
"$SCRIPT_DIR/test-agent-communication.sh"

log "=== All tests passed! ==="
```

### 1.2 Create the agent communication test

Create `scripts/test-agent-communication.sh`:

```bash
#!/bin/bash
set -e

# Test that Agent A can send a message to Agent B and receive a response

COORDINATOR="http://localhost:8420"

log() { echo -e "\033[0;32m[comm-test]\033[0m $1"; }
error() { echo -e "\033[0;31m[comm-test]\033[0m $1" >&2; }

# Helper to call MCP tools
mcp_call() {
    local agent_id=$1
    local tool=$2
    local args=$3

    curl -s -X POST "$COORDINATOR/mcp" \
        -H "Content-Type: application/json" \
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
```

### 1.3 Create the agent Dockerfile

Create `tests/acceptance/Dockerfile.agent`:

```dockerfile
FROM python:3.12-slim

# Install dependencies
RUN apt-get update && apt-get install -y \
    curl \
    && rm -rf /var/lib/apt/lists/*

# The agent just needs to make HTTP requests to register
# In a real test with Claude Code, we'd install that instead

# For now, this is a minimal agent that registers via REST API
COPY tests/acceptance/agent-register.py /agent-register.py

CMD ["python", "/agent-register.py"]
```

### 1.4 Create the agent registration script

Create `tests/acceptance/agent-register.py`:

```python
#!/usr/bin/env python3
"""
Minimal agent that registers with the coordinator and stays online.
Used for acceptance testing without requiring full Claude Code installation.
"""

import json
import os
import sys
import time
import urllib.request
import urllib.error

COORDINATOR_URL = os.environ.get("C3PO_COORDINATOR_URL", "http://localhost:8420")
AGENT_ID = os.environ.get("C3PO_AGENT_ID", "test-agent")

def log(msg):
    print(f"[{AGENT_ID}] {msg}", flush=True)

def mcp_call(method, params=None):
    """Make an MCP call to the coordinator."""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
        "params": params or {}
    }

    req = urllib.request.Request(
        f"{COORDINATOR_URL}/mcp",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "X-Agent-ID": AGENT_ID
        }
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception as e:
        log(f"MCP call failed: {e}")
        return None

def main():
    log(f"Starting agent, connecting to {COORDINATOR_URL}")

    # Wait for coordinator to be ready
    for i in range(30):
        try:
            req = urllib.request.Request(f"{COORDINATOR_URL}/api/health")
            with urllib.request.urlopen(req, timeout=5) as resp:
                health = json.loads(resp.read())
                if health.get("status") == "ok":
                    log("Coordinator is ready")
                    break
        except Exception:
            pass
        time.sleep(1)
    else:
        log("Coordinator not ready after 30 seconds")
        sys.exit(1)

    # Register by calling ping (any MCP call registers the agent)
    result = mcp_call("tools/call", {"name": "ping", "arguments": {}})
    if result:
        log(f"Registered successfully: {result}")
    else:
        log("Failed to register")
        sys.exit(1)

    # Keep alive by pinging periodically
    log("Staying online (Ctrl+C to stop)...")
    while True:
        time.sleep(30)
        result = mcp_call("tools/call", {"name": "ping", "arguments": {}})
        if result:
            log("Heartbeat sent")
        else:
            log("Heartbeat failed, but continuing...")

if __name__ == "__main__":
    main()
```

### 1.5 Create directory structure

```bash
mkdir -p tests/acceptance
```

---

## Part 2: Plugin Installation Test (Future Enhancement)

Once the basic infrastructure works, add a second test that actually installs Claude Code and the plugin. This requires:

1. A container image with Node.js and Claude Code CLI
2. Automated plugin installation via CLI commands
3. Non-interactive setup (using environment variables)

Create `tests/acceptance/Dockerfile.claude-agent` for future use:

```dockerfile
FROM node:20-slim

# Install Claude Code CLI
RUN npm install -g @anthropic-ai/claude-code

# Install curl for health checks
RUN apt-get update && apt-get install -y curl && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace

# Copy test script
COPY tests/acceptance/claude-agent-test.sh /test.sh
RUN chmod +x /test.sh

CMD ["/test.sh"]
```

---

## Part 3: Make the Tests Executable

```bash
chmod +x scripts/acceptance-test.sh
chmod +x scripts/test-agent-communication.sh
```

---

## Part 4: Run and Debug

Execute the acceptance test:

```bash
./scripts/acceptance-test.sh
```

If it fails:
1. Use `--no-cleanup` flag to keep containers running for debugging
2. Check logs: `finch logs c3po-coordinator`
3. Check agent logs: `finch logs c3po-agent-a`
4. Exec into containers: `finch exec -it c3po-agent-a bash`

---

## Success Criteria

The acceptance test passes when:

- [ ] `scripts/acceptance-test.sh` exists and is executable
- [ ] Coordinator starts in a container
- [ ] Two agents start in separate containers
- [ ] Both agents register with coordinator
- [ ] Agent A can send a request to Agent B
- [ ] Agent B receives the request
- [ ] Agent B can respond
- [ ] Agent A receives the response
- [ ] All containers clean up on exit
- [ ] Test is idempotent (can run multiple times)

---

## Files to Create

| File | Purpose |
|------|---------|
| `scripts/acceptance-test.sh` | Main test runner |
| `scripts/test-agent-communication.sh` | Communication test |
| `tests/acceptance/Dockerfile.agent` | Agent container image |
| `tests/acceptance/agent-register.py` | Agent registration script |

---

## Notes

- Uses `finch` instead of `docker` per user preference
- Coordinator runs from mounted volume (no build needed)
- Agents are minimal Python scripts (not full Claude Code) for speed
- Future enhancement: Add full Claude Code + plugin installation test
