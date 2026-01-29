# C3PO Improvement Plan: Detailed Design

## Overview

This document describes the improvements to be made to the C3PO multi-agent coordination system based on the comprehensive project review. The work addresses documentation gaps, code issues, test coverage, and design document inconsistencies.

**Goal**: Bring C3PO from "70-80% complete with rough edges" to production-ready with accurate documentation and comprehensive test coverage.

---

## Detailed Requirements

### Scope
All identified issues will be addressed (High + Medium + Low priority).

### Implementation Target
Claude Code will implement via PROMPT.md with autonomous execution.

### Work Organization
Grouped sequential - one PROMPT.md with clearly identified independent groups, enabling future parallelization.

### Verification Strategy
- Run tests after each group completes
- Create a commit after each group passes tests
- Fail fast on Redis connection errors with clear messages

### Design Decisions
- Unimplemented features in design docs: Mark as "Future" (do not remove)
- Dead code: Remove entirely
- API Reference format: OpenAPI for REST endpoints, simple markdown for MCP tools
- BLPOP timeout: Change from 1s to 10s

---

## Work Groups

The improvements are organized into 5 independent groups. Groups can be executed in any order, though the suggested order minimizes potential conflicts.

### Group 1: Code Fixes

Critical and medium priority code changes.

#### 1.1 Fix Response Queue Race Condition (Critical)
- **File**: `coordinator/messaging.py:338-340`
- **Issue**: Wrong responses get re-queued with `lpush` (front) instead of `rpush` (back)
- **Fix**: Change `lpush` to `rpush` to maintain FIFO order
- **Verification**: Existing tests should pass; consider adding a specific test for this behavior

#### 1.2 Increase BLPOP Timeout (Medium)
- **File**: `coordinator/messaging.py:322`
- **Issue**: 1-second timeout causes excessive Redis round-trips
- **Fix**: Change timeout from 1 to 10 seconds
- **Verification**: Existing tests should pass

#### 1.3 Remove Dead Code (Medium)
- **Files**:
  - `coordinator/messaging.py:221-231` - Remove `pending_count()` method
  - `coordinator/errors.py` - Remove unused error codes: `AGENT_BUSY`, `COORD_UNAVAILABLE`, `MESSAGE_EXPIRED`
  - `coordinator/agents.py` - Remove `update_heartbeat()` method if unused
  - `coordinator/tests/` - Remove tests for removed methods
- **Verification**: All remaining tests pass

#### 1.4 Improve Redis Error Messages (Medium)
- **Files**: Throughout `coordinator/` modules
- **Issue**: Redis exceptions propagate without user-friendly context
- **Fix**: Ensure Redis connection errors have clear, actionable messages
- **Approach**: Fail fast but with helpful error text (e.g., "Cannot connect to Redis at {host}:{port}. Ensure Redis is running.")
- **Verification**: Manual verification of error messages

---

### Group 2: Test Additions

New tests for untested critical functionality.

#### 2.1 Add Middleware Tests (Critical)
- **New File**: `coordinator/tests/test_middleware.py`
- **Coverage Required**:
  - Header extraction (`X-Agent-ID` parsing)
  - Auto-registration when agent not found
  - Collision resolution (session_id tracking, auto-suffix generation)
  - Missing header handling
- **Reference**: `coordinator/server.py:35-59` (AgentIdentityMiddleware)

#### 2.2 Add /api/unregister Endpoint Tests (Critical)
- **File**: `coordinator/tests/test_rest_api.py` (add to existing)
- **Coverage Required**:
  - Successful unregistration
  - Unregistration of non-existent agent
  - Request validation
  - Integration with SessionEnd hook behavior
- **Reference**: `coordinator/server.py:122-151`

#### 2.3 Add Response Put-Back Mechanism Tests (Medium)
- **File**: `coordinator/tests/test_messaging.py` (add to existing)
- **Coverage Required**:
  - Verify FIFO order maintained when response is put back
  - Multiple agents waiting for responses scenario
- **Note**: This validates the Group 1.1 fix

---

### Group 3: Documentation Updates

Fix inaccuracies and add missing documentation.

#### 3.1 Fix Agent Timeout Documentation (Critical)
- **File**: `docs/USAGE.md`
- **Issue**: Documents "5 minutes" but code uses 90 seconds
- **Fix**: Update to say 90 seconds, reference `AGENT_TIMEOUT_SECONDS` constant

#### 3.2 Document SessionEnd Hook (Critical)
- **File**: `docs/USAGE.md` (add section) or `docs/SETUP.md`
- **Content**:
  - Explain that agents gracefully unregister on session end
  - Reference the hook mechanism
  - Document `/api/unregister` endpoint behavior
  - Explain automatic cleanup via TTL

#### 3.3 Create API Reference (Critical)
- **New File**: `docs/API_REFERENCE.md`
- **MCP Tools Section** (simple markdown format):
  - `ping` - Health check
  - `list_agents` - List registered agents with status
  - `register_agent` - Explicit registration with capabilities
  - `send_request` - Send request to another agent
  - `get_pending_requests` - Get queued requests (consumes them)
  - `respond_to_request` - Respond to a received request
  - `wait_for_response` - Blocking wait for response
  - `wait_for_request` - Blocking wait for incoming request
