# C3PO Improvement Implementation Plan

## Progress Checklist

### Group 1: Code Fixes
- [x] Step 1: Fix response queue race condition (lpush → rpush)
- [x] Step 2: Increase BLPOP timeout (1s → 10s)
- [x] Step 3: Remove dead code (pending_count, unused error codes, update_heartbeat)
- [x] Step 4: Improve Redis error messages
- [x] Step 5: Run tests and commit Group 1

### Group 2: Test Additions
- [x] Step 6: Create middleware test file with header extraction tests
- [x] Step 7: Add middleware collision resolution tests
- [x] Step 8: Add /api/unregister endpoint tests
- [x] Step 9: Add response put-back mechanism tests
- [x] Step 10: Run tests and commit Group 2

### Group 3: Documentation Updates
- [x] Step 11: Fix agent timeout documentation (5min → 90s)
- [x] Step 12: Document SessionEnd hook and /api/unregister
- [x] Step 13: Create API Reference - MCP tools section
- [x] Step 14: Create API Reference - REST endpoints section
- [x] Step 15: Document collision detection behavior
- [x] Step 16: Fix TROUBLESHOOTING.md and add ping tool docs
- [x] Step 17: Fix terminology inconsistencies
- [x] Step 18: Commit Group 3

### Group 4: Design Document Reconciliation
- [x] Step 19: Update detailed-design.md (mark unimplemented as Future)
- [x] Step 20: Update PROMPT.md task statuses
- [x] Step 21: Commit Group 4

Note: Group 4 was completed differently than originally planned. The original design
documents (detailed-design.md and PROMPT.md with TASK 4/5) were superseded by the
improvement plan artifacts rather than being updated in-place. The unimplemented
features (set_status, escalate_to_human, get_agent_status, /api/agents) are documented
in the "Out of Scope" section of summary.md.

### Group 5: Low Priority Improvements
- [ ] Step 22: Add input validation to REST endpoints
- [ ] Step 23: Add pagination to list_agents (or document as deferred)
- [ ] Step 24: Address session ID uniqueness
- [ ] Step 25: Run tests and commit Group 5

### Final Verification
- [ ] Step 26: Run full test suite and verify all passes
- [ ] Step 27: Final review of documentation accuracy

---

## Implementation Steps

### Group 1: Code Fixes

---

**Step 1: Fix response queue race condition**

**Objective**: Fix the bug where wrong responses get re-queued to the front instead of back, causing FIFO order violation.

**Implementation Guidance**:
1. Open `coordinator/messaging.py`
2. Locate line ~338-340 where `lpush` is used to put back a response
3. Change `lpush` to `rpush` to maintain FIFO order
4. Verify the surrounding logic makes sense with this change

**Test Requirements**: Existing tests should continue to pass.

**Integration**: Standalone fix, no dependencies.

**Demo**: Run existing message tests to verify no regression. The fix ensures that when multiple agents wait for responses, they receive them in correct order.

---

**Step 2: Increase BLPOP timeout**

**Objective**: Reduce Redis round-trips by increasing the blocking wait timeout.

**Implementation Guidance**:
1. Open `coordinator/messaging.py`
2. Locate line ~322 where BLPOP timeout is set to 1
3. Change timeout value from 1 to 10

**Test Requirements**: Existing tests should pass. Tests may run slightly slower if they depend on timeout behavior.

**Integration**: Standalone fix, no dependencies.

**Demo**: Observe reduced Redis command frequency in logs during idle wait periods.

---

**Step 3: Remove dead code**

**Objective**: Clean up unused code to reduce confusion and maintenance burden.

**Implementation Guidance**:
1. In `coordinator/messaging.py`:
   - Remove the `pending_count()` method (lines ~221-231)
2. In `coordinator/errors.py`:
   - Remove `AGENT_BUSY` error code
   - Remove `COORD_UNAVAILABLE` error code
   - Remove `MESSAGE_EXPIRED` error code
3. In `coordinator/agents.py`:
   - Check if `update_heartbeat()` is called anywhere in production code
   - If not called, remove the method
