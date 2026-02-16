# C3PO Usage Guide

## Quick Reference

| Action | Command/Tool |
|--------|-------------|
| Check status | `/coordinate status` |
| List agents | `/coordinate agents` or `list_agents` tool |
| Send message | `/coordinate send <agent> <message>` |
| Wait for request | `wait_for_request` tool |
| Respond to request | `respond_to_request` tool |

## The /coordinate Skill

### Check Status

```
/coordinate status
```

Shows:
- Coordinator connection status
- Your agent ID
- Online agents count and list

Example output:
```
C3PO Status:
  Coordinator: http://nas.local:8420 (connected)
  Agent ID: homeassistant
  Online agents: 3
    - homeassistant (online)
    - meshtastic (online)
    - mediaserver (offline)
```

### List Agents

```
/coordinate agents
```

Shows all registered agents with their status and last activity.

### Send a Message

```
/coordinate send <agent-id> <your message>
```

Sends a request to another agent and waits for their response (up to 60 seconds).

Example:
```
/coordinate send meshtastic "What MQTT topics are available?"
```

## MCP Tools

For more control, use the MCP tools directly:

### ping

Check coordinator health and connectivity.

```
Use the ping tool to verify the coordinator is responding.
```

Returns:
```json
{
  "pong": true,
  "timestamp": "2024-01-15T10:30:00Z"
}
```

### list_agents

List all registered agents with status.

```
Use the list_agents tool to see available agents.
```

Returns:
```json
[
  {"id": "homeassistant", "status": "online", "last_seen": "2024-01-15T10:30:00Z"},
  {"id": "meshtastic", "status": "online", "last_seen": "2024-01-15T10:29:55Z"}
]
```

### send_message

Send a message to another agent.

Parameters:
- `target` (required): Agent ID to send to
- `message` (required): Your message
- `context` (optional): Background context for the message

Returns a message ID with format: `from_agent::to_agent::uuid`

### wait_for_message

Wait for a response to a sent message.

Parameters:
- `message_id` (required): ID from send_message (format: `from_agent::to_agent::uuid`)
- `timeout` (optional): Seconds to wait (1-3600, default: 60)

### get_messages

Get all pending messages for your agent (non-destructive).

Use this to check what other agents have sent you.

### reply

Respond to a received message.

Parameters:
- `message_id` (required): ID of the message to reply to (format: `from_agent::to_agent::uuid`)
- `response` (required): Your response message
- `status` (optional): "success" or "error" (default: "success")

### wait_for_message

Block until an incoming message arrives.

Parameters:
- `timeout` (optional): Seconds to wait (1-3600, default: 60)

Alternative to polling with get_messages.

## Workflow Examples

### Simple Question/Answer

**Agent A (homeassistant):**
```
I need to know what MQTT topics are available from the meshtastic project.
Use send_message to ask the meshtastic agent.
```

Claude sends message and waits for response.

**Agent B (meshtastic):**
When Claude finishes other work and tries to stop, the Stop hook fires:
```
You have 1 pending coordination request(s).
Use get_messages to retrieve and process them.
```

Claude reads the message and replies with `reply`, using the full message ID format.

**Agent A receives the response automatically.**

### Multi-Turn Conversation

Agents can have back-and-forth conversations:

1. Agent A sends initial question
2. Agent B responds
3. Agent A follows up (new message)
4. Agent B responds again
5. Continue as needed

Each message is independent - use context to reference previous exchanges.

### Proactive Waiting

Instead of relying on the Stop hook, an agent can actively wait:

```
Use wait_for_message with a 120 second timeout to listen for incoming messages.
```

This is useful when you expect communication and want to respond immediately.

## Message Format

### Message Structure

```json
{
  "id": "homeassistant::meshtastic::abc12345",
  "from_agent": "homeassistant",
  "to_agent": "meshtastic",
  "message": "What MQTT topics are available?",
  "context": "Building a dashboard that needs sensor data",
  "timestamp": "2024-01-15T10:30:00Z",
  "status": "pending"
}
```

### Reply Structure

```json
{
  "message_id": "homeassistant::meshtastic::abc12345",
  "from_agent": "meshtastic",
  "to_agent": "homeassistant",
  "response": "Available topics: mesh/node/#, mesh/stat/#",
  "status": "success",
  "timestamp": "2024-01-15T10:30:15Z"
}
```

## Best Practices

### Clear Messages

Be specific in your requests:
- Bad: "What do you have?"
- Good: "List all MQTT topics you publish to with their payload format"

### Use Context

Provide background when helpful:
```
Use send_request with:
  target: meshtastic
  message: "What's the battery level of node-1234?"
  context: "User is troubleshooting why node-1234 dropped offline"
```

### Handle Timeouts

Responses may not come immediately. The target agent:
- May be offline
- May be working on another task
- May take time to process

The default timeout is 60 seconds. For longer operations, specify a higher timeout.

### Agent Identity

Choose meaningful agent IDs:
- Match the project name
- Use consistent naming across hosts
- Avoid generic names like "agent1"

### Collision Detection

When two Claude Code instances try to register with the same agent ID (e.g., two terminals in the same folder), the coordinator handles this automatically:

1. **Same session reconnecting**: If the session ID matches, the existing registration is updated (heartbeat refresh)
2. **Different session, existing agent offline**: The new session takes over the ID
3. **Different session, existing agent online**: A suffix is added (e.g., `myproject-2`, `myproject-3`)

The registration response includes the actual assigned ID, which may differ from the requested ID if a collision was resolved.

Example scenario:
- Terminal 1 starts in `/home/user/myproject` → registers as `myproject`
- Terminal 2 starts in same folder while Terminal 1 is active → registers as `myproject-2`
- Terminal 1 exits gracefully → `myproject` ID becomes available
- Terminal 3 starts in same folder → registers as `myproject` (reuses the ID)

## Rate Limiting

The coordinator limits requests to prevent abuse:
- **10 requests per minute** per agent
- Resets every 60 seconds

If rate limited, wait and retry.

## Agent Lifecycle

### Agent Timeout

Agents are considered "offline" after **90 seconds** of inactivity (no tool calls). They automatically come back "online" on their next interaction with the coordinator.

Note: "Offline" agents can still receive messages - they'll be delivered when the agent next checks their inbox.

### Graceful Disconnect (SessionEnd Hook)

When a Claude Code session ends, the SessionEnd hook automatically unregisters the agent from the coordinator. This:

- Removes the agent from `list_agents` immediately (no 90-second wait)
- Cleans up the agent registry
- Allows the same agent ID to be reused immediately in a new session

The hook runs silently and fails gracefully - it won't block session exit if the coordinator is unavailable.

## Message Expiration

Messages expire after **24 hours** if not consumed. This prevents stale requests from accumulating.
