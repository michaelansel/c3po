# Requirements Clarification

This document captures the Q&A process for refining the C3PO project review into actionable improvements.

---

## Q1: What is the scope of this improvement effort?

The review identifies issues across multiple priority levels:
- **High Priority (6 items)**: Race condition fix, timeout docs, middleware tests, /api/unregister tests, SessionEnd hook docs, API reference
- **Medium Priority (6 items)**: Task status updates, dead code removal, collision docs, Redis error handling, BLPOP timeout, design doc reconciliation
- **Low Priority (4 items)**: Terminology fixes, pagination, session ID enforcement, REST validation

Do you want to address:
- All issues (High + Medium + Low)?
- Only High and Medium priority?
- Only High priority items?
- A custom selection?

**Answer:** All issues (High + Medium + Low priority) will be addressed.

---

## Q2: What is the target for this work?

Who will be implementing these improvements?

- **You (the human)** - I should create detailed step-by-step instructions you can follow
- **Claude Code (me/another agent)** - I should create a PROMPT.md suitable for autonomous implementation
- **A mix** - Some tasks for you, some for Claude Code
- **Other** - Please specify

**Answer:** Claude Code will implement the improvements. The deliverable should be a PROMPT.md suitable for autonomous implementation.

---

## Q3: Should the work be done sequentially or in parallel?

The improvements fall into distinct categories that could potentially be worked on independently:

- **Code fixes** (race condition, BLPOP timeout, dead code removal)
- **Test additions** (middleware tests, /api/unregister tests)
- **Documentation updates** (timeout fix, SessionEnd docs, API reference, collision docs, terminology)
- **Design reconciliation** (update detailed-design.md, PROMPT.md task statuses)

Options:
- **Sequential** - One PROMPT.md with all tasks in order (simpler, but slower)
- **Parallel** - Multiple PROMPT.md files that can be run by separate CC instances simultaneously (faster, but requires coordination)
- **Grouped sequential** - One PROMPT.md but organized so independent groups can be identified

**Answer:** Grouped sequential - One PROMPT.md organized with independent groups clearly identified, allowing future parallelization if desired.

---

## Q4: What are the verification requirements?

How should Claude Code verify its work as it progresses?

- **Run tests after each code change** - Ensure no regressions (slower but safer)
- **Run tests only at the end** - Faster but riskier
- **Run tests after each group** - Balance of speed and safety
- **No automated verification** - Trust the implementation, verify manually later

Additionally, should the implementation:
- Create a commit after each group?
- Create a single commit at the end?
- Not commit (leave changes staged/unstaged for your review)?

**Answer:** Run tests after each group and create a commit after each group completes successfully.

---

## Q5: How should design document discrepancies be handled?

The detailed-design.md references features that were never implemented:
- `set_status` tool
- `escalate_to_human` tool
- `get_agent_status` tool
- `/api/agents` REST endpoint

Options:
- **Remove from design** - Delete references to unimplemented features (cleaner, acknowledges current scope)
- **Mark as "Future"** - Keep them but clearly mark as planned/not-yet-implemented (preserves intent)
- **Implement them** - Actually build these features (significantly expands scope)

**Answer:** Mark as "Future" - Keep references but clearly indicate they are planned/not-yet-implemented.

---

## Q6: How should dead code be handled?

The review identified unused code:
- `MessageManager.pending_count()` method - never called
- `ErrorCodes.AGENT_BUSY` - defined but never used
- `ErrorCodes.COORD_UNAVAILABLE` - defined but never used
- `ErrorCodes.MESSAGE_EXPIRED` - defined but never used
- `agent_manager.update_heartbeat()` - has tests but never called in production code

Options:
- **Remove all dead code** - Clean up the codebase (simpler, but loses potential future utility)
- **Keep error codes, remove methods** - Error codes are cheap to keep; unused methods add confusion
- **Keep all** - Document as "reserved for future use" (preserves intent but adds clutter)

**Answer:** Remove all dead code - clean up the codebase entirely.

---

## Q7: What format should the new API Reference use?

The review calls for a new `docs/API_REFERENCE.md` documenting all MCP tools. What format do you prefer?

Options:
- **Simple markdown** - Tool name, description, parameters, returns, example (straightforward)
- **OpenAPI-style** - More formal with types, required/optional markers, error responses (thorough but verbose)
- **Match existing docs style** - Follow whatever pattern USAGE.md and other docs use

**Answer:** Use OpenAPI-style for REST endpoints; use simple markdown for MCP tools.

---

## Q8: How should Redis connection errors be handled?

The review notes there's no Redis connection failure handling. When Redis is unavailable, what should happen?

Options:
- **Return structured errors** - Catch Redis exceptions, return a proper error response with error code (e.g., `COORD_UNAVAILABLE`)
- **Fail fast with clear message** - Let the exception propagate but ensure the error message is user-friendly
- **Graceful degradation** - Return empty results or cached data where possible, with a warning
- **Other** - Please specify

**Answer:** Fail fast with clear message - Let exceptions propagate but ensure error messages are user-friendly.

---

## Q9: What BLPOP timeout value should be used?

The review notes that BLPOP currently uses a 1-second timeout and suggests 10 seconds for efficiency. The tradeoff:
- **Shorter timeout (1s)**: More responsive to cancellation/shutdown, but more Redis round-trips
- **Longer timeout (10s)**: Fewer Redis round-trips, but slower response to shutdown signals

What timeout value do you prefer?
- Keep at 1 second
- Change to 10 seconds (as suggested)
- Other value (please specify)

**Answer:** Change to 10 seconds as suggested.

---

## Q10: Are there any additional constraints or requirements?

We've established:
1. **Scope**: All issues (High + Medium + Low)
2. **Target**: Claude Code implementation via PROMPT.md
3. **Structure**: Grouped sequential with independent groups identified
4. **Verification**: Run tests and commit after each group
5. **Design docs**: Mark unimplemented features as "Future"
6. **Dead code**: Remove all
7. **API Reference**: OpenAPI for REST, simple markdown for MCP tools
8. **Redis errors**: Fail fast with clear messages
9. **BLPOP timeout**: 10 seconds

Is there anything else you'd like to specify before we proceed? For example:
- Specific commit message format?
- Branch name requirements?
- Any files that should NOT be modified?
- Anything else?

**Answer:** No additional constraints or requirements.