4. In `coordinator/tests/`:
   - Remove any tests for the deleted methods
   - Search for references to removed error codes and update/remove tests

**Test Requirements**: Remaining tests must pass. Some tests will be removed.

**Integration**: Ensure no imports or references to removed code remain.

**Demo**: Run `grep -r "pending_count\|AGENT_BUSY\|COORD_UNAVAILABLE\|MESSAGE_EXPIRED\|update_heartbeat" coordinator/` to verify no references remain (except in comments if documenting removal).

---

**Step 4: Improve Redis error messages**

**Objective**: Ensure Redis connection errors provide actionable information.

**Implementation Guidance**:
1. Identify Redis client initialization points in the codebase
2. Wrap connection attempts with try/except for Redis connection errors
3. Re-raise with enhanced message: "Cannot connect to Redis at {host}:{port}. Ensure Redis is running and accessible."
4. Apply consistently across `agents.py`, `messaging.py`, and `server.py`

**Test Requirements**: Existing tests pass. Consider adding a test that verifies error message format when Redis is unavailable (may require mocking).

**Integration**: Affects all modules that use Redis.

**Demo**: Stop Redis and attempt to start the coordinator; verify the error message is helpful.

---

**Step 5: Run tests and commit Group 1**

**Objective**: Verify all code fixes work correctly and commit the changes.

**Implementation Guidance**:
1. Run: `cd coordinator && pytest tests/ -v`
2. Run: `cd plugin/hooks && pytest tests/ -v`
3. If all pass, stage changed files:
   - `coordinator/messaging.py`
   - `coordinator/errors.py`
   - `coordinator/agents.py`
   - Any modified test files
4. Commit with message: "Fix code issues: race condition, BLPOP timeout, dead code removal, Redis errors"

**Test Requirements**: All tests must pass before committing.

**Integration**: Completes Group 1.

**Demo**: Show passing test output and git log with new commit.

---

### Group 2: Test Additions

---

**Step 6: Create middleware test file with header extraction tests**

**Objective**: Add test coverage for AgentIdentityMiddleware header extraction.

**Implementation Guidance**:
1. Create new file: `coordinator/tests/test_middleware.py`
2. Import necessary dependencies (pytest, the middleware class, test client)
3. Write tests for:
   - Valid `X-Agent-ID` header is correctly extracted
   - Missing `X-Agent-ID` header handling
   - Malformed header handling
   - Header value is passed to request state correctly

**Test Requirements**: New tests must pass.

**Integration**: Tests the middleware in `coordinator/server.py:35-59`.

**Demo**: Run `pytest coordinator/tests/test_middleware.py -v` showing all new tests pass.

---

**Step 7: Add middleware collision resolution tests**

**Objective**: Test the auto-registration and collision handling in middleware.

**Implementation Guidance**:
1. Add to `coordinator/tests/test_middleware.py`:
   - Test: Agent auto-registers when not found
   - Test: Collision detected when same agent ID from different session
   - Test: Auto-suffix generation on collision
   - Test: Session ID tracking behavior

**Test Requirements**: New tests must pass.

**Integration**: Builds on Step 6.

**Demo**: Run middleware tests showing collision scenarios handled correctly.

---

**Step 8: Add /api/unregister endpoint tests**

**Objective**: Add test coverage for the unregister REST endpoint.

**Implementation Guidance**:
1. Open `coordinator/tests/test_rest_api.py`
2. Add tests for `POST /api/unregister`:
   - Successful unregistration of existing agent
   - Unregistration of non-existent agent (expected behavior)
   - Missing agent_id in request body
   - Invalid request body format
3. Reference implementation at `coordinator/server.py:122-151`

**Test Requirements**: New tests must pass.

**Integration**: Tests endpoint used by SessionEnd hook.

**Demo**: Run REST API tests showing unregister endpoint behaves correctly.

---

**Step 9: Add response put-back mechanism tests**

**Objective**: Add specific tests verifying FIFO order when responses are put back.

