"""C3PO Coordinator - FastMCP server for multi-agent coordination."""

import os
import re
from datetime import datetime, timezone
from typing import Optional

import redis
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_headers
from fastmcp.server.middleware import Middleware, MiddlewareContext
from starlette.responses import JSONResponse

from coordinator.agents import AgentManager
from coordinator.errors import (
    agent_not_found,
    invalid_request,
    rate_limited,
    redis_unavailable,
    RedisConnectionError,
    ErrorCodes,
)
from coordinator.messaging import MessageManager


def create_redis_client(redis_url: str, test_connection: bool = False) -> redis.Redis:
    """Create Redis client with improved error handling.

    Args:
        redis_url: Redis connection URL
        test_connection: If True, test the connection immediately

    Returns:
        Redis client (connection tested if test_connection=True)

    Raises:
        RedisConnectionError: If connection test fails with actionable message
    """
    client = redis.from_url(redis_url, decode_responses=False)
    if test_connection:
        try:
            client.ping()
        except redis.ConnectionError as e:
            raise RedisConnectionError(redis_url, e) from e
        except redis.RedisError as e:
            raise RedisConnectionError(redis_url, e) from e
    return client


# Redis connection (lazy - connection tested on first use or at server start)
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")
redis_client = create_redis_client(REDIS_URL, test_connection=False)

# Agent manager
agent_manager = AgentManager(redis_client)

# Message manager
message_manager = MessageManager(redis_client)


class AgentIdentityMiddleware(Middleware):
    """Extract agent identity from X-Agent-ID header and auto-register."""

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        headers = get_http_headers()
        agent_id = headers.get("x-agent-id")
        session_id = headers.get("x-session-id")

        if not agent_id:
            raise ToolError(
                "Missing X-Agent-ID header. "
                "Set this header to identify your agent."
            )

        # Auto-register/update heartbeat on each tool call
        # This may return a different agent_id if collision was resolved
        registration = agent_manager.register_agent(agent_id, session_id)
        actual_agent_id = registration["id"]

        # Store actual agent_id in context for tools to use
        context.fastmcp_context.set_state("agent_id", actual_agent_id)
        context.fastmcp_context.set_state("requested_agent_id", agent_id)
        context.fastmcp_context.set_state("session_id", session_id)

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


# REST API endpoints for hooks (non-MCP access)
@mcp.custom_route("/api/health", methods=["GET"])
async def api_health(request):
    """Health check endpoint.

    Returns coordinator status and count of online agents.
    Used by hooks and monitoring systems.
    """
    try:
        online_count = agent_manager.count_online_agents()
        return JSONResponse({
            "status": "ok",
            "agents_online": online_count,
        })
    except Exception as e:
        return JSONResponse(
            {"status": "error", "error": str(e)},
            status_code=500,
        )


@mcp.custom_route("/api/pending", methods=["GET"])
async def api_pending(request):
    """Check pending requests for an agent without consuming them.

    Requires X-Agent-ID header. Used by Stop hooks to check inbox.
    Does NOT consume messages - just peeks at the inbox.
    """
    agent_id = request.headers.get("x-agent-id")
    if not agent_id:
        return JSONResponse(
            {"error": "Missing X-Agent-ID header"},
            status_code=400,
        )

    try:
        requests = message_manager.peek_pending_requests(agent_id)
        return JSONResponse({
            "count": len(requests),
            "requests": requests,
        })
    except Exception as e:
        return JSONResponse(
            {"error": str(e)},
            status_code=500,
        )


@mcp.custom_route("/api/unregister", methods=["POST"])
async def api_unregister(request):
    """Unregister an agent when it disconnects gracefully.

    Requires X-Agent-ID header. Called by SessionEnd hook.
    Removes the agent from the registry so list_agents doesn't show stale entries.
    """
    agent_id = request.headers.get("x-agent-id")
    if not agent_id:
        return JSONResponse(
            {"error": "Missing X-Agent-ID header"},
            status_code=400,
        )

    try:
        removed = agent_manager.remove_agent(agent_id)
        if removed:
            return JSONResponse({
                "status": "ok",
                "message": f"Agent '{agent_id}' unregistered",
            })
        else:
            return JSONResponse({
                "status": "ok",
                "message": f"Agent '{agent_id}' was not registered",
            })
    except Exception as e:
        return JSONResponse(
            {"error": str(e)},
            status_code=500,
        )


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
    session_id: Optional[str] = None,
    name: Optional[str] = None,
    capabilities: Optional[list[str]] = None,
) -> dict:
    """Register an agent with optional name and capabilities."""
    # Use provided name or agent_id as the identifier
    return manager.register_agent(agent_id, session_id, capabilities)


