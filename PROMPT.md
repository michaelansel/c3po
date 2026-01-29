# C3PO Improvement Implementation

## Objective

Address all issues identified in the project review to bring C3PO from "70-80% complete with rough edges" to production-ready with accurate documentation and comprehensive test coverage.

## Key Requirements

- Fix critical code issues (race condition, documentation errors)
- Add missing test coverage (middleware, /api/unregister endpoint)
- Create comprehensive API reference documentation
- Reconcile design documents with implementation reality
- Remove dead code and improve consistency

## Work Groups

Execute groups in order. Run tests and commit after each group.

### Group 1: Code Fixes
- Fix response queue race condition (`messaging.py:340` - change `lpush` to `rpush`)
- Increase BLPOP timeout to 10 seconds (`messaging.py:322`)
- Remove dead code: `pending_count()`, unused error codes, `update_heartbeat()`
- Improve Redis connection error messages

### Group 2: Test Additions
- Create `coordinator/tests/test_middleware.py` for AgentIdentityMiddleware
- Add `/api/unregister` endpoint tests to `test_rest_api.py`
- Add response put-back mechanism tests to `test_messaging.py`

### Group 3: Documentation Updates
- Fix agent timeout in USAGE.md (5min → 90s)
- Document SessionEnd hook and graceful disconnect
- Create `docs/API_REFERENCE.md` (markdown for MCP, OpenAPI for REST)
- Document collision detection behavior
- Fix TROUBLESHOOTING.md (auto-TTL vs manual cleanup)
- Document ping tool
- Fix terminology inconsistencies

### Group 4: Design Document Reconciliation
- Mark unimplemented tools as "Future" in detailed-design.md
- Update original PROMPT.md task statuses (TASK 4, 5 → COMPLETE)

### Group 5: Low Priority
- Add input validation to REST endpoints
- Add pagination to list_agents (or document as deferred)
- Address session ID uniqueness

## Acceptance Criteria

1. All existing tests pass after each group
2. New tests provide coverage for middleware and /api/unregister
3. Documentation accurately reflects implementation (90s timeout, not 5min)
4. API Reference documents all MCP tools and REST endpoints
5. Design documents marked with "Future" for unimplemented features
6. No dead code remains in codebase
7. Terminology is consistent across all documentation

## Verification

After each group:
```bash
cd coordinator && pytest tests/ -v
cd plugin/hooks && pytest tests/ -v
```

Final verification:
- Full test suite passes
- Two-agent test per procedure below

## Two-Agent Test Procedure

1. Start Agent B (listener):
   ```bash
   echo "Use mcp__c3po__wait_for_request with timeout 120. When you receive a request, respond using mcp__c3po__respond_to_request." | \
     claude -p --allowedTools "mcp__c3po__wait_for_request,mcp__c3po__respond_to_request"
   ```

2. Start Agent A (sender):
   ```bash
   echo "Use mcp__c3po__send_request to ask agent-b 'What is 2+2?', then use mcp__c3po__wait_for_response with timeout 60." | \
     claude -p --allowedTools "mcp__c3po__send_request,mcp__c3po__wait_for_response"
   ```

3. Verify Agent A receives response from Agent B.

## Reference Documents

- **Detailed Design**: `.sop/planning/design/detailed-design.md`
- **Implementation Plan**: `.sop/planning/implementation/plan.md`
- **Requirements**: `.sop/planning/idea-honing.md`
- **Original Review**: `.sop/planning/rough-idea.md`
