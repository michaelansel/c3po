# C3PO Acceptance Test Specification

This spec describes a sequential acceptance test that starts from a completely clean environment and validates the full C3PO multi-agent coordination system. Each phase has numbered steps with actions and pass/fail assertions.

Items tagged `[ASPIRATIONAL]` are not yet known to work. Delete the tags as they are validated.

---

## Container Environment

The acceptance test runs in three containers orchestrated by Docker Compose. All containers share a dedicated bridge network, which provides automatic DNS resolution by service name.

### docker-compose.acceptance.yml

```yaml
services:
  coordinator:
    build:
      context: ../../coordinator
    environment:
      - REDIS_URL=redis://redis:6379
    depends_on:
      redis:
        condition: service_healthy
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8420/api/health')"]
      interval: 5s
      timeout: 5s
      retries: 10
      start_period: 5s

  redis:
    image: redis:7-alpine
    command: redis-server --appendonly no
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      timeout: 3s
      retries: 5

  host-a:
    build:
      context: ../..
      dockerfile: tests/acceptance/Dockerfile.cc-agent
    environment:
      - ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}
      - C3PO_COORDINATOR_URL=http://coordinator:8420
    depends_on:
      coordinator:
        condition: service_healthy
    stdin_open: true
    tty: true

  host-b:
    build:
      context: ../..
      dockerfile: tests/acceptance/Dockerfile.cc-agent
    environment:
      - ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}
      - C3PO_COORDINATOR_URL=http://coordinator:8420
    depends_on:
      coordinator:
        condition: service_healthy
    stdin_open: true
    tty: true
```

### Dockerfile.cc-agent

A clean-room Claude Code agent container with no pre-existing c3po configuration:

```dockerfile
FROM node:20-bookworm

# System dependencies
RUN apt-get update && apt-get install -y \
    curl \
    git \
    python3 \
    python3-pip \
    && rm -rf /var/lib/apt/lists/*

# Install Claude Code CLI
RUN npm install -g @anthropic-ai/claude-code

# Claude Code requires non-root user
RUN useradd -m -s /bin/bash testuser
USER testuser
WORKDIR /home/testuser

# Create a fake project directory (for agent identity)
RUN mkdir -p /home/testuser/test-project
WORKDIR /home/testuser/test-project
RUN git init

CMD ["sleep", "infinity"]
```

### Running the Environment

```bash
# From tests/acceptance/
export ANTHROPIC_API_KEY="sk-ant-..."

# Start coordinator + redis, wait for healthy, then start agents
docker compose -f docker-compose.acceptance.yml up -d

# Verify coordinator is up
docker compose -f docker-compose.acceptance.yml exec host-a \
  curl -sf http://coordinator:8420/api/health

# Open interactive shells to run test phases
docker compose -f docker-compose.acceptance.yml exec host-a bash   # Terminal 1
docker compose -f docker-compose.acceptance.yml exec host-b bash   # Terminal 2

# Teardown
docker compose -f docker-compose.acceptance.yml down -v
```

### Networking

- All services share the default Compose bridge network.
- Containers resolve each other by service name: `coordinator`, `redis`, `host-a`, `host-b`.
- The coordinator listens on port 8420 inside the network (no host port mapping needed).
- `host-a` and `host-b` reach the coordinator at `http://coordinator:8420`.

---

## Phase 0: Prerequisites

**Goal**: Verify the container environment is clean and the coordinator is reachable.

1. **Action**: Start the environment: `docker compose -f docker-compose.acceptance.yml up -d`
   - **Assert**: All containers start without errors
   - **Assert**: `docker compose -f docker-compose.acceptance.yml ps` shows coordinator and redis as "healthy"
2. **Action**: From host-a: `curl http://coordinator:8420/api/health`
   - **Assert**: Returns `{"status": "ok"}`
3. **Action**: From host-a: `claude mcp list`
   - **Assert**: Output does NOT contain "c3po" (clean slate)

---

## Phase 1: Plugin Installation

**Goal**: Install the C3PO plugin and verify it is present but not yet configured.

Repeat on both host-a and host-b:

1. **Action**: `claude plugin marketplace add michaelansel/c3po`
   - **Assert**: Command succeeds without errors
2. **Action**: `claude plugin install c3po`
   - **Assert**: Command succeeds without errors
3. **Action**: `claude plugin list`
   - **Assert**: Shows c3po in the list
   - **Assert**: No errors or warnings
4. **Action**: `claude mcp list`
   - **Assert**: Does NOT show c3po (install alone should not configure MCP)

---

## Phase 2: Setup / MCP Configuration

**Goal**: Configure the MCP server so Claude Code can communicate with the coordinator.

Repeat on both host-a and host-b:

1. **Action**: Run setup to configure MCP: `/c3po setup`
   - The coordinator URL is `http://coordinator:8420` (Docker DNS)
   - **Assert**: Command completes without errors
2. **Action**: `claude mcp list`
   - **Assert**: Shows c3po with status "Connected"

---

## Phase 3: Single Agent Session

**Goal**: Verify a single Claude Code session on host-a can connect and use basic tools.