**Implementation Guidance**:
1. Open `coordinator/tests/test_messaging.py`
2. Add tests for:
   - Response put back maintains FIFO order
   - Multiple responses put back in correct sequence
   - Concurrent waiters receive responses in order

**Test Requirements**: New tests must pass.

**Integration**: Validates the fix from Step 1.

**Demo**: Run messaging tests showing put-back behavior is correct.

---

**Step 10: Run tests and commit Group 2**

**Objective**: Verify all new tests pass and commit.

**Implementation Guidance**:
1. Run: `cd coordinator && pytest tests/ -v`
2. Verify new test file appears and all tests pass
3. Stage new/modified files:
   - `coordinator/tests/test_middleware.py` (new)
   - `coordinator/tests/test_rest_api.py`
   - `coordinator/tests/test_messaging.py`
4. Commit with message: "Add test coverage: middleware, /api/unregister, response put-back"

**Test Requirements**: All tests must pass.

**Integration**: Completes Group 2.

**Demo**: Show test count increased and all passing.

---

### Group 3: Documentation Updates

---

**Step 11: Fix agent timeout documentation**

**Objective**: Correct the agent timeout value in documentation.

**Implementation Guidance**:
1. Open `docs/USAGE.md`
2. Search for "5 minute" or "five minute" references to agent timeout
3. Change to "90 seconds"
4. Add reference: "See `AGENT_TIMEOUT_SECONDS` in `coordinator/agents.py`"

**Test Requirements**: N/A (documentation only).

**Integration**: Standalone fix.

**Demo**: Show the corrected documentation section.

---

**Step 12: Document SessionEnd hook and /api/unregister**

**Objective**: Document the graceful disconnect mechanism.

**Implementation Guidance**:
1. Open `docs/USAGE.md` or `docs/SETUP.md` (choose most appropriate location)
2. Add new section "Graceful Agent Disconnect" covering:
   - SessionEnd hook automatically unregisters agents
   - How it works: hook calls `/api/unregister` endpoint
   - Automatic cleanup via TTL as backup
   - What happens if coordinator is unavailable (fails open)
3. Document the `/api/unregister` endpoint behavior

**Test Requirements**: N/A (documentation only).

**Integration**: Explains functionality added in TASK 4.

**Demo**: Show the new documentation section explaining graceful disconnect.

---

**Step 13: Create API Reference - MCP tools section**

**Objective**: Document all MCP tools with parameters and return values.

**Implementation Guidance**:
1. Create new file: `docs/API_REFERENCE.md`
2. Add header and introduction
3. Document each MCP tool in simple markdown format:
   - `ping` - Parameters: none. Returns: pong with timestamp.
   - `list_agents` - Parameters: none. Returns: list of agents with status.
   - `register_agent` - Parameters: name (optional), capabilities (optional). Returns: registration data.
   - `send_request` - Parameters: target, message, context (optional). Returns: request data with ID.
   - `get_pending_requests` - Parameters: none. Returns: list of pending requests (consumes them).
   - `respond_to_request` - Parameters: request_id, response, status (optional). Returns: response confirmation.
   - `wait_for_response` - Parameters: request_id, timeout (optional, default 60). Returns: response or timeout.
   - `wait_for_request` - Parameters: timeout (optional, default 60). Returns: request or timeout.
4. Include example usage for each tool

**Test Requirements**: N/A (documentation only).

**Integration**: New file, referenced from README.md.

**Demo**: Show the new API reference with all MCP tools documented.

---

**Step 14: Create API Reference - REST endpoints section**

**Objective**: Document REST endpoints in OpenAPI style.

**Implementation Guidance**:
1. Continue in `docs/API_REFERENCE.md`
2. Add "REST API" section
3. Document `POST /api/unregister` in OpenAPI style:
   - Summary and description
   - Request body schema (agent_id: string, required)
   - Response schemas (200 success, 400 bad request, 404 not found)
   - Example request/response
4. Document any other REST endpoints that exist

**Test Requirements**: N/A (documentation only).

**Integration**: Builds on Step 13.

**Demo**: Show the OpenAPI-style REST documentation.

