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

When `C3PO_AGENT_ID_FILE` is set, the SessionStart hook writes the assigned
agent ID to that path on successful registration. The watcher owns this file
and is responsible for choosing a unique, stable path.

### C3PO_AGENT_ID_FILE

Set this to an absolute path before launching Claude Code. The SessionStart hook
writes the assigned agent ID to this file after successful registration. Use it
when your entrypoint needs to discover the agent ID after the session exits:

```bash
export C3PO_AGENT_ID_FILE=/run/my-watcher/agent-id
claude  # On exit, agent ID will be in that file
agent_id=$(cat /run/my-watcher/agent-id)
```

The watcher chooses the path and ensures it's unique (e.g. per-rig or per-crew).
The file is not managed by any hook — create, read, and clean it up yourself.

## Cold Start — Pre-Claiming an Agent Name (wait_first Pattern)

Some trigger-based agents have no internal work queue to drain on startup — they
should only launch when a message is waiting. A `wait_first` watcher must claim
the agent name *before* the first container run so that other agents can send
messages to it and the `/agent/api/wait` poll can begin.

On the very **first** cold start (no prior registry entry), use this sequence:

```
1. POST /agent/api/register   (X-Machine-Name: machine/project, no X-Session-ID)
       └─ Creates an offline-ready registry entry
          Returns the actual assigned agent_id (confirms no collision suffix)

2. POST /agent/api/unregister?keep=true   (X-Machine-Name: assigned_id)
       └─ Immediately marks the entry offline
          Leaves inbox intact — same state as ensure_placeholder
          Safe for the real agent's SessionStart hook to re-register (Case 3)

3. GET /agent/api/wait   (normal polling begins)
```

**Why step 2 is required:** Registering without an `X-Session-ID` creates an
entry with `session_id=null`. If the real agent's SessionStart hook arrives while
that entry is still in a newly-registered state, collision detection will fire
and the agent gets a `-2` suffix. Calling `unregister?keep=true` immediately
transitions the entry to the same offline state as a post-session keep, which
the coordinator recognises as safe to update in-place (Case 3 collision logic).

**After the first run:** With `C3PO_KEEP_REGISTERED=1`, the SessionEnd hook calls
`unregister?keep=true` and the entry persists across restarts. The watcher can
skip steps 1–2 and go straight to step 3.

Use the `c3po-claim-name` script (in `scripts/`) to do this in one step:

```bash
agent_id=$(c3po-claim-name myproject)
# agent_id is now e.g. "machine/myproject" — ready to pass to the watcher
```

The script derives the machine name using the same priority logic as the hook
scripts (`C3PO_MACHINE_NAME` → `~/.claude.json` header → hostname), sanitizes
both components, and performs the register+unregister?keep=true sequence.
Diagnostic messages go to stderr; only the agent_id is written to stdout.

Or manually:

```python
# Cold-start pre-claim (run once on first launch)
resp = requests.post(f"{url}/agent/api/register",
                     headers={**auth_headers, "X-Machine-Name": "machine/project"})
agent_id = resp.json()["id"]   # e.g. "machine/project" (no suffix = good)

requests.post(f"{url}/agent/api/unregister?keep=true",
              headers={**auth_headers, "X-Machine-Name": agent_id})

# Now begin normal watcher polling
while True:
    resp = requests.get(f"{url}/agent/api/wait", headers=auth_headers)
    ...
```

**Key guarantee:** `register` (no session) + `unregister?keep=true` leaves the
registry entry in an offline state that is semantically identical to
`ensure_placeholder`. Future coordinator changes must preserve this invariant
so that cold-start watchers continue to work correctly.

## API Reference

All watcher-facing endpoints use the same auth as `/agent/*` endpoints:
`Authorization: Bearer <api_token>` with `X-Machine-Name: machine/project`.

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `POST /agent/api/register` | POST | Pre-claim agent name on cold start (wait_first) |
| `GET /agent/api/wait` | GET | Long-poll until message arrives |
| `GET /agent/api/pending` | GET | Non-blocking peek (polling fallback) |
| `POST /agent/api/unregister?keep=true` | POST | Called by hook with `C3PO_KEEP_REGISTERED=1` or after pre-claim |
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

Response on server shutdown/restart (with `Retry-After: 15` HTTP header):
```json
{"count": 0, "status": "retry"}
```

Your polling loop should handle all three cases:

```python
response = requests.get(url, headers=headers, timeout=timeout + 5)
data = response.json()

if data["status"] == "received":
    wake_agent(data["messages"])
elif data["status"] == "retry":
    retry_after = int(response.headers.get("Retry-After", "15"))
    time.sleep(retry_after)   # server is restarting; reconnect shortly
    # then loop
elif data["status"] == "timeout":
    pass  # loop immediately
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

**Not using `C3PO_AGENT_ID_FILE` with the watcher**: Without it, there's no
reliable way for the entrypoint to discover the assigned agent ID after Claude
exits (the session file is named by Claude's internal session UUID). Set
`C3PO_AGENT_ID_FILE` to a stable path your entrypoint controls.

## Canonical Example

See `ithaca/lib/wait-for-trigger.py` and `ithaca/run-ithaca.sh` for a reference
implementation of this pattern.
