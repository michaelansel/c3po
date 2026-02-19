# Watcher Pattern — Offline Agent with Companion Process

This document describes how to use C3PO to keep an agent discoverable and
message-accepting even after its Claude Code session exits.

## What It Is

A **watcher** is an external process (not a Claude Code session) that monitors
an offline agent's inbox and wakes the agent when messages arrive. This enables
workflows like `run-ithaca.sh` + `wait-for-trigger.py`, where a companion
process polls for messages and relaunches the agent when work arrives.

## Lifecycle

```
Claude Code exits
    └─ SessionEnd hook calls POST /agent/api/unregister?keep=true
           └─ Registry entry kept; agent immediately appears offline
                    │
                    ▼
         Watcher polls GET /agent/api/wait?timeout=30
                    │
         Message arrives (from another agent)
                    │
                    ▼
         Watcher wakes Claude Code (e.g. runs run-ithaca.sh)
                    │
         Claude Code re-registers with same machine/project name
           └─ Offline entry updated in-place → same agent_id returned
           └─ Queued messages are waiting in inbox
                    │
         Agent processes messages and exits
           └─ Watcher calls POST /agent/api/unregister (no keep)
                   ├─ Empty inbox → full cleanup (agent removed, keys deleted)
                   └─ Pending messages → mark offline + keep (messages preserved)
```

## Configuration

### C3PO_KEEP_REGISTERED=1

Set this environment variable **before launching Claude Code**. The SessionEnd
hook reads it and calls `POST /agent/api/unregister?keep=true` instead of the
normal removal. The agent immediately appears offline in `list_agents`, preventing
collision detection if the watcher relaunches Claude Code within 15 minutes.

```bash
export C3PO_KEEP_REGISTERED=1
claude  # Start Claude Code — on exit, agent will be kept + marked offline
```

The session file (`/tmp/c3po-agent-id-<session>`) is always deleted regardless
of this setting.

## API Reference

All watcher-facing endpoints use the same auth as `/agent/*` endpoints:
`Authorization: Bearer <api_token>` with `X-Machine-Name: machine/project`.

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `GET /agent/api/wait` | GET | Long-poll until message arrives |
| `GET /agent/api/pending` | GET | Non-blocking peek (polling fallback) |
| `POST /agent/api/unregister?keep=true` | POST | Called by hook with `C3PO_KEEP_REGISTERED=1` |
| `POST /agent/api/unregister` | POST | Called by watcher on clean shutdown |

### GET /agent/api/wait

Blocks until messages arrive or timeout expires.

**Does NOT update agent heartbeat.** The agent correctly shows as offline while
the watcher runs. (Use the MCP `wait_for_message` tool if you need heartbeats.)

Query parameters:
- `timeout`: seconds to wait, 1–3600 (default: 30)

Response when messages arrive:
```json
{"count": 1, "messages": [...], "status": "received"}
```

Response on timeout:
```json
{"count": 0, "status": "timeout"}
```

### POST /agent/api/unregister?keep=true

Marks agent offline without removing registry entry. Inbox remains intact.

Response:
```json
{
  "status": "ok",
  "message": "Agent 'machine/project' marked offline and kept in registry",
  "pending_messages": false,
  "kept": true
}
```

### POST /agent/api/unregister (watcher shutdown)

Normal unregister called by watcher on clean shutdown.

- Empty inbox → full cleanup (agent removed, inbox/notify/acked keys deleted)
- Pending messages → agent kept as offline (preserves queued messages)

## Credentials

Watchers read credentials from `~/.claude/c3po-credentials.json`:

```json
{
  "coordinator_url": "https://mcp.example.com",
  "api_token": "server_secret.api_key"
}
```

Auth header: `Authorization: Bearer <api_token>`

## Reconnect — Same Agent ID Guaranteed

When the watcher wakes the agent, the SessionStart hook sends the same
`machine/project` identifier. The coordinator finds the existing offline entry
and updates it in-place (no `-2` suffix collision), returning the same `agent_id`.
Queued messages in the inbox are untouched.

## Heartbeat Note

Watcher endpoints intentionally do **not** touch the heartbeat (`last_seen`).
The agent appears offline while the watcher runs, which is correct — the watcher
is not the agent. Only the agent's own MCP tool calls refresh the heartbeat.

## Common Pitfalls

**Forgetting `C3PO_KEEP_REGISTERED=1`**: Without it, the SessionEnd hook calls
plain `unregister`, removing the agent from the registry. The next `send_message`
from another agent will fail with "agent not found" (unless `deliver_offline=True`
is passed, which creates a placeholder, but only if the sender knows to use it).

**Using MCP tools from the watcher**: MCP tool calls touch the heartbeat and keep
the agent online. Use REST endpoints from external watcher processes.

**Not acking messages after processing**: The REST `/api/wait` endpoint uses
peek+ack semantics — messages stay in the inbox until explicitly acked. The agent
should call `ack_messages` after processing.

## Canonical Example

See `ithaca/lib/wait-for-trigger.py` and `ithaca/run-ithaca.sh` for a reference
implementation of this pattern.
