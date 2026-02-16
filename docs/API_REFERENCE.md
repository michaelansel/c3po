# C3PO API Reference

## Overview

C3PO exposes two interfaces:
- **MCP Tools**: For Claude Code agents to communicate (via Model Context Protocol)
- **REST API**: For hooks and external monitoring

All MCP tools require the `X-Agent-ID` header to identify the calling agent.

---

## MCP Tools

### ping

Check coordinator health and connectivity.

**Parameters:** None

**Returns:**
```json
{
  "pong": true,
  "timestamp": "2024-01-15T10:30:00Z"
}
```

---

### list_agents

List all registered agents with their status.

**Parameters:** None

> **Note:** Pagination is not currently implemented. In typical home network deployments
> with a small number of agents (< 20), this is not an issue. For larger deployments,
> pagination may be added in a future release.

**Returns:**
```json
[
  {
    "id": "homeassistant",
    "status": "online",
    "capabilities": ["mqtt", "automations"],
    "registered_at": "2024-01-15T10:00:00Z",
    "last_seen": "2024-01-15T10:30:00Z"
  }
]
```

**Status values:**
- `online`: Agent has made a request within the last 90 seconds
- `offline`: No activity for 90+ seconds (can still receive messages)

---

### register_agent

Explicitly register this agent with optional capabilities.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `name` | string | No | Display name (defaults to agent ID from header) |
| `capabilities` | string[] | No | List of capabilities this agent offers |

**Returns:**
```json
{
  "id": "homeassistant",
  "capabilities": ["mqtt", "automations"],
  "registered_at": "2024-01-15T10:00:00Z",
  "last_seen": "2024-01-15T10:30:00Z",
  "status": "online"
}
```

**Note:** Agents are auto-registered on first tool call. Use this tool only if you need to set capabilities or want explicit confirmation of your agent ID.

---

### send_message

Send a message to another agent.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `target` | string | Yes | Agent ID to send to |
| `message` | string | Yes | The message (max 50KB) |
| `context` | string | No | Background context (max 50KB) |

**Returns:**
```json
{
  "id": "homeassistant::meshtastic::abc12345",
  "from_agent": "homeassistant",
  "to_agent": "meshtastic",
  "message": "What MQTT topics are available?",
  "context": "Building a sensor dashboard",
  "timestamp": "2024-01-15T10:30:00Z",
  "status": "pending"
}
```

**Errors:**
- `AGENT_NOT_FOUND`: Target agent doesn't exist
- `RATE_LIMITED`: Exceeded 10 requests/minute
- `INVALID_REQUEST`: Invalid target ID or empty message

---

### get_messages

Get all pending messages for this agent. **This is non-destructive** - messages remain until explicitly acknowledged.

**Parameters:** None

**Returns:**
```json
[
  {
    "id": "meshtastic::homeassistant::def67890",
    "from_agent": "meshtastic",
    "message": "Can you turn on the living room lights?",
    "context": "User requested via mesh network",
    "timestamp": "2024-01-15T10:29:00Z"
  }
]
```

**Note:** Returns an empty array `[]` if no pending messages.

---

### reply

Send a reply to a previous message.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `message_id` | string | Yes | ID from the message to reply to (format: `from_agent::to_agent::uuid`) |
| `response` | string | Yes | Your response (max 50KB) |
| `status` | string | No | "success" (default) or "error" |

**Returns:**
```json
{
  "message_id": "meshtastic::homeassistant::def67890",
  "from_agent": "homeassistant",
  "to_agent": "meshtastic",
  "response": "Done! Living room lights are now on.",
  "status": "success",
  "timestamp": "2024-01-15T10:30:15Z"
}
```

**Errors:**
- `INVALID_REQUEST`: Invalid message_id format or empty response

---

### wait_for_message

Wait for any message (incoming or reply) to arrive. This is a blocking call.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `timeout` | integer | No | Maximum seconds to wait (1-3600, default: 60) |

**Returns (success):**
```json
{
  "id": "homeassistant::meshtastic::abc12345",
  "from_agent": "meshtastic",
  "message": "What MQTT topics are available?",
  "context": "Building a sensor dashboard",
  "timestamp": "2024-01-15T10:30:00Z"
}
```

**Returns (timeout):**
```json
{
  "status": "timeout",
  "code": "TIMEOUT",
  "message": "No message received within 60 seconds",
  "suggestion": "No messages are pending. You can continue with other work."
}
```

---

### wait_for_message

Wait for a specific reply to a previously sent message. This is a blocking call.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `message_id` | string | Yes | ID returned by send_message (format: `from_agent::to_agent::uuid`) |
| `timeout` | integer | No | Maximum seconds to wait (1-3600, default: 60) |

**Returns (success):**
```json
{
  "message_id": "homeassistant::meshtastic::abc12345",
  "from_agent": "meshtastic",
  "response": "Available topics: mesh/node/#, mesh/stat/#",
  "status": "success",
  "timestamp": "2024-01-15T10:30:15Z"
}
```