# Validation patterns
AGENT_ID_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$")
MAX_MESSAGE_LENGTH = 50000  # 50KB max message size


def _validate_agent_id(agent_id: str, field_name: str = "agent_id") -> None:
    """Validate an agent ID format.

    Args:
        agent_id: The ID to validate
        field_name: Name of the field for error messages

    Raises:
        ToolError: If validation fails
    """
    if not agent_id:
        err = invalid_request(field_name, "cannot be empty")
        raise ToolError(f"{err.message} {err.suggestion}")

    if not AGENT_ID_PATTERN.match(agent_id):
        err = invalid_request(
            field_name,
            "must be 1-64 characters, alphanumeric with _ . - (no leading special chars)"
        )
        raise ToolError(f"{err.message} {err.suggestion}")


def _validate_message(message: str) -> None:
    """Validate a message.

    Args:
        message: The message to validate

    Raises:
        ToolError: If validation fails
    """
    if not message or not message.strip():
        err = invalid_request("message", "cannot be empty")
        raise ToolError(f"{err.message} {err.suggestion}")

    if len(message) > MAX_MESSAGE_LENGTH:
        err = invalid_request(
            "message",
            f"exceeds maximum length of {MAX_MESSAGE_LENGTH} characters"
        )
        raise ToolError(f"{err.message} {err.suggestion}")


def _send_request_impl(
    msg_manager: MessageManager,
    agent_manager: AgentManager,
    from_agent: str,
    target: str,
    message: str,
    context: Optional[str] = None,
) -> dict:
    """Send a request to another agent."""
    # Validate inputs
    _validate_agent_id(target, "target")
    _validate_message(message)
    if context and len(context) > MAX_MESSAGE_LENGTH:
        err = invalid_request(
            "context",
            f"exceeds maximum length of {MAX_MESSAGE_LENGTH} characters"
        )
        raise ToolError(f"{err.message} {err.suggestion}")

    # Check rate limit
    is_allowed, current_count = msg_manager.check_rate_limit(from_agent)
    if not is_allowed:
        err = rate_limited(
            from_agent,
            msg_manager.RATE_LIMIT_REQUESTS,
            msg_manager.RATE_LIMIT_WINDOW_SECONDS
        )
        raise ToolError(f"{err.message} {err.suggestion}")

    # Check if target agent exists
    target_agent = agent_manager.get_agent(target)
    if target_agent is None:
        # Get list of available agents for helpful error
        available = agent_manager.list_agents()
        agent_ids = [a["id"] for a in available]
        err = agent_not_found(target, agent_ids)
        raise ToolError(f"{err.message} {err.suggestion}")

    # Record request for rate limiting
    msg_manager.record_request(from_agent)

    return msg_manager.send_request(from_agent, target, message, context)


def _get_pending_requests_impl(
    msg_manager: MessageManager,
    agent_id: str,
) -> list[dict]:
    """Get all pending requests for an agent (consumes them)."""
    return msg_manager.get_pending_requests(agent_id)


def _respond_to_request_impl(
    msg_manager: MessageManager,
    from_agent: str,
    request_id: str,
    response: str,
    status: str = "success",
) -> dict:
    """Send a response to a previous request."""
    # Validate response
    if not response or not response.strip():
        err = invalid_request("response", "cannot be empty")
        raise ToolError(f"{err.message} {err.suggestion}")

    if len(response) > MAX_MESSAGE_LENGTH:
        err = invalid_request(
            "response",
            f"exceeds maximum length of {MAX_MESSAGE_LENGTH} characters"
        )
        raise ToolError(f"{err.message} {err.suggestion}")

    # Validate request_id format
    if not request_id or "::" not in request_id:
        err = invalid_request(
            "request_id",
            "invalid format - should be from a previous request"
        )
        raise ToolError(f"{err.message} {err.suggestion}")

    return msg_manager.respond_to_request(request_id, from_agent, response, status)


def _wait_for_response_impl(
    msg_manager: MessageManager,
    agent_id: str,
    request_id: str,
    timeout: int = 60,
) -> dict:
    """Wait for a response to a specific request."""
    result = msg_manager.wait_for_response(agent_id, request_id, timeout)
    if result is None:
        return {
            "status": "timeout",
            "code": ErrorCodes.TIMEOUT,
            "request_id": request_id,
            "message": f"No response received within {timeout} seconds",
            "suggestion": "The target agent may be offline or busy. Check agent status with list_agents.",
        }
    return result


