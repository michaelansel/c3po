# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

C3PO is a multi-agent coordination framework that enables multiple Claude Code instances to communicate and collaborate through a central coordinator service. Built on FastMCP (MCP protocol) with a Redis message queue backend.

## Commands

### Running the Coordinator
```bash
# Start coordinator + Redis locally
./scripts/test-local.sh start

# Check status / view logs / stop
./scripts/test-local.sh status
./scripts/test-local.sh logs
./scripts/test-local.sh stop
```

### Tests
```bash
# Unit tests (coordinator)
python3 -m pytest coordinator/tests/ -v

# Single test file
python3 -m pytest coordinator/tests/test_agents.py -v

# Single test
python3 -m pytest coordinator/tests/test_agents.py::TestAgentManager::test_collision_with_different_session_gets_suffix -v

# Plugin hook tests
cd plugin/hooks && pytest tests/ -v

# E2E tests (requires live coordinator)
export C3PO_TEST_LIVE=1
./scripts/test-local.sh start
pytest tests/test_e2e_integration.py -v

# Acceptance tests (containerized)
bash tests/acceptance/run-acceptance.sh
```

### Deployment
```bash
./scripts/deploy.sh          # Full deployment
./scripts/enroll.sh          # Enroll a Claude Code instance
```

## Architecture

### Components

- **coordinator/** — FastMCP server with Redis backend (port 8420)
  - `server.py` — MCP tools, REST endpoints, middleware, entry point (`main()`)
  - `agents.py` — `AgentManager`: registration, collision detection, heartbeat tracking
  - `messaging.py` — `MessageManager`: request/response queues, rate limiting, notifications
  - `errors.py` — Structured error codes

- **plugin/** — Claude Code plugin (hooks + skills)
  - `hooks/register_agent.py` — SessionStart: registers agent via REST
  - `hooks/check_inbox.py` — Stop: blocks stop if pending requests exist
  - `hooks/unregister_agent.py` — SessionEnd: unregisters agent
  - `hooks/ensure_agent_id.py` — PreToolUse: ensures agent_id for MCP calls
  - `setup.py` — Interactive plugin installer

### Key Design Patterns

**Agent ID format**: `{machine}/{project}` (e.g., `macbook/myproject`). Bare machine names without a slash are rejected.

**Collision detection**: When two sessions claim the same agent ID, the second gets a suffix (`-2`, `-3`, etc.). Same session reconnecting just updates the heartbeat.

**Dual interface**: MCP tools for agent-to-agent communication within Claude Code sessions; REST API (`/api/register`, `/api/pending`, `/api/unregister`, `/api/health`) for hook scripts that run outside MCP context.

**Adding MCP tools**: When adding a new tool to `coordinator/server.py`, also update `plugin/hooks/hooks.json` (PreToolUse matcher list) and, if the tool uses `agent_id`, `plugin/hooks/ensure_agent_id.py` (TOOLS_NEEDING_AGENT_ID). The matcher must explicitly list all tool names because prefix patterns don't work in plugin hooks.

**Version bumping**: When committing a version bump, update the version in **both** `plugin/.claude-plugin/plugin.json` and `.claude-plugin/marketplace.json` in addition to the commit message tag. These manifests must stay in sync.

**Message flow**: Requests go to `c3po:inbox:{agent}` Redis lists. Notifications (separate from messages) go to `c3po:notify:{agent}` to wake blocked `wait_for_request` calls without consuming messages. This separation prevents message loss.

**Rate limiting**: Sliding window (10 requests per 60 seconds per agent) using Redis sorted sets.

**Agent liveness**: Heartbeat updated on every MCP tool call. Agents go offline after 15 minutes of inactivity. Messages expire after 24 hours.

### Redis Key Structure

- `c3po:agents` — Hash of all registered agents
- `c3po:inbox:{agent_id}` — Request queue (FIFO list)
- `c3po:notify:{agent_id}` — Notification signals for wait_for_request
- `c3po:responses:{agent_id}` — Response queue
- `c3po:rate:{agent_id}` — Rate limit tracking (sorted set)

### Environment Variables

- `REDIS_URL` — Redis connection (default: `redis://localhost:6379`)
- `C3PO_PORT` — Server port (default: `8420`)
- `C3PO_HOST` — Server bind address (default: `0.0.0.0`)
- `C3PO_AGENT_ID` / `C3PO_PROJECT_NAME` / `C3PO_SESSION_ID` — Plugin overrides

## Testing

### Philosophy

Tests are organized in layers from fast/isolated to slow/integrated. Each layer builds confidence that the layer below didn't miss something:

1. **Unit tests** (`coordinator/tests/`, `plugin/hooks/tests/`) — Fast, no network. Use `fakeredis` for in-memory Redis and mock HTTP servers for hook tests. Every module has its own test file. These run in seconds and should always pass before committing.

2. **E2E integration tests** (`tests/test_e2e_integration.py`) — Real MCP client sessions against a live coordinator. Gated behind `C3PO_TEST_LIVE=1`. Validates that MCP transport, headers, and tool dispatch work end-to-end.

3. **Acceptance tests** (`tests/acceptance/`) — Fully containerized: builds the coordinator image, starts Redis, coordinator, and agent containers, then runs multi-phase scenarios. This is the closest thing to production. Run with `bash tests/acceptance/run-acceptance.sh`.

4. **Manual scenarios** (`tests/TESTING.md`) — Human-executed tests for behaviors that require real Claude Code sessions (stop hooks, human interrupt, task delegation).

### Guidelines

- Unit tests use `fakeredis` — no running Redis needed. Each test gets a fresh instance via fixtures.
- Async tests use `pytest-asyncio`. REST endpoint tests use `httpx` `AsyncClient` with ASGI transport.
- Plugin hook tests run the hook scripts as subprocesses against a mock HTTP server, matching how Claude Code invokes them.
- Acceptance tests support both `docker` and `finch` runtimes.
- Test documentation lives in `tests/`: `TEST_PLAN.md` (test matrix and IDs), `TESTING.md` (manual scenarios), `acceptance/ACCEPTANCE_SPEC.md` (acceptance phases).

### What to run when

- **Before committing**: Unit tests (`coordinator/tests/` and `plugin/hooks/tests/`)
- **Before deploying**: Acceptance tests (`tests/acceptance/run-acceptance.sh`)
- **After deploying**: Smoke-check with `scripts/test_e2e.sh` or E2E integration tests

## Container Runtime

Use `finch` instead of `docker` on this machine.