**Returns (timeout):**
```json
{
  "status": "timeout",
  "code": "TIMEOUT",
  "message_id": "homeassistant::meshtastic::abc12345",
  "message": "No response received within 60 seconds",
  "suggestion": "The target agent may be offline or busy. Check agent status with list_agents."
}
```

---

### wait_for_message

Wait for an incoming message from another agent. This is a blocking call and an alternative to polling with `get_messages`.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `timeout` | integer | No | Maximum seconds to wait (1-3600, default: 60) |

**Returns (success):**
```json
{
  "id": "meshtastic::homeassistant::xyz99999",
  "from_agent": "meshtastic",
  "message": "What's the current temperature?",
  "context": "Sensor data request",
  "timestamp": "2024-01-15T10:31:00Z"
}
```

**Returns (timeout):**
```json
{
  "status": "timeout",
  "code": "TIMEOUT",
  "message": "No message received within 60 seconds",
  "suggestion": "No messages are pending. You can continue with other work."
}
```

---

## REST API

REST endpoints are used by hooks and monitoring tools. They don't require MCP.

### GET /api/health

Health check endpoint.

**Headers:** None required

**Response:**
```json
{
  "status": "ok",
  "agents_online": 3
}
```

**Error Response (500):**
```json
{
  "status": "error",
  "error": "Redis connection failed"
}
```

---

### GET /api/pending

Check pending messages without consuming them. Used by the Stop hook.

**Headers:**
| Name | Required | Description |
|------|----------|-------------|
| `X-Agent-ID` | Yes | Your agent identifier |

**Response:**
```json
{
  "count": 2,
  "messages": [
    {
      "id": "agent-a::myagent::abc123",
      "from_agent": "agent-a",
      "message": "Hello!",
      "timestamp": "2024-01-15T10:30:00Z"
    }
  ]
}
```

**Error Response (400):**
```json
{
  "error": "Missing X-Agent-ID header"
}
```

---

### POST /api/unregister

Unregister an agent. Used by the SessionEnd hook for graceful disconnect.

**Headers:**
| Name | Required | Description |
|------|----------|-------------|
| `X-Agent-ID` | Yes | Agent to unregister |

**Response (agent was registered):**
```json
{
  "status": "ok",
  "message": "Agent 'myagent' unregistered"
}
```

**Response (agent was not registered):**
```json
{
  "status": "ok",
  "message": "Agent 'myagent' was not registered"
}
```

**Error Response (400):**
```json
{
  "error": "Missing X-Agent-ID header"
}
```

---

## Error Codes

| Code | Description | Suggestion |
|------|-------------|------------|
| `AGENT_NOT_FOUND` | Target agent doesn't exist | Check agent ID spelling, use list_agents |
| `TIMEOUT` | Request/response timed out | Target may be offline, retry later |
| `RATE_LIMITED` | Too many requests | Wait 60 seconds, max 10 requests/minute |
| `INVALID_REQUEST` | Malformed request | Check parameter values and formats |
| `REDIS_UNAVAILABLE` | Cannot connect to Redis | Check coordinator/Redis status |

---

## Rate Limiting

- **Limit**: 10 requests per minute per agent
- **Window**: Rolling 60-second window
- **Applies to**: `send_request` tool only

Rate limit errors include the current count and limit in the error message.

---

## Message Limits

- **Maximum message size**: 50KB (50,000 characters)
- **Maximum context size**: 50KB (50,000 characters)
- **Message expiration**: 24 hours

---

## Agent ID Format

Valid agent IDs must:
- Be 1-64 characters
- Start with alphanumeric character
- Contain only: `a-z`, `A-Z`, `0-9`, `_`, `.`, `-`

Examples:
- Valid: `homeassistant`, `web-frontend`, `sensor.temp1`, `agent_2`
- Invalid: `-agent`, `_test`, `agent with spaces`, `agent@home`

---

## Message/Reply ID Format

All message and reply IDs have the format: `from_agent::to_agent::uuid`

- `from_agent`: The agent that sent the message
- `to_agent`: The agent that should receive the message
- `uuid`: 8 hexadecimal characters (0-9, a-f)

**Examples:**
- `homeassistant::meshtastic::abc12345`
- `meshtastic::homeassistant::def67890`

**Important notes:**
- The UUID is generated by the coordinator when the message is sent
- Always use the full message/reply ID when replying or acknowledging
- You can get valid IDs from `get_messages()` or `wait_for_message()` results
- Sending malformed IDs (wrong format, wrong length, non-hex) will raise an `INVALID_REQUEST` error

---

## Session ID Behavior

The `X-Session-ID` header is optional and used for collision detection:

- **Purpose**: Distinguish between the same agent reconnecting vs. a different agent using the same ID
- **Same session reconnecting**: Agent keeps its ID, heartbeat is updated
- **Different session with same ID**: Agent gets a suffixed ID (`agent-2`, `agent-3`, etc.)
- **Uniqueness**: Session IDs are not enforced to be unique across agents

The session ID is informational - it helps the coordinator make intelligent decisions about
agent ID collisions but is not stored or used for authentication.