def _wait_for_request_impl(
    msg_manager: MessageManager,
    agent_id: str,
    timeout: int = 60,
) -> dict:
    """Wait for an incoming request (blocking)."""
    result = msg_manager.wait_for_request(agent_id, timeout)
    if result is None:
        return {
            "status": "timeout",
            "code": ErrorCodes.TIMEOUT,
            "message": f"No request received within {timeout} seconds",
            "suggestion": "No agents have sent requests. You can continue with other work.",
        }
    return result


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
    # Use requested_agent_id so explicit registration can retry collision resolution
    agent_id = ctx.get_state("requested_agent_id") or ctx.get_state("agent_id")
    session_id = ctx.get_state("session_id")
    return _register_agent_impl(agent_manager, agent_id, session_id, name, capabilities)


@mcp.tool()
def send_request(
    ctx: Context,
    target: str,
    message: str,
    context: Optional[str] = None,
) -> dict:
    """Send a request to another agent.

    Args:
        ctx: MCP context (injected automatically)
        target: The ID of the agent to send the request to
        message: The request message
        context: Optional context or background for the request

    Returns:
        Request data including id, status, and timestamp
    """
    from_agent = ctx.get_state("agent_id")
    return _send_request_impl(
        message_manager, agent_manager, from_agent, target, message, context
    )


@mcp.tool()
def get_pending_requests(ctx: Context) -> list[dict]:
    """Get all pending requests for this agent.

    This consumes the requests - they will not be returned again.
    Process each request and respond with respond_to_request.

    Args:
        ctx: MCP context (injected automatically)

    Returns:
        List of pending request dicts with id, from_agent, message, etc.
    """
    agent_id = ctx.get_state("agent_id")
    return _get_pending_requests_impl(message_manager, agent_id)


@mcp.tool()
def respond_to_request(
    ctx: Context,
    request_id: str,
    response: str,
    status: str = "success",
) -> dict:
    """Respond to a request from another agent.

    Args:
        ctx: MCP context (injected automatically)
        request_id: The ID of the request to respond to
        response: Your response message
        status: Response status (default "success", can be "error" for failures)

    Returns:
        Response data including request_id, status, and timestamp
    """
    from_agent = ctx.get_state("agent_id")
    return _respond_to_request_impl(
        message_manager, from_agent, request_id, response, status
    )


@mcp.tool()
def wait_for_response(
    ctx: Context,
    request_id: str,
    timeout: int = 60,
) -> dict:
    """Wait for a response to a previously sent request.

    This is a blocking call - it will wait until a response arrives
    or the timeout is reached.

    Args:
        ctx: MCP context (injected automatically)
        request_id: The ID of the request to wait for
        timeout: Maximum seconds to wait (default 60)

    Returns:
        Response data if received, or timeout indicator
    """
    agent_id = ctx.get_state("agent_id")
    return _wait_for_response_impl(message_manager, agent_id, request_id, timeout)


@mcp.tool()
def wait_for_request(
    ctx: Context,
    timeout: int = 60,
) -> dict:
    """Wait for an incoming request from another agent.

    This is a blocking call - it will wait until a request arrives
    in your inbox or the timeout is reached. This is an alternative
    to polling with get_pending_requests.

    Args:
        ctx: MCP context (injected automatically)
        timeout: Maximum seconds to wait (default 60)

    Returns:
        Request data if received, or timeout indicator
    """
    agent_id = ctx.get_state("agent_id")
    return _wait_for_request_impl(message_manager, agent_id, timeout)


def main():
    """Run the coordinator server."""
    port = int(os.environ.get("C3PO_PORT", "8420"))
    host = os.environ.get("C3PO_HOST", "0.0.0.0")

    print(f"Starting C3PO coordinator on {host}:{port}")
    print(f"Redis URL: {REDIS_URL}")

    # Test Redis connection at startup with improved error message
    try:
        redis_client.ping()
        print("Redis connection verified")
    except redis.ConnectionError as e:
        raise RedisConnectionError(REDIS_URL, e) from e
    except redis.RedisError as e:
        raise RedisConnectionError(REDIS_URL, e) from e

    mcp.run(transport="http", host=host, port=port)


if __name__ == "__main__":
    main()
