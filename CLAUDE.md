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
cd hooks && pytest tests/ -v

# E2E tests (requires live coordinator)
export C3PO_TEST_LIVE=1
./scripts/test-local.sh start
pytest tests/test_e2e_integration.py -v

# Acceptance tests (containerized)
bash tests/acceptance/run-acceptance.sh
```

### Deployment
```bash
bash scripts/deploy.sh       # Deploy to pubpop3 (builds, configures, prints nginx sudo commands)
# Enroll via: python3 setup.py --enroll https://mcp.qerk.be '<admin_token>'
```

## Architecture

### Components

- **coordinator/** — FastMCP server with Redis backend (port 8420)
  - `server.py` — MCP tools, REST endpoints, middleware, entry point (`main()`)
  - `agents.py` — `AgentManager`: registration, collision detection, heartbeat tracking
  - `messaging.py` — `MessageManager`: message/reply queues, notifications
  - `errors.py` — Structured error codes
  - `auth.py` — `AuthManager`: validates API keys, proxy tokens, and admin keys; manages per-agent API key lifecycle
  - `audit.py` — `AuditLogger`: structured JSON audit logging to Python logger + Redis
  - `rate_limit.py` — `RateLimiter`: per-operation, per-identity sliding window rate limiting

- **hooks/** — Claude Code plugin hooks
  - `register_agent.py` — SessionStart: registers agent via REST
  - `check_inbox.py` — Stop: blocks stop if pending messages exist
  - `unregister_agent.py` — SessionEnd: unregisters agent
  - `ensure_agent_id.py` — PreToolUse: ensures agent_id for MCP calls
  - `c3po_common.py` — Shared utilities: credentials file I/O, auth headers, agent ID file management
- **setup.py** — Interactive plugin installer with enrollment

### Key Design Patterns

**Agent ID format**: `{machine}/{project}` (e.g., `macbook/myproject`). Bare machine names without a slash are rejected.

**Collision detection**: When two sessions claim the same agent ID, the second gets a suffix (`-2`, `-3`, etc.). Same session reconnecting just updates the heartbeat.

**Dual interface**: MCP tools for agent-to-agent communication within Claude Code sessions; REST API for hook scripts that run outside MCP context. URLs are split by auth type:

| Path | Auth | Used by |
|------|------|---------|
| `/agent/mcp` | API key bearer token | Claude Code (headless MCP) |
| `/oauth/mcp` | OAuth (mcp-auth-proxy) | Claude Desktop, Claude.ai |
| `/agent/api/register` | API key bearer token | Hooks (SessionStart) |
| `/agent/api/pending` | API key bearer token | Hooks (Stop) |
| `/agent/api/unregister` | API key bearer token | Hooks (SessionEnd) |
| `/admin/api/keys` | Admin key | Enrollment (setup.py) |
| `/admin/api/audit` | Admin key | Admin tools |
| `/api/health` | None (public) | Health checks |

**Authentication**: Three auth mechanisms, determined by URL path prefix:
- **API key** (`/agent/*`): `Authorization: Bearer <server_secret>.<api_key>`. nginx validates the server_secret prefix; coordinator validates the api_key via SHA-256 lookup + bcrypt verification. Per-agent API keys stored in Redis, scoped by agent_pattern (fnmatch glob). Used by Claude Code instances.
- **OAuth proxy** (`/oauth/*`): `Authorization: Bearer <proxy_token>`. Injected by mcp-auth-proxy after OAuth flow. Used by Claude Desktop and Claude.ai.
- **Admin key** (`/admin/*`): `Authorization: Bearer <server_secret>.<admin_key>`. nginx validates the server_secret prefix; coordinator validates the admin_key portion. Legacy format `Bearer <admin_key>` also accepted.
- **Dev mode**: When no auth env vars are set (`C3PO_SERVER_SECRET`, `C3PO_ADMIN_KEY`, `C3PO_PROXY_BEARER_TOKEN`), all requests pass without auth.

**Credentials**: Plugin hooks read `~/.claude/c3po-credentials.json` (0o600 perms) for auth. Contains `coordinator_url`, `api_token` (composite `server_secret.api_key`), `key_id`, `agent_pattern`. Legacy format with separate `server_secret` and `api_key` fields is also supported. Created by `setup.py --enroll`.

**MCP tools**: `send_message` (send to another agent), `reply` (respond to a message), `get_messages` (consume pending messages/replies), `wait_for_message` (block until message arrives), `ping`, `list_agents`, `register_agent`, `set_description`.

**Adding MCP tools**: When adding a new tool to `coordinator/server.py`, also update `hooks/hooks.json` (PreToolUse matcher list) and, if the tool uses `agent_id`, `hooks/ensure_agent_id.py` (TOOLS_NEEDING_AGENT_ID). The matcher must explicitly list all tool names because prefix patterns don't work in plugin hooks. New modules also need to be added to the `Dockerfile` COPY commands.

**Version bumping**: When committing a version bump, update `.claude-plugin/plugin.json` in this repo. The marketplace (`michaelansel/c3po`) does not need separate updates.

**Message flow**: Messages go to `c3po:inbox:{agent}` Redis lists. Notifications (separate from messages) go to `c3po:notify:{agent}` to wake blocked `wait_for_message` calls without consuming messages. This separation prevents message loss. Messages have type `"message"`, replies have type `"reply"`. Each message gets a `message_id` used for replies.

**Rate limiting**: Per-operation sliding window rate limits using Redis sorted sets. Different limits for different operations (e.g., `send_message`: 10/60s, `list_agents`: 30/60s, `rest_register`: 5/60s). Per-agent for MCP tools, per-IP for REST endpoints.

**Agent liveness**: Heartbeat updated on every MCP tool call. Agents go offline after 15 minutes of inactivity. Messages expire after 24 hours.

### Redis Key Structure

- `c3po:agents` — Hash of all registered agents
- `c3po:inbox:{agent_id}` — Message queue (FIFO list)
- `c3po:notify:{agent_id}` — Notification signals for wait_for_message
- `c3po:replies:{agent_id}` — Reply queue
- `c3po:rate:{operation}:{identity}` — Rate limit tracking (sorted set)
- `c3po:audit` — List of recent audit entries (JSON, newest first)
- `c3po:api_keys` — Hash: `sha256(api_key)` → JSON key metadata (includes `bcrypt_hash` for verification)
- `c3po:key_ids` — Hash: `key_id` → `sha256(api_key)` (reverse lookup)

### Environment Variables

- `REDIS_URL` — Redis connection (default: `redis://localhost:6379`)
- `C3PO_PORT` — Server port (default: `8420`)
- `C3PO_HOST` — Server bind address (default: `0.0.0.0`)
- `C3PO_SERVER_SECRET` — Server-side secret for API key validation (first half of `Bearer <secret>.<key>`)
- `C3PO_ADMIN_KEY` — Admin key for `/admin/*` endpoints
- `C3PO_PROXY_BEARER_TOKEN` — Shared token for OAuth proxy (`/oauth/*` paths)
- `C3PO_BEHIND_PROXY` — Set to `true` to trust X-Forwarded-For/X-Real-IP headers
- `C3PO_CA_CERT` — Path to custom CA certificate for HTTPS (hooks)
- `C3PO_MACHINE_NAME` / `C3PO_PROJECT_NAME` / `C3PO_SESSION_ID` — Plugin overrides

## Testing

### Philosophy

Tests are organized in layers from fast/isolated to slow/integrated. Each layer builds confidence that the layer below didn't miss something:

1. **Unit tests** (`coordinator/tests/`, `hooks/tests/`) — Fast, no network. Use `fakeredis` for in-memory Redis and mock HTTP servers for hook tests. Every module has its own test file. These run in seconds and should always pass before committing.

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

- **Before committing**: Unit tests (`coordinator/tests/` and `hooks/tests/`)
- **Before deploying**: Acceptance tests (`tests/acceptance/run-acceptance.sh`)
- **After deploying**: Smoke-check with `scripts/test_e2e.sh` or E2E integration tests

## Container Runtime

Use `finch` instead of `docker` on this machine.