- **REST Endpoints Section** (OpenAPI-style):
  - `POST /api/unregister` - Unregister an agent
  - Include: parameters, request/response schemas, error codes

#### 3.4 Document Collision Detection (Medium)
- **File**: `docs/USAGE.md` (add section)
- **Content**:
  - Explain what happens when two CC instances use same working directory
  - Describe session_id tracking and auto-suffix mechanism
  - Provide guidance on avoiding collisions

#### 3.5 Fix TROUBLESHOOTING.md Inconsistency (Medium)
- **File**: `docs/TROUBLESHOOTING.md`
- **Issue**: Suggests manual `redis-cli FLUSHDB` but auto-TTL handles cleanup
- **Fix**: Clarify that manual cleanup is rarely needed; explain TTL-based expiration

#### 3.6 Document ping Tool (Medium)
- **File**: `docs/USAGE.md`
- **Issue**: Tool exists but not mentioned
- **Fix**: Add brief description of ping tool for health checks

#### 3.7 Fix Terminology Inconsistencies (Low)
- **Files**: All documentation files
- **Standardize**:
  - "stop hook" → "Stop hook" (consistent capitalization)
  - Choose either "MCP server" or "coordinator" and use consistently
  - "agent ID" vs "agent identifier" → pick one
  - "registration" vs "enrollment" → pick one

---

### Group 4: Design Document Reconciliation

Align design documents with implementation reality.

#### 4.1 Update detailed-design.md (Medium)
- **File**: `.sop/planning/design/detailed-design.md` (the original one, not this file)
- **Changes**:
  - Mark `set_status` tool as "Future"
  - Mark `escalate_to_human` tool as "Future"
  - Mark `get_agent_status` tool as "Future"
  - Mark `/api/agents` REST endpoint as "Future"
  - Add section documenting implemented features not in original design:
    - `wait_for_request` tool
    - `register_agent` explicit tool
    - `peek_pending_requests` internal method

#### 4.2 Update PROMPT.md Task Statuses (Medium)
- **File**: `PROMPT.md`
- **Changes**:
  - TASK 4 (Graceful disconnect): Mark as COMPLETE
  - TASK 5 (Collision handling): Mark as COMPLETE
  - Update any other tasks that have been completed

---

### Group 5: Low Priority Improvements

Nice-to-have improvements.

#### 5.1 Add Input Validation to REST Endpoints (Low)
- **File**: `coordinator/server.py`
- **Issue**: 50KB validation exists at MCP tool level but not REST level
- **Fix**: Add consistent input size validation to REST endpoints

#### 5.2 Add Pagination to list_agents (Low)
- **File**: `coordinator/agents.py` and `coordinator/server.py`
- **Issue**: Large agent lists returned without pagination
- **Fix**: Add optional `limit` and `offset` parameters
- **Note**: May be deferred if agent count is expected to remain small

#### 5.3 Session ID Uniqueness (Low)
- **File**: `coordinator/agents.py`
- **Issue**: Session ID uniqueness not enforced
- **Fix**: Add validation or documentation about expected behavior
- **Note**: May be documentation-only fix

---

## Testing Strategy

### Per-Group Testing
After each group, run:
```bash
cd coordinator && pytest tests/ -v
cd plugin/hooks && pytest tests/ -v
```

### New Test Files
- `coordinator/tests/test_middleware.py` - New file for middleware tests

### Test Updates
- `coordinator/tests/test_rest_api.py` - Add /api/unregister tests
- `coordinator/tests/test_messaging.py` - Add response put-back tests
- Remove tests for deleted dead code

### Final Verification
After all groups complete:
1. Full test suite passes
2. Two-agent manual test per PROMPT.md procedure
3. Documentation review against implementation

---

## Error Handling

### Redis Connection Errors
- Fail fast with clear error message
- Include connection details in error (host, port)
- Suggest remediation (e.g., "Ensure Redis is running")

### Validation Errors
- Return structured error responses with error codes
- Include field-level details where applicable

---

## Appendices

### A. Files Modified by Group

| Group | Files |
|-------|-------|
| 1 - Code Fixes | messaging.py, errors.py, agents.py, related tests |
| 2 - Test Additions | test_middleware.py (new), test_rest_api.py, test_messaging.py |
| 3 - Documentation | USAGE.md, SETUP.md, TROUBLESHOOTING.md, API_REFERENCE.md (new) |
| 4 - Design Docs | detailed-design.md, PROMPT.md |
| 5 - Low Priority | server.py, agents.py |

### B. Risk Assessment

| Risk | Mitigation |
|------|------------|
| Breaking existing functionality | Run tests after each group; commit only on pass |
| Documentation drift | Create API_REFERENCE.md from code inspection |
| Test gaps | New tests written to cover middleware and API |

### C. Out of Scope

- Implementing `set_status`, `escalate_to_human`, `get_agent_status` tools (marked Future)
- Adding authentication (noted as "Should" priority in review)
- Plugin-based enrollment (TASK 3 from original PROMPT.md)
- Clean room validation (TASK 1 from original PROMPT.md)