---

**Step 15: Document collision detection behavior**

**Objective**: Explain what happens when two CC instances use the same working directory.

**Implementation Guidance**:
1. Open `docs/USAGE.md`
2. Add section "Agent ID Collisions" covering:
   - How agent IDs are derived (from working directory)
   - What happens when collision is detected
   - Session ID tracking mechanism
   - Auto-suffix generation behavior
   - Guidance on avoiding collisions

**Test Requirements**: N/A (documentation only).

**Integration**: Documents TASK 5 implementation.

**Demo**: Show the new collision detection documentation.

---

**Step 16: Fix TROUBLESHOOTING.md and add ping tool docs**

**Objective**: Correct misleading troubleshooting advice and document ping tool.

**Implementation Guidance**:
1. Open `docs/TROUBLESHOOTING.md`
2. Find section about manual `redis-cli FLUSHDB`
3. Update to clarify:
   - Auto-TTL handles cleanup in most cases
   - Manual cleanup rarely needed
   - When manual cleanup might be appropriate
4. Open `docs/USAGE.md`
5. Add mention of `ping` tool for health checks

**Test Requirements**: N/A (documentation only).

**Integration**: Standalone fixes.

**Demo**: Show corrected troubleshooting guidance.

---

**Step 17: Fix terminology inconsistencies**

**Objective**: Standardize terminology across all documentation.

**Implementation Guidance**:
1. Search all docs for inconsistent terms
2. Standardize to:
   - "Stop hook" (capitalize consistently)
   - "coordinator" (prefer over "MCP server" when referring to C3PO)
   - "agent ID" (not "agent identifier")
   - "registration" (not "enrollment")
3. Apply changes across: USAGE.md, SETUP.md, TROUBLESHOOTING.md, API_REFERENCE.md, README.md

**Test Requirements**: N/A (documentation only).

**Integration**: Final polish for documentation.

**Demo**: Show consistent terminology in documentation.

---

**Step 18: Commit Group 3**

**Objective**: Commit all documentation changes.

**Implementation Guidance**:
1. Stage documentation files:
   - `docs/USAGE.md`
   - `docs/SETUP.md`
   - `docs/TROUBLESHOOTING.md`
   - `docs/API_REFERENCE.md` (new)
   - `README.md` (if modified)
2. Commit with message: "Documentation updates: fix timeout, add API reference, document SessionEnd and collisions"

**Test Requirements**: N/A (documentation only).

**Integration**: Completes Group 3.

**Demo**: Show git diff summary of documentation changes.

---

### Group 4: Design Document Reconciliation

---

**Step 19: Update detailed-design.md**

**Objective**: Align the original design document with implementation reality.

**Implementation Guidance**:
1. Open `.sop/planning/design/detailed-design.md` (the original project design, not the improvement plan)
2. Find references to unimplemented tools:
   - `set_status` - Add note: "[Future] Not yet implemented"
   - `escalate_to_human` - Add note: "[Future] Not yet implemented"
   - `get_agent_status` - Add note: "[Future] Not yet implemented"
   - `/api/agents` endpoint - Add note: "[Future] Not yet implemented"
3. Add section "Implemented Features Not In Original Design":
   - `wait_for_request` tool (blocking listener)
   - `register_agent` explicit tool
   - `peek_pending_requests` internal method

**Test Requirements**: N/A (documentation only).

**Integration**: Standalone.

**Demo**: Show the updated design document with Future annotations.

---

**Step 20: Update PROMPT.md task statuses**

**Objective**: Reflect completed work in the project PROMPT.md.

**Implementation Guidance**:
1. Open `PROMPT.md`
2. Find TASK 4 (Graceful disconnect) and mark as COMPLETE
3. Find TASK 5 (Collision handling) and mark as COMPLETE
4. Review other tasks and update status if appropriate

**Test Requirements**: N/A (documentation only).

**Integration**: Standalone.

**Demo**: Show updated task statuses in PROMPT.md.

---

**Step 21: Commit Group 4**

**Objective**: Commit design document updates.

