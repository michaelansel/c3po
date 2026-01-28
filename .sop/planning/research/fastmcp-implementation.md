# FastMCP Implementation Research

## Summary

FastMCP is ideal for the c3po coordinator. It supports HTTP transport, handles multiple concurrent clients, and provides mechanisms for agent identification and authentication.

---

## Key Findings

### 1. HTTP Transport Setup

```python
from fastmcp import FastMCP

mcp = FastMCP("c3po Coordinator")

@mcp.tool
def send_message(recipient: str, message: str) -> dict:
    """Send a message to another agent"""
    return {"status": "sent", "to": recipient}

if __name__ == "__main__":
    mcp.run(transport="http", host="0.0.0.0", port=8420)
```

Endpoint accessible at `http://host:8420/mcp`

### 2. Agent Identification via Middleware

```python
from fastmcp import FastMCP, Context
from fastmcp.server.middleware import Middleware, MiddlewareContext
from fastmcp.server.dependencies import get_http_headers
from fastmcp.exceptions import ToolError

class AgentIdentityMiddleware(Middleware):
    """Extract agent identity from X-Agent-ID header"""

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        headers = get_http_headers()
        agent_id = headers.get("x-agent-id")

        if not agent_id:
            raise ToolError("Missing X-Agent-ID header")

        context.fastmcp_context.set_state("agent_id", agent_id)
        return await call_next(context)

mcp = FastMCP("c3po Coordinator")
mcp.add_middleware(AgentIdentityMiddleware())
```

### 3. Session Handling

FastMCP automatically handles multiple concurrent connections:
- Each client gets isolated session via `Mcp-Session-Id` header
- Context state is per-session
- No shared state unless explicitly managed (Redis)

### 4. Complete Agent Router Example

```python
"""c3po Coordinator MCP Server"""

from fastmcp import FastMCP, Context
from fastmcp.server.middleware import Middleware, MiddlewareContext
from fastmcp.server.dependencies import get_http_headers
from fastmcp.exceptions import ToolError
from collections import defaultdict
from datetime import datetime
from typing import Optional
import redis
import json

# Redis connection for shared state
r = redis.Redis(host='localhost', port=6379, db=0)

class AgentIdentityMiddleware(Middleware):
    async def on_call_tool(self, context: MiddlewareContext, call_next):
        headers = get_http_headers()
        agent_id = headers.get("x-agent-id")

        if not agent_id:
            raise ToolError("Missing X-Agent-ID header")

        context.fastmcp_context.set_state("agent_id", agent_id)

        # Register agent as online
        r.hset("agents:online", agent_id, datetime.now().isoformat())

        return await call_next(context)

mcp = FastMCP(
    name="c3po Coordinator",
    instructions="Central hub for inter-agent communication"
)
mcp.add_middleware(AgentIdentityMiddleware())

@mcp.tool
def list_agents(ctx: Context) -> list[dict]:
    """List all registered agents"""
    agents = r.hgetall("agents:online")
    return [
        {"id": k.decode(), "last_seen": v.decode()}
        for k, v in agents.items()
    ]

@mcp.tool
def send_request(
    ctx: Context,
    target_agent: str,
    message: str,
    context: str = ""
) -> dict:
    """Send a request to another agent"""
    sender = ctx.get_state("agent_id")

    request = {
        "id": f"{sender}-{target_agent}-{datetime.now().timestamp()}",
        "from": sender,
        "to": target_agent,
        "message": message,
        "context": context,
        "timestamp": datetime.now().isoformat(),
        "status": "pending"
    }

    # Push to target agent's queue
    r.rpush(f"inbox:{target_agent}", json.dumps(request))

    return {"status": "sent", "request_id": request["id"]}

@mcp.tool
def get_pending_requests(ctx: Context) -> list[dict]:
    """Get pending requests for this agent"""
    agent_id = ctx.get_state("agent_id")

    requests = []
    while True:
        item = r.lpop(f"inbox:{agent_id}")
        if not item:
            break
        requests.append(json.loads(item))

    return requests

@mcp.tool
def respond_to_request(
    ctx: Context,
    request_id: str,
    response: str,
    status: str = "success"
) -> dict:
    """Respond to a previously received request"""
    agent_id = ctx.get_state("agent_id")

    # Parse request_id to get original sender
    parts = request_id.split("-")
    original_sender = parts[0]

    reply = {
        "request_id": request_id,
        "from": agent_id,
        "to": original_sender,
        "response": response,
        "status": status,
        "timestamp": datetime.now().isoformat()
    }

    r.rpush(f"responses:{original_sender}", json.dumps(reply))

    return {"status": "responded"}

@mcp.tool
def wait_for_response(
    ctx: Context,
    request_id: str,
    timeout: int = 60
) -> dict:
    """Wait for a response to a previously sent request"""
    agent_id = ctx.get_state("agent_id")

    # Blocking wait on Redis list
    result = r.blpop(f"responses:{agent_id}", timeout=timeout)

    if result is None:
        return {"status": "timeout", "request_id": request_id}

    response = json.loads(result[1])
    return response

if __name__ == "__main__":
    mcp.run(transport="http", host="0.0.0.0", port=8420)
```

### 5. Client Connection (CC Config)

```json
{
  "mcpServers": {
    "c3po": {
      "type": "http",
      "url": "http://nas:8420/mcp",
      "headers": {
        "X-Agent-ID": "${C3PO_AGENT_ID}"
      }
    }
  }
}
```

Or via plugin `.mcp.json`.

---

## Architecture Decisions

### Agent Identification

**Recommended: X-Agent-ID header**
- Simple, no auth complexity for home lab
- Can add Bearer token auth later if needed
- Header set via CC MCP config or environment variable

### Message Storage

**Recommended: Redis**
- Already planning to use it
- Native list operations (RPUSH/LPOP) for queues
- BLPOP for blocking wait
- Simple key structure: `inbox:{agent_id}`, `responses:{agent_id}`

### Collision Handling

When agent registers with a name that exists:
- Check `agents:online` hash
- If exists and seen recently (< 90s), suffix with `-2`, `-3`, etc.
- Store canonical name in response

---

## Installation

```bash
pip install fastmcp redis
```

---

## Sources

- [FastMCP GitHub](https://github.com/jlowin/fastmcp)
- [FastMCP Documentation](https://gofastmcp.com)
- [FastMCP Context](https://gofastmcp.com/servers/context)
- [FastMCP Authentication](https://gofastmcp.com/clients/auth/bearer)
