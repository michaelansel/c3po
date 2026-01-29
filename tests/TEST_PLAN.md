# C3PO Comprehensive Test Plan

This document defines all test cases for the C3PO multi-agent coordination system. Each test has a unique ID for tracking and status reporting.

## Test Categories

1. **Unit Tests** - Test individual components in isolation
2. **Integration Tests** - Test component interactions with dependencies
3. **End-to-End Tests** - Test complete user workflows
4. **Error Handling Tests** - Test failure scenarios and recovery
5. **Performance Tests** - Test latency and scalability

---

## 1. Unit Tests

### 1.1 Coordinator: Agent Management

| ID | Test | Description | Expected Result | Automated |
|----|------|-------------|-----------------|-----------|
| U-AGT-001 | Agent registration | Register new agent with valid ID | Agent stored in registry with timestamp | Yes |
| U-AGT-002 | Agent ID validation | Reject invalid agent IDs (empty, special chars) | Validation error returned | Yes |
| U-AGT-003 | Agent listing | List all registered agents | Returns list with online/offline status | Yes |
| U-AGT-004 | Agent heartbeat | Update agent heartbeat timestamp | Last_seen updated | Yes |
| U-AGT-005 | Agent offline detection | Agent offline after 90s no heartbeat | Status shows offline | Yes |
| U-AGT-006 | Duplicate registration | Re-register existing agent | Updates existing record | Yes |
| U-AGT-007 | Agent removal | Remove agent from registry | Agent no longer in list | Yes |

**Location**: `coordinator/tests/test_agents.py`

### 1.2 Coordinator: Messaging

| ID | Test | Description | Expected Result | Automated |
|----|------|-------------|-----------------|-----------|
| U-MSG-001 | Send request | Send request with target, message, context | Request stored in target inbox | Yes |
| U-MSG-002 | Request ID format | Verify request ID format | ID is {from}::{to}::{uuid8} | Yes |
| U-MSG-003 | Get pending requests | Retrieve inbox contents | Returns list of requests | Yes |
| U-MSG-004 | Request consumption | Getting requests clears inbox | Second get returns empty | Yes |
| U-MSG-005 | Respond to request | Send response to request | Response stored for sender | Yes |
| U-MSG-006 | Response retrieval | Get response by request ID | Returns response with status | Yes |
| U-MSG-007 | Message expiration | Messages expire after 24h | Expired messages not returned | Yes |
| U-MSG-008 | Rate limiting | Exceed 10 req/min limit | Rate limit error returned | Yes |
| U-MSG-009 | Rate limit window | Requests allowed after window | Requests succeed again | Yes |
| U-MSG-010 | Request to offline agent | Send to agent not registered | Returns agent_not_found error | Yes |

**Location**: `coordinator/tests/test_messaging.py`

### 1.3 Coordinator: Error Handling

| ID | Test | Description | Expected Result | Automated |
|----|------|-------------|-----------------|-----------|
| U-ERR-001 | Invalid request ID | Respond to non-existent request | Error code INVALID_REQUEST | Yes |
| U-ERR-002 | Missing target | Send request without target | Error code INVALID_REQUEST | Yes |
| U-ERR-003 | Missing message | Send request without message | Error code INVALID_REQUEST | Yes |
| U-ERR-004 | Agent not found | Send to non-existent agent | Error code AGENT_NOT_FOUND | Yes |
| U-ERR-005 | Self-message | Send request to self | Error code INVALID_REQUEST | Yes |
| U-ERR-006 | Timeout error | Wait exceeds timeout | Error code TIMEOUT with status | Yes |

**Location**: `coordinator/tests/test_errors.py`

### 1.4 Coordinator: REST API

| ID | Test | Description | Expected Result | Automated |
|----|------|-------------|-----------------|-----------|
| U-REST-001 | Health endpoint | GET /api/health | Returns status=ok, agents_online | Yes |
| U-REST-002 | Pending endpoint | GET /api/pending with agent header | Returns count and requests | Yes |
| U-REST-003 | Pending without header | GET /api/pending without header | Returns 400 error | Yes |
| U-REST-004 | Pending count accuracy | Check count matches inbox | Count equals actual requests | Yes |

**Location**: `coordinator/tests/test_rest_api.py`

### 1.5 Plugin: Hooks

| ID | Test | Description | Expected Result | Automated |
|----|------|-------------|-----------------|-----------|
| U-HOOK-001 | SessionStart success | Coordinator available | Shows connection + agent count | Yes |
| U-HOOK-002 | SessionStart offline | Coordinator unavailable | Shows warning, exits 0 | Yes |
| U-HOOK-003 | Stop empty inbox | No pending requests | Exits 0 (allow stop) | Yes |
| U-HOOK-004 | Stop with pending | Requests in inbox | Exits 1 (block stop), shows message | Yes |
| U-HOOK-005 | Stop hook loop prevention | Re-entry detection | Exits 0 to prevent infinite loop | Yes |
| U-HOOK-006 | Stop coordinator down | Coordinator unavailable | Exits 0 (allow stop) | Yes |