**Implementation Guidance**:
1. Stage files:
   - `.sop/planning/design/detailed-design.md`
   - `PROMPT.md`
2. Commit with message: "Update design docs: mark unimplemented as Future, update task statuses"

**Test Requirements**: N/A.

**Integration**: Completes Group 4.

**Demo**: Show commit with design document updates.

---

### Group 5: Low Priority Improvements

---

**Step 22: Add input validation to REST endpoints**

**Objective**: Add consistent input size validation to REST endpoints.

**Implementation Guidance**:
1. Open `coordinator/server.py`
2. Identify REST endpoints that accept request bodies
3. Add validation for request body size (match 50KB limit used in MCP tools)
4. Return appropriate error response if validation fails

**Test Requirements**: Add tests for oversized request handling.

**Integration**: Affects REST API behavior.

**Demo**: Show that oversized requests are rejected with clear error.

---

**Step 23: Add pagination to list_agents**

**Objective**: Support pagination for large agent lists.

**Implementation Guidance**:
1. Open `coordinator/agents.py`
2. Modify `list_agents()` to accept optional `limit` and `offset` parameters
3. Update `coordinator/server.py` MCP tool to expose these parameters
4. Default behavior (no params) returns all agents for backward compatibility

**Alternative**: If agent count is expected to remain small, document this as "deferred" and skip implementation.

**Test Requirements**: Add tests for pagination if implemented.

**Integration**: Updates MCP tool interface.

**Demo**: Show paginated results with limit/offset parameters.

---

**Step 24: Address session ID uniqueness**

**Objective**: Handle or document session ID behavior.

**Implementation Guidance**:
Option A (Enforce uniqueness):
1. Open `coordinator/agents.py`
2. Add validation to reject duplicate session IDs
3. Return clear error if session ID already in use

Option B (Document current behavior):
1. Document that session IDs are informational only
2. Explain they are used for collision detection, not enforced unique

**Test Requirements**: Add tests if implementing enforcement.

**Integration**: Affects registration behavior.

**Demo**: Show behavior when duplicate session ID is used.

---

**Step 25: Run tests and commit Group 5**

**Objective**: Verify low priority improvements and commit.

**Implementation Guidance**:
1. Run: `cd coordinator && pytest tests/ -v`
2. Run: `cd plugin/hooks && pytest tests/ -v`
3. Stage modified files
4. Commit with message: "Low priority improvements: input validation, pagination, session ID handling"

**Test Requirements**: All tests must pass.

**Integration**: Completes Group 5.

**Demo**: Show passing tests and final commit.

---

### Final Verification

---

**Step 26: Run full test suite**

**Objective**: Verify all changes work together.

**Implementation Guidance**:
1. Run: `cd coordinator && pytest tests/ -v`
2. Run: `cd plugin/hooks && pytest tests/ -v`
3. Verify test count matches or exceeds expectations
4. Verify no warnings or deprecation notices

**Test Requirements**: 100% pass rate.

**Integration**: Validates all groups together.

**Demo**: Show complete test output with all tests passing.

---

**Step 27: Final documentation review**

**Objective**: Ensure all documentation is accurate and consistent.

**Implementation Guidance**:
1. Read through each documentation file
2. Verify terminology is consistent
3. Check that code references (file paths, line numbers) are accurate
4. Verify API Reference matches actual implementation
5. Note any remaining issues for follow-up

**Test Requirements**: N/A.

**Integration**: Final quality check.

**Demo**: Confirm documentation accurately reflects implementation.

---

## Summary

This implementation plan contains 27 steps organized into 5 groups plus final verification:

| Group | Steps | Focus |
|-------|-------|-------|
| 1 | 1-5 | Code fixes (race condition, timeout, dead code, errors) |
| 2 | 6-10 | Test additions (middleware, API, messaging) |
| 3 | 11-18 | Documentation updates |
| 4 | 19-21 | Design document reconciliation |
| 5 | 22-25 | Low priority improvements |
| Final | 26-27 | Full verification |

Each group results in a working, tested state with a commit checkpoint.
