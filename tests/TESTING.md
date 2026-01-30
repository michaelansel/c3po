# C3PO End-to-End Testing Guide

This document describes manual test procedures for validating C3PO's multi-agent coordination functionality.

## Prerequisites

1. Python 3.14+ with venv
2. finch or docker for Redis container
3. Claude Code installed

## Quick Start

```bash
# Start the local test environment
./scripts/test-local.sh start

# Verify it's running
./scripts/test-local.sh status

# Check the health endpoint
curl http://localhost:8420/api/health
```

## Test Scenarios

### Scenario 1: Simple Request/Response

**Objective**: Verify that Agent A can send a request to Agent B and receive a response.

**Setup**:
```bash
# Terminal 1: Start environment
./scripts/test-local.sh start

# Terminal 2: Start Agent A
./scripts/test-local.sh agent-a

# Terminal 3: Start Agent B
./scripts/test-local.sh agent-b
```

**Test Steps**:

1. **In Agent A's Claude Code session**:
   - Use `list_agents` tool to verify both agents are registered
   - Expected: Should see `agent-a` and `agent-b` in the list

2. **In Agent A**: Send a request to Agent B:
   - Use `send_request` with target="agent-b", message="What is 2+2?"
   - Note the returned `request_id`
   - Use `wait_for_response` with the request_id (timeout=30)

3. **In Agent B**: Process the request:
   - Use `get_pending_requests` - should see the request from agent-a
   - Use `respond_to_request` with the request_id and response="The answer is 4"

4. **In Agent A**: Verify response:
   - `wait_for_response` should return with the response from Agent B

**Expected Results**:
- Agent A receives response with content "The answer is 4"
- Latency should be < 10 seconds
- No errors in coordinator logs

---

### Scenario 2: Multi-Turn Conversation

**Objective**: Verify agents can have a back-and-forth conversation.

**Test Steps**:

1. **Agent A**: Ask an initial question
   ```
   send_request(target="agent-b", message="I need help debugging. What info do you need?")
   wait_for_response(request_id=..., timeout=60)
   ```

2. **Agent B**: Respond and ask for details
   ```
   get_pending_requests()
   respond_to_request(request_id=..., response="Please share the error message and stack trace")
   ```

3. **Agent A**: Provide details (new request)
   ```
   send_request(target="agent-b", message="Error: TypeError at line 42", context="Previous conversation about debugging")
   wait_for_response(request_id=..., timeout=60)
   ```

4. **Agent B**: Provide solution
   ```
   get_pending_requests()
   respond_to_request(request_id=..., response="Check if the variable is None before accessing .items()")
   ```

**Expected Results**:
- Both exchanges complete successfully
- Request IDs are unique for each message
- Context is preserved through the conversation

---

### Scenario 3: Request Timeout

**Objective**: Verify that waiting for a response from an unresponsive agent times out gracefully.

**Test Steps**:

1. **Agent A**: Send request to a registered but non-responding agent
   ```
   send_request(target="agent-b", message="Are you there?")
   wait_for_response(request_id=..., timeout=10)  # Short timeout
   ```

2. **Agent B**: Do NOT respond (let it timeout)

**Expected Results**:
- `wait_for_response` returns with `status: "timeout"` after 10 seconds
- No crash or error - just a timeout indicator
- Agent A can continue with other work

---

### Scenario 4: Human Interrupt

**Objective**: Verify that pressing Esc/Ctrl-C during a wait interrupts correctly.

**Test Steps**:

1. **Agent A**: Start a long wait
   ```
   wait_for_response(request_id="non-existent", timeout=300)
   ```

2. **Human**: Press Esc or Ctrl-C during the wait

**Expected Results**:
- Wait is interrupted
- Claude Code returns control to the human
- System remains in a valid state
- Subsequent requests work normally

---

### Scenario 5: Stop Hook Trigger

**Objective**: Verify the Stop hook blocks Claude when requests are pending.

**Setup**: Ensure the c3po plugin is installed with hooks enabled.

**Test Steps**:

1. **Agent A**: Send request to Agent B while Agent B is working on something else
   ```
   send_request(target="agent-b", message="Need your help when you're free")
   ```

2. **Agent B**: Complete current task and let Claude try to stop
   - The Stop hook should detect the pending request
   - Claude should be informed of the pending request
   - Claude should process it with `get_pending_requests` and `respond_to_request`

**Expected Results**:
- Stop hook fires when Claude attempts to complete a turn
- Claude is instructed to check pending requests
- After responding, Claude can stop normally

---

### Scenario 6: Graceful Degradation

**Objective**: Verify the system continues working when coordinator is unavailable.

**Test Steps**:

1. **Start normally**:
   ```bash
   ./scripts/test-local.sh start
   ./scripts/test-local.sh agent-a
   ```

2. **Stop coordinator**:
   ```bash
   ./scripts/test-local.sh stop
   ```

3. **Try c3po tools in Agent A**:
   - `list_agents` should return an error indicating coordinator unavailable
   - Claude Code should continue to work for non-c3po tasks

4. **Restart coordinator**:
   ```bash
   ./scripts/test-local.sh start
   ```

5. **Try c3po tools again**:
   - `list_agents` should work again
   - No Claude Code restart required

**Expected Results**:
- Error messages are clear and actionable
- Claude Code does not crash
- Recovery is automatic when coordinator comes back

---

## Automated Tests

### Containerized Acceptance Tests (recommended)

The most comprehensive automated validation. Builds Docker images, spins up Redis + coordinator + host agent containers in an isolated network, and runs all acceptance test phases inside the containers. This validates the actual deployment artifact.

```bash
# Full containerized acceptance test (uses docker or finch)
bash tests/acceptance/run-acceptance.sh

# Keep containers running after test for debugging
bash tests/acceptance/run-acceptance.sh --no-cleanup

# Run specific phase only
bash tests/acceptance/run-acceptance.sh --phase 5
```

**Any change to coordinator code should run this before merging.**

### Acceptance Tests Against a Live Coordinator

For faster iteration when the coordinator is already deployed (does not validate the built image):

```bash
python3 tests/acceptance/test_acceptance.py --coordinator-url http://<host>:8420
```

### Unit Tests

```bash
# Coordinator unit tests
python3 -m pytest coordinator/tests/ -v

# Hook unit tests
cd plugin/hooks && pytest tests/ -v
```

### E2E Integration Test

For automated testing without Claude Code, use the E2E test script:

```bash
./scripts/test_e2e.sh
```

This script:
1. Starts Redis and coordinator
2. Simulates Agent A and Agent B using curl/MCP protocol
3. Validates request/response flow
4. Reports pass/fail status

---

## Troubleshooting

### Coordinator not responding
```bash
./scripts/test-local.sh logs
# Check for Redis connection errors or port conflicts
```

### Agent not appearing in list_agents
- Verify `X-Agent-ID` header is set
- Check that agent made a recent tool call (heartbeat timeout is 90s)

### Requests not being received
```bash
curl http://localhost:8420/api/pending -H "X-Agent-ID: agent-b"
# Check if requests are in the inbox
```

### Stop hook not triggering
- Verify plugin hooks are properly configured
- Check hook timeout settings in `hooks.json`
- Ensure `C3PO_COORDINATOR_URL` and `C3PO_AGENT_ID` env vars are set

---

## Performance Expectations

| Metric | Target |
|--------|--------|
| Request delivery latency | < 500ms |
| Response roundtrip | < 2s |
| Stop hook check | < 1s |
| Agent registration | < 500ms |
| list_agents response | < 200ms |