**Location**: `plugin/hooks/tests/test_register_agent.py`, `plugin/hooks/tests/test_check_inbox.py`

---

## 2. Integration Tests

### 2.1 Coordinator + Redis

| ID | Test | Description | Steps | Expected Result | Automated |
|----|------|-------------|-------|-----------------|-----------|
| I-REDIS-001 | Agent persistence | Store and retrieve agent | 1. Register agent 2. Restart coordinator 3. List agents | Agent still present | No |
| I-REDIS-002 | Message persistence | Message survives restart | 1. Send request 2. Restart coordinator 3. Get pending | Request available | No |
| I-REDIS-003 | Concurrent writes | Multiple agents register simultaneously | 1. 10 agents register in parallel | All agents registered | Yes |
| I-REDIS-004 | Redis connection loss | Redis disconnects | 1. Send request 2. Kill Redis 3. Send another | Error indicates Redis unavailable | No |
| I-REDIS-005 | Redis reconnection | Redis comes back | 1. Kill Redis 2. Restart Redis 3. Send request | Request succeeds | No |

### 2.2 MCP Protocol

| ID | Test | Description | Steps | Expected Result | Automated |
|----|------|-------------|-------|-----------------|-----------|
| I-MCP-001 | Tool discovery | List available tools | 1. Connect MCP client 2. List tools | All 7 tools returned | Yes |
| I-MCP-002 | Tool invocation | Call ping tool | 1. Connect 2. Call ping() | Returns pong + timestamp | Yes |
| I-MCP-003 | Tool with args | Call send_request | 1. Connect 2. Call with target/message | Returns request_id | Yes |
| I-MCP-004 | Header extraction | X-Agent-ID propagation | 1. Connect with header 2. list_agents | Caller appears in list | Yes |
| I-MCP-005 | Session management | MCP session lifecycle | 1. Connect 2. Use tools 3. Disconnect | Clean disconnect, no orphan state | Yes |

**Location**: `tests/test_e2e_integration.py` (with C3PO_TEST_LIVE=1)

### 2.3 Hook Execution

| ID | Test | Description | Steps | Expected Result | Automated |
|----|------|-------------|-------|-----------------|-----------|
| I-HOOK-001 | SessionStart integration | Hook runs on session start | 1. Start Claude Code with plugin 2. Check output | Connection message displayed | Manual |
| I-HOOK-002 | Stop hook integration | Hook runs before stop | 1. Send request to agent 2. Agent tries to stop | Stop blocked, message shown | Manual |
| I-HOOK-003 | Env var propagation | C3PO_* vars available | 1. Set env vars 2. Run hooks | Hooks use correct URL and agent ID | Yes |

---

## 3. End-to-End Tests

### 3.1 Two-Agent Communication

| ID | Test | Description | Steps | Expected Result | Automated |
|----|------|-------------|-------|-----------------|-----------|
| E2E-001 | Simple request/response | Agent A asks, Agent B answers | 1. A: send_request 2. B: get_pending 3. B: respond 4. A: wait_for_response | A receives B's response | Partial |
| E2E-002 | Multi-turn conversation | 3+ message exchange | 1. A asks 2. B responds 3. A asks follow-up 4. B responds 5. A asks again 6. B responds | All exchanges complete | Manual |
| E2E-003 | Bidirectional messaging | Both agents initiate | 1. A sends to B 2. B sends to A 3. Both respond | Both receive responses | Manual |
| E2E-004 | Concurrent requests | Multiple requests in flight | 1. A sends to B 2. A sends to C 3. Both respond | A receives both responses | Manual |

**Reference**: `tests/TESTING.md` Scenarios 1-2

### 3.2 Blocking Operations

| ID | Test | Description | Steps | Expected Result | Automated |
|----|------|-------------|-------|-----------------|-----------|
| E2E-010 | wait_for_response success | Wait returns when response arrives | 1. A sends 2. A waits 3. B responds | A unblocks with response | Partial |
| E2E-011 | wait_for_request success | Wait returns when request arrives | 1. B waits 2. A sends | B unblocks with request | Partial |
| E2E-012 | wait_for_response timeout | No response within timeout | 1. A sends 2. A waits (short timeout) 3. B doesn't respond | Timeout status returned | Yes |
| E2E-013 | wait_for_request timeout | No request within timeout | 1. B waits (short timeout) 2. No one sends | Timeout status returned | Yes |

### 3.3 Stop Hook Workflow

| ID | Test | Description | Steps | Expected Result | Automated |
|----|------|-------------|-------|-----------------|-----------|
| E2E-020 | Stop hook triggers processing | Pending requests block stop | 1. A sends to B 2. B completes task 3. B tries to stop | B sees pending, processes, then stops | Manual |
| E2E-021 | Stop hook with multiple pending | Multiple requests queued | 1. A sends 3 requests 2. B tries to stop | B processes all 3, then stops | Manual |
| E2E-022 | Stop hook after processing | No more pending | 1. B responds to all 2. B tries to stop | B stops successfully | Manual |

**Reference**: `tests/TESTING.md` Scenario 5

---

## 4. Error Handling Tests

### 4.1 Coordinator Availability

