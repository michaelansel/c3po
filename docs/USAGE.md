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

### send_request

Send a request to another agent.

Parameters:
- `target` (required): Agent ID to send to
- `message` (required): Your request message
- `context` (optional): Background context for the request

Returns a request ID for tracking the response.

### wait_for_response

Wait for a response to a sent request.

Parameters:
- `request_id` (required): ID from send_request
- `timeout` (optional): Seconds to wait (default: 60)

### get_pending_requests

Get all pending requests for your agent (consumes them).

Use this to check what other agents have asked you.

### respond_to_request

Respond to a request from another agent.

Parameters:
- `request_id` (required): ID of the request to respond to
- `response` (required): Your response message
- `status` (optional): "success" or "error" (default: "success")

### wait_for_request

Block until an incoming request arrives.

Parameters:
- `timeout` (optional): Seconds to wait (default: 60)

Alternative to polling with get_pending_requests.

## Workflow Examples

### Simple Question/Answer

**Agent A (homeassistant):**
```
I need to know what MQTT topics are available from the meshtastic project.
Use send_request to ask the meshtastic agent.
```

Claude sends request and waits for response.

**Agent B (meshtastic):**
When Claude finishes other work and tries to stop, the Stop hook fires:
```
You have 1 pending coordination request(s).
Use get_pending_requests to retrieve and process them.
```

Claude reads the request and responds with `respond_to_request`.

**Agent A receives the response automatically.**

### Multi-Turn Conversation

Agents can have back-and-forth conversations:

1. Agent A sends initial question
2. Agent B responds
3. Agent A follows up (new request)
4. Agent B responds again
5. Continue as needed

Each message is independent - use context to reference previous exchanges.

### Proactive Waiting

Instead of relying on the Stop hook, an agent can actively wait:

```
Use wait_for_request with a 120 second timeout to listen for incoming requests.
```

This is useful when you expect communication and want to respond immediately.

## Message Format

### Request Structure

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

### Response Structure

```json
{
  "request_id": "homeassistant::meshtastic::abc12345",
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

## Rate Limiting

The coordinator limits requests to prevent abuse:
- **10 requests per minute** per agent
- Resets every 60 seconds

If rate limited, wait and retry.

## Message Expiration

Messages expire after **24 hours** if not consumed. This prevents stale requests from accumulating.