1. **Action**: On host-a, start a Claude Code session with the plugin active
   - **Assert**: Session startup output includes c3po connection message
2. **Action**: Call `ping` tool
   - **Assert**: Returns pong with a timestamp
3. **Action**: Call `list_agents` tool
   - **Assert**: Returns at least 1 agent (self)

---

## Phase 4: Two-Agent Registration

**Goal**: Verify two agents on separate hosts can connect simultaneously with distinct identities.

1. **Action**: On host-b, start a Claude Code session with the plugin active
   - **Assert**: Session starts without errors
   - **Assert**: Session startup output includes c3po connection message
2. **Action**: Call `list_agents` from either host
   - **Assert**: Returns 2 agents online
3. **Assert**: Agent IDs are distinct

---

## Phase 5: Request/Response Roundtrip

**Goal**: Verify the full send-request / get-pending / respond / wait-for-response cycle between host-a and host-b.

1. **Action**: Agent on host-a calls `send_request(target=<host-b-agent-id>, message="What is 2+2?")`
   - **Assert**: Returns a request ID (non-empty string)
2. **Action**: Agent on host-b calls `get_pending_requests()`
   - **Assert**: Returns the request from host-a's agent with the message "What is 2+2?"
3. **Action**: Agent on host-b calls `respond_to_request(request_id=..., response="4")`
   - **Assert**: Returns success status
4. **Action**: Agent on host-a calls `wait_for_response(request_id=..., timeout=30)`
   - **Assert**: Returns the response "4"

---

## Phase 5: Acknowledgment and Compaction

**Goal**: Verify that acknowledged messages are removed from the queue and compaction works correctly.

Repeat on both host-a and host-b:

1. **Action**: From host-a, send 25 messages to host-b using the c3po MCP tool
   - **Assert**: All messages are queued successfully
2. **Action**: From host-b, receive all 25 messages using `wait_for_message`
   - **Assert**: All 25 messages are received in order
3. **Action**: From host-b, acknowledge all 25 messages using `ack_messages`
   - **Assert**: No errors returned
4. **Action**: From host-b, call `get_messages` again
   - **Assert**: Returns empty list (all messages removed)
5. **Action**: From host-a, send 3 more messages to host-b
   - **Assert**: All 3 messages are queued
6. **Action**: From host-b, receive and acknowledge the 3 messages
   - **Assert**: All 3 messages are removed after ack

---

## Phase 6: Blocking Wait Behavior

**Goal**: Verify that blocking wait calls handle timeouts gracefully without crashing.

1. **Action**: Agent A calls `wait_for_response(request_id="nonexistent", timeout=5)`
   - **Assert**: Returns timeout status after ~5 seconds (not a crash or unhandled error)
2. **Action**: Agent A calls `wait_for_request(timeout=5)`
   - **Assert**: Returns timeout status after ~5 seconds (not a crash or unhandled error)

---

## Phase 7: Stop Hook - Inbox Check

**Goal**: Verify the stop hook prevents an agent from exiting when it has unprocessed requests.

1. **Action**: Agent A sends a request to Agent B
2. **Action**: Agent B completes a task (Claude tries to stop)
3. **Assert**: Stop hook blocks the stop and tells Claude about the pending request
4. **Action**: Agent B processes and responds to the request
5. **Assert**: Stop hook allows the stop on the next attempt

---

## Phase 8: Task Delegation (End-to-End)

**Goal**: Verify that a human can instruct one agent to delegate work to another and get results back.

1. **Action**: Human tells Agent A: "ask Agent B to write a 50 word short story and send it back"
2. **Assert**: Agent A sends a request to Agent B
3. **Assert**: Agent B receives, processes, and responds with a story
4. **Assert**: Agent A receives and presents the story to the human

---

## Phase 9: Error Cases

**Goal**: Verify the system handles errors gracefully without crashing Claude Code.

1. **Action**: Call `send_request(target="nonexistent-agent", message="hello")`
   - **Assert**: Returns an error response (not a crash)
2. **Action**: Call `respond_to_request(request_id="fake-id", response="nope")`
   - **Assert**: Returns an error response (not a crash)
3. **Action**: Stop the coordinator container: `docker compose -f docker-compose.acceptance.yml stop coordinator`
4. **Action**: From host-a, call `list_agents`
   - **Assert**: Returns a connection error
   - **Assert**: Claude Code session continues working (does not crash)
5. **Action**: Restart the coordinator: `docker compose -f docker-compose.acceptance.yml start coordinator`

---

## Phase 10: Teardown

**Goal**: Verify clean shutdown and uninstallation.

1. **Action**: End host-b's Claude Code session
2. **Action**: Call `list_agents` from host-a
   - **Assert**: host-b's agent is shown as offline or absent
3. **Action**: On host-a: `claude plugin uninstall c3po`
   - **Assert**: `claude plugin list` does not show c3po
4. **Action**: `claude mcp list`
   - **Assert**: Does not show c3po
5. **Action**: Destroy the environment: `docker compose -f docker-compose.acceptance.yml down -v`
   - **Assert**: All containers and volumes are removed