| ID | Test | Description | Steps | Expected Result | Automated |
|----|------|-------------|-------|-----------------|-----------|
| ERR-001 | Coordinator unavailable | Can't connect to coordinator | 1. Stop coordinator 2. Call list_agents | Connection error, Claude continues | Manual |
| ERR-002 | Coordinator restart | Coordinator restarts mid-session | 1. Send request 2. Restart coordinator 3. wait_for_response | Request may be lost, error returned | Manual |
| ERR-003 | Graceful degradation | CC works when coordinator down | 1. Stop coordinator 2. Use non-C3PO tools | CC functions normally | Manual |

**Reference**: `tests/TESTING.md` Scenario 6

### 4.2 Agent Availability

| ID | Test | Description | Steps | Expected Result | Automated |
|----|------|-------------|-------|-----------------|-----------|
| ERR-010 | Target agent offline | Send to disconnected agent | 1. B disconnects 2. A sends to B | Error: agent_not_found or offline | Yes |
| ERR-011 | Agent reconnect | Agent comes back online | 1. B disconnects 2. A sends (fails) 3. B reconnects 4. A sends | Second send succeeds | Manual |
| ERR-012 | Agent never responds | Waiting for unresponsive agent | 1. A sends 2. A waits 3. B ignores | Timeout after specified duration | Yes |

**Reference**: `tests/TESTING.md` Scenario 3

### 4.3 Invalid Operations

| ID | Test | Description | Steps | Expected Result | Automated |
|----|------|-------------|-------|-----------------|-----------|
| ERR-020 | Respond to unknown request | Invalid request_id | 1. respond_to_request with fake ID | Error: invalid_request | Yes |
| ERR-021 | Double response | Respond twice to same request | 1. Respond 2. Respond again | Second may succeed (idempotent) or error | Yes |
| ERR-022 | Empty message | Send with empty message | 1. send_request with message="" | Error: invalid_request | Yes |

---

## 5. Performance Tests

### 5.1 Latency

| ID | Test | Description | Target | Steps | Automated |
|----|------|-------------|--------|-------|-----------|
| PERF-001 | Request delivery | Time from send to inbox | < 500ms | 1. Timestamp before send 2. Check inbox 3. Measure | Yes |
| PERF-002 | Response roundtrip | Full request/response cycle | < 2s | 1. Send 2. Wait for response 3. Measure total | Yes |
| PERF-003 | Stop hook check | /api/pending latency | < 1s | 1. Call endpoint 2. Measure | Yes |
| PERF-004 | Agent registration | Time to register | < 500ms | 1. Connect 2. List agents 3. Measure | Yes |
| PERF-005 | list_agents response | Listing latency | < 200ms | 1. Call list_agents 2. Measure | Yes |

**Reference**: `tests/TESTING.md` Performance Expectations

### 5.2 Scalability

| ID | Test | Description | Target | Steps | Automated |
|----|------|-------------|--------|-------|-----------|
| PERF-010 | Concurrent agents | Many agents online | 10+ agents | 1. Register 10 agents 2. All list_agents 3. Verify | Manual |
| PERF-011 | Message throughput | Sustained messaging | 100 msg/min | 1. Send 100 messages over 60s 2. Verify all delivered | Manual |
| PERF-012 | Large message | Handle large payloads | 10KB message | 1. Send 10KB message 2. Verify delivery | Yes |

---

## Test Execution

### Running Automated Tests

```bash
# Unit tests (coordinator)
cd coordinator && source ../.venv/bin/activate
pytest tests/ -v

# Unit tests (hooks)
cd plugin/hooks && pytest tests/ -v

# Integration/E2E tests (requires running coordinator)
export C3PO_TEST_LIVE=1
./scripts/test-local.sh start
pytest tests/test_e2e_integration.py -v
```

### Running Manual Tests

Follow the procedures in `tests/TESTING.md` for manual test scenarios.

### Test Coverage Goals

| Category | Target Coverage |
|----------|-----------------|
| Unit Tests | 80%+ line coverage |
| Integration Tests | All critical paths |
| E2E Tests | All user workflows |
| Error Handling | All error codes exercised |
| Performance | All latency targets validated |

---

## Test Status Summary

| Category | Total | Automated | Manual | Status |
|----------|-------|-----------|--------|--------|
| Unit Tests | 33 | 33 | 0 | Implemented |
| Integration Tests | 13 | 5 | 8 | Partial |
| End-to-End Tests | 13 | 4 | 9 | Partial |
| Error Handling Tests | 9 | 5 | 4 | Partial |
| Performance Tests | 8 | 5 | 3 | Partial |
| **Total** | **76** | **52** | **24** | |

---

## Appendix: Test ID Reference

- **U-*** - Unit tests
- **I-*** - Integration tests
- **E2E-*** - End-to-end tests
- **ERR-*** - Error handling tests
- **PERF-*** - Performance tests

Suffixes:
- **AGT** - Agent management
- **MSG** - Messaging
- **REST** - REST API
- **HOOK** - Hooks
- **REDIS** - Redis integration
- **MCP** - MCP protocol
