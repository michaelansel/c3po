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

### send_request

Send a request to another agent.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `target` | string | Yes | Agent ID to send to |
| `message` | string | Yes | The request message (max 50KB) |
| `context` | string | No | Background context for the request (max 50KB) |

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

### get_pending_requests

Get all pending requests for this agent. **This consumes the requests** - they will not be returned again.

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

**Note:** Returns an empty array `[]` if no pending requests.

---

### respond_to_request

Respond to a request from another agent.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `request_id` | string | Yes | ID from the request to respond to |
| `response` | string | Yes | Your response message (max 50KB) |
| `status` | string | No | "success" (default) or "error" |

**Returns:**
```json
{
  "request_id": "meshtastic::homeassistant::def67890",
  "from_agent": "homeassistant",
  "to_agent": "meshtastic",
  "response": "Done! Living room lights are now on.",
  "status": "success",
  "timestamp": "2024-01-15T10:30:15Z"
}
```

**Errors:**
- `INVALID_REQUEST`: Invalid request_id format or empty response

---

### wait_for_response

Wait for a response to a previously sent request. This is a blocking call.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `request_id` | string | Yes | ID returned by send_request |
| `timeout` | integer | No | Maximum seconds to wait (default: 60) |

**Returns (success):**
```json
{
  "request_id": "homeassistant::meshtastic::abc12345",
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
  "request_id": "homeassistant::meshtastic::abc12345",
  "message": "No response received within 60 seconds",
  "suggestion": "The target agent may be offline or busy. Check agent status with list_agents."
}
```

---

### wait_for_request

Wait for an incoming request from another agent. This is a blocking call and an alternative to polling with `get_pending_requests`.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `timeout` | integer | No | Maximum seconds to wait (default: 60) |

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
  "message": "No request received within 60 seconds",
  "suggestion": "No agents have sent requests. You can continue with other work."
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

Check pending requests without consuming them. Used by the Stop hook.

**Headers:**
| Name | Required | Description |
|------|----------|-------------|
| `X-Agent-ID` | Yes | Your agent identifier |

**Response:**
```json
{
  "count": 2,
  "requests": [
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

- **Maximum message size**: 50KB (51,200 characters)
- **Maximum context size**: 50KB (51,200 characters)
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
