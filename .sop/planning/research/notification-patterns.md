# MCP Notification Patterns Research

## Summary

MCP supports server-to-client notifications, but **Claude Code doesn't automatically act on them**. The best patterns for c3po are **blocking tool calls** and **Stop hooks**.

---

## Key Findings

### 1. MCP Notification Support vs Claude Code Reality

| Feature | MCP Spec | Claude Code |
|---------|----------|-------------|
| `notifications/message` | Yes | Receives but ignores |
| `notifications/progress` | Yes | Displays progress bar |
| `notifications/resources/updated` | Yes | Refreshes resources |
| Server-initiated sampling | Yes | Limited/Unknown |
| Auto-resume on notification | No standard | Not implemented |

**Critical limitation:** Claude Code receives notifications but does not take action on them.

### 2. Pattern A: Blocking Tool Call (Recommended)

Create an MCP tool that **blocks** until a message arrives:

```python
@mcp.tool
def wait_for_request(ctx: Context, timeout: int = 60) -> dict:
    """
    Wait for an incoming request from another agent.
    Blocks until a request arrives or timeout is reached.
    """
    agent_id = ctx.get_state("agent_id")

    # BLPOP blocks until item available or timeout
    result = redis.blpop(f"inbox:{agent_id}", timeout=timeout)

    if result is None:
        return {"status": "timeout", "message": "No requests received"}

    request = json.loads(result[1])
    return {
        "status": "received",
        "request": request
    }
```

**Usage in prompt:**
```
You are the homeassistant agent. Use wait_for_request to listen for
incoming requests from other agents. When you receive a request,
process it and use respond_to_request to send your answer.
```

### 3. Pattern B: Stop Hook Polling

When Claude finishes a task, check for pending work:

```python
# ~/.claude/hooks/check_inbox.py
#!/usr/bin/env python3
import json
import requests
import sys
import os

COORDINATOR = os.environ.get("C3PO_COORDINATOR", "http://nas:8420")
AGENT_ID = os.environ.get("C3PO_AGENT_ID")

try:
    resp = requests.get(
        f"{COORDINATOR}/api/pending",
        headers={"X-Agent-ID": AGENT_ID},
        timeout=5
    )
    pending = resp.json()

    if pending.get("count", 0) > 0:
        output = {
            "decision": "block",
            "reason": f"You have {pending['count']} pending request(s). Use get_pending_requests to retrieve and process them."
        }
        print(json.dumps(output))
except:
    pass  # Fail open

sys.exit(0)
```

**Pros:** Works even if agent wasn't explicitly waiting
**Cons:** Only triggers when Claude finishes a task

### 4. Pattern C: Hybrid Approach (Best)

Combine both patterns:

1. **Stop hook** catches requests when agent finishes other work
2. **wait_for_request** tool for agents explicitly listening
3. **get_pending_requests** for on-demand checking

```
┌─────────────────────────────────────────────────────────────┐
│                    Agent Lifecycle                          │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│   [User Task] ───► [Agent Works] ───► [Stop Hook]          │
│                                            │                │
│                                     Check pending?          │
│                                        │     │              │
│                                       Yes    No             │
│                                        │     │              │
│                                        ▼     ▼              │
│                                    [Process] [Idle]         │
│                                     Request                 │
│                                        │                    │
│                                        ▼                    │
│                                   [Stop Hook]               │
│                                   (repeat)                  │
│                                                             │
│   Alternative: Agent can call wait_for_request(60)          │
│   to actively listen for requests                           │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

### 5. Why Not SSE Push Notifications?

Even though MCP supports SSE:
1. Claude Code primarily uses stdio transport
2. CC doesn't auto-act on notifications (GitHub issue #1478 - closed NOT_PLANNED)
3. Blocking tool pattern achieves same result more reliably

### 6. Timeout Considerations

- **Blocking tool timeout:** 60s default, configurable
- **Stop hook timeout:** 60s default (hook must complete within this)
- **Redis BLPOP:** Matches tool timeout
- **Coordinator health check:** Include in timeout calculation

---

## Recommended Implementation

### Coordinator Endpoints

```
POST /mcp              - FastMCP HTTP endpoint
GET  /api/pending      - Quick check for pending requests (for hooks)
GET  /api/health       - Health check
```

### MCP Tools

| Tool | Description | Blocking? |
|------|-------------|-----------|
| `list_agents` | List online agents | No |
| `send_request` | Send request to agent | No |
| `get_pending_requests` | Get all pending requests | No |
| `respond_to_request` | Respond to a request | No |
| `wait_for_request` | Block until request arrives | Yes |
| `wait_for_response` | Block until response arrives | Yes |

### Hooks (in plugin)

| Hook | Purpose |
|------|---------|
| `SessionStart` | Register agent, set identity |
| `Stop` | Check for pending requests |

---

## Sources

- [MCP Specification](https://modelcontextprotocol.io/specification/2025-06-18)
- [Claude Code Issue #3174](https://github.com/anthropics/claude-code/issues/3174) - Notifications not displayed
- [Claude Code Issue #1478](https://github.com/anthropics/claude-code/issues/1478) - Auto-resume (NOT_PLANNED)
- [MCP Async Tasks](https://workos.com/blog/mcp-async-tasks-ai-agent-workflows)
