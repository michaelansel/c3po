# Claude Code Hooks Research

## Summary

Claude Code hooks are a powerful mechanism for customizing behavior at key lifecycle events. For c3po, hooks can **notify agents of incoming requests** without modifying Claude Code itself.

---

## Key Findings

### 1. Hook Events Available

| Hook Event | When It Fires | Useful For c3po? |
|------------|---------------|------------------|
| `Stop` | Claude finishes responding | **Yes** - check for pending requests |
| `UserPromptSubmit` | User submits prompt | **Yes** - inject pending request context |
| `SessionStart` | Session begins/resumes | Yes - initialize agent identity |
| `PreToolUse` | Before tool execution | Maybe - intercept certain tools |
| `PostToolUse` | After tool succeeds | Maybe - log coordination activity |
| `Notification` | CC sends notifications | Maybe - intercept idle prompts |

### 2. Stop Hook - Primary Pattern for Incoming Requests

The `Stop` hook can **block Claude from stopping** if there's pending work:

```json
{
  "hooks": {
    "Stop": [{
      "hooks": [{
        "type": "command",
        "command": "python3 ~/.claude/hooks/check_pending_requests.py"
      }]
    }]
  }
}
```

**check_pending_requests.py:**
```python
#!/usr/bin/env python3
import json
import requests
import sys
import os

COORDINATOR_URL = os.environ.get("C3PO_COORDINATOR", "http://nas:8420")
AGENT_ID = os.environ.get("C3PO_AGENT_ID", "unknown")

try:
    response = requests.get(
        f"{COORDINATOR_URL}/pending",
        headers={"X-Agent-ID": AGENT_ID},
        timeout=5
    )
    pending = response.json()

    if pending.get("requests"):
        req = pending["requests"][0]
        output = {
            "decision": "block",
            "reason": f"You have a pending request from agent '{req['from']}': {req['message']}. Please process this using the c3po tools."
        }
        print(json.dumps(output))
        sys.exit(0)
except Exception as e:
    # Fail open - don't block if coordinator is down
    pass

sys.exit(0)  # Allow stop if no pending requests
```

**How it works:**
1. Claude finishes a task and tries to stop
2. Stop hook intercepts and checks coordinator for pending requests
3. If pending, returns `decision: "block"` with instructions
4. Claude receives the reason and continues processing

### 3. UserPromptSubmit Hook - Context Injection

Can inject pending request information before Claude processes user input:

```json
{
  "hooks": {
    "UserPromptSubmit": [{
      "hooks": [{
        "type": "command",
        "command": "python3 ~/.claude/hooks/inject_pending_context.py"
      }]
    }]
  }
}
```

The hook's stdout becomes additional context for Claude.

### 4. Existing Solution: HCOM

[HCOM (Hook Communications)](https://github.com/aannoo/hcom) is an existing inter-agent communication system:

- Uses SQLite as message bus
- Mid-turn message delivery
- @-mention targeting for specific agents
- Event subscriptions

**Architecture:** `agents → hooks → sqlite → hooks → other agents`

We could either use HCOM directly or borrow its patterns.

### 5. Hook Data Available

Hooks receive via stdin:
```json
{
  "session_id": "abc123",
  "transcript_path": "/path/to/session.jsonl",
  "cwd": "/current/working/directory",
  "hook_event_name": "Stop"
}
```

Stop hook also receives `stop_hook_active` boolean to prevent infinite loops.

---

## Recommended Pattern for c3po

### Option A: Stop Hook + MCP Tools (Simpler)

1. **Stop hook** checks coordinator for pending requests
2. If pending, blocks with instructions to use MCP tools
3. Agent uses `get_pending_requests` MCP tool
4. Agent processes request and uses `respond_to_request` MCP tool
5. Agent finishes, stop hook runs again, checks for more requests

**Pros:** Simple, works with existing CC patterns
**Cons:** Only checks when Claude finishes a task (not real-time)

### Option B: Blocking MCP Tool (More Responsive)

1. Agent calls `wait_for_request(timeout=60)` MCP tool
2. Tool blocks until a request arrives or times out
3. Tool returns the request, Claude processes it
4. Repeat

**Pros:** More responsive, cleaner API
**Cons:** Agent must be in a "waiting" state, tool timeout handling

### Option C: Hybrid

- Use Stop hook for opportunistic checking
- Provide optional `wait_for_request` for agents that want to actively listen
- Both patterns work with same coordinator backend

---

## Limitations

1. **No true push notifications** - CC doesn't act on MCP notifications automatically
2. **Hook changes require session restart** - Can't update hooks mid-session
3. **60-second default timeout** - Hooks timeout after 60 seconds (configurable)
4. **Context isolation** - Subagents can't directly pass context to parent

---

## Sources

- [Claude Code Hooks Reference](https://code.claude.com/docs/en/hooks)
- [HCOM - Inter-agent communication](https://github.com/aannoo/hcom)
- [Multi-Agent Observability](https://github.com/disler/claude-code-hooks-multi-agent-observability)
- [Stop Hook Workflow](https://egghead.io/force-claude-to-ask-whats-next-with-a-continuous-stop-hook-workflow~oiqzj)
