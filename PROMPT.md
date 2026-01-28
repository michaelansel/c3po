# C3PO Implementation

## Objective

Build a multi-agent coordination system for Claude Code that enables CC instances on different hosts to send requests to each other, have multi-turn conversations, and collaborate on cross-cutting problems—without requiring the human to relay messages.

## Key Requirements

### Coordinator (Python/FastMCP + Redis)
- HTTP MCP server on port 8420 accepting connections from multiple CC instances
- Agent identification via `X-Agent-ID` header (defaults to folder name)
- Redis-backed message queues (`inbox:{agent}`, `responses:{agent}`)
- MCP tools: `list_agents`, `send_request`, `get_pending_requests`, `respond_to_request`, `wait_for_response`, `wait_for_request`
- REST endpoints: `GET /api/health`, `GET /api/pending` (for hooks)
- Docker packaging with docker-compose (coordinator + Redis)

### Plugin (Claude Code Plugin format)
- MCP config pointing to coordinator with X-Agent-ID header
- Stop hook that checks `/api/pending` and blocks if requests pending
- SessionStart hook that confirms connection and shows online agents
- `/coordinate` skill for status checks and quick messaging

### Behavior
- Human-initiated collaboration only (no automatic agent suggestions)
- Async request/response with multi-turn support
- Graceful degradation when coordinator unavailable
- Human interrupt (Esc/Ctrl-C) works normally

## Acceptance Criteria

1. Two CC instances can exchange messages via coordinator (< 10s latency)
2. Stop hook triggers Claude to process pending requests
3. Multi-turn back-and-forth conversations work
4. System continues working locally if coordinator is down
5. New host setup: install plugin + set env vars

## Environment Variables

- `C3PO_COORDINATOR_URL` - Coordinator URL (default: `http://localhost:8420`)
- `C3PO_AGENT_ID` - Agent identifier (default: folder name)

## Implementation Guide

Follow the step-by-step implementation plan:
**`.sop/planning/implementation/plan.md`**

Each of the 13 steps produces a working, demoable increment. Complete the demo for each step before proceeding.

## Reference Documents

- **Detailed Design**: `.sop/planning/design/detailed-design.md`
- **Requirements**: `.sop/planning/idea-honing.md`
- **Research**: `.sop/planning/research/`

## Tech Stack

- **Coordinator**: Python 3.11+, FastMCP, Redis, uvicorn
- **Plugin**: Claude Code plugin format (.claude-plugin/, .mcp.json, hooks/)
- **Deployment**: Docker, docker-compose
