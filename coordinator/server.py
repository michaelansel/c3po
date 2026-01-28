"""C3PO Coordinator - FastMCP server for multi-agent coordination."""

import os
from datetime import datetime, timezone
from typing import Optional

import redis
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_headers
from fastmcp.server.middleware import Middleware, MiddlewareContext

from coordinator.agents import AgentManager

# Redis connection
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")
redis_client = redis.from_url(REDIS_URL, decode_responses=False)

# Agent manager
agent_manager = AgentManager(redis_client)


class AgentIdentityMiddleware(Middleware):
    """Extract agent identity from X-Agent-ID header and auto-register."""

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        headers = get_http_headers()
        agent_id = headers.get("x-agent-id")

        if not agent_id:
            raise ToolError(
                "Missing X-Agent-ID header. "
                "Set this header to identify your agent."
            )

        # Store agent_id in context for tools to use
        context.fastmcp_context.set_state("agent_id", agent_id)

        # Auto-register/update heartbeat on each tool call
        agent_manager.register_agent(agent_id)

        return await call_next(context)


# Create the MCP server
mcp = FastMCP(
    name="c3po",
    instructions=(
        "C3PO coordinates multiple Claude Code instances. "
        "Use list_agents to see available agents, send_request to communicate with them."
    ),
)
mcp.add_middleware(AgentIdentityMiddleware())


# Tool implementations (testable standalone functions)
def _ping_impl() -> dict:
    """Check coordinator health. Returns pong with timestamp."""
    return {
        "pong": True,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def _list_agents_impl(manager: AgentManager) -> list[dict]:
    """List all registered agents."""
    return manager.list_agents()


def _register_agent_impl(
    manager: AgentManager,
    agent_id: str,
    name: Optional[str] = None,
    capabilities: Optional[list[str]] = None,
) -> dict:
    """Register an agent with optional name and capabilities."""
    # Use provided name or agent_id as the identifier
    return manager.register_agent(agent_id, capabilities)


# Register tools with MCP server
@mcp.tool()
def ping() -> dict:
    """Check coordinator health. Returns pong with timestamp."""
    return _ping_impl()


@mcp.tool()
def list_agents() -> list[dict]:
    """List all registered agents with their status (online/offline)."""
    return _list_agents_impl(agent_manager)


@mcp.tool()
def register_agent(
    ctx: Context,
    name: Optional[str] = None,
    capabilities: Optional[list[str]] = None,
) -> dict:
    """Explicitly register this agent with optional capabilities.

    Args:
        ctx: MCP context (injected automatically)
        name: Optional display name (uses agent ID from header if not provided)
        capabilities: Optional list of capabilities this agent offers

    Returns:
        Agent registration data including id, capabilities, and timestamps
    """
    agent_id = ctx.get_state("agent_id")
    return _register_agent_impl(agent_manager, agent_id, name, capabilities)


def main():
    """Run the coordinator server."""
    port = int(os.environ.get("C3PO_PORT", "8420"))
    host = os.environ.get("C3PO_HOST", "0.0.0.0")

    print(f"Starting C3PO coordinator on {host}:{port}")
    print(f"Redis URL: {REDIS_URL}")
    mcp.run(transport="http", host=host, port=port)


if __name__ == "__main__":
    main()
