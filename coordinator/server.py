"""C3PO Coordinator - FastMCP server for multi-agent coordination."""

import asyncio
import logging
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

logger = logging.getLogger("c3po.server")

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
    """Extract agent identity from headers and auto-register.

    Constructs full agent_id from components:
    - X-Machine-Name: Machine/base identifier (required; falls back to X-Agent-ID)
    - X-Project-Name: Project name (optional, appended to agent_id)
    - X-Session-ID: Session identifier (for same-session detection)

    Full agent_id format: "{machine}/{project}" or just "{machine}" if no project.

    When project_name is missing (MCP calls from static config), we skip
    registration and rely on the PreToolUse hook's explicit agent_id parameter
    to provide the correct identity via _resolve_agent_id().
    """

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        headers = get_http_headers()
        # Prefer X-Machine-Name, fall back to X-Agent-ID for old configs
        machine_name = headers.get("x-machine-name") or headers.get("x-agent-id")
        if headers.get("x-agent-id") and not headers.get("x-machine-name"):
            logger.warning("deprecated_header: X-Agent-ID used instead of X-Machine-Name")
        project_name = headers.get("x-project-name")
        session_id = headers.get("x-session-id")

        logger.info(
            "middleware_headers machine_name=%s project_name=%s session_id=%s",
            machine_name, project_name, session_id,
        )

        if not machine_name:
            raise ToolError(
                "Missing X-Machine-Name header. "
                "Set this header to identify your machine."
            )

        # Construct full agent_id from components
        # Format: machine/project (e.g., "macbook/myproject")
        if project_name and project_name.strip():
            agent_id = f"{machine_name}/{project_name.strip()}"
            # Register/heartbeat with full identity
            registration = agent_manager.register_agent(agent_id, session_id)
            actual_agent_id = registration["id"]
        else:
            # No project name — can't construct full identity from headers alone.
            # Skip registration (the SessionStart hook already registered).
            # Store machine_name as placeholder; _resolve_agent_id() will prefer
            # the explicit agent_id parameter injected by the PreToolUse hook.
            logger.warning(
                "no_project_name machine_name=%s session_id=%s",
                machine_name, session_id,
            )
            actual_agent_id = machine_name  # placeholder

        # Store agent_id in context for tools to use
        context.fastmcp_context.set_state("agent_id", actual_agent_id)
        context.fastmcp_context.set_state("requested_agent_id", agent_id if project_name and project_name.strip() else machine_name)
        context.fastmcp_context.set_state("machine_name", machine_name)
        context.fastmcp_context.set_state("project_name", project_name)
        context.fastmcp_context.set_state("session_id", session_id)

        logger.debug("tool_call agent=%s tool=%s", actual_agent_id, getattr(context, 'tool_name', '?'))

        return await call_next(context)


# Create the MCP server
mcp = FastMCP(
    name="c3po",
    instructions=(
        "C3PO coordinates multiple Claude Code instances. "
        "Use list_agents to see available agents, send_request to communicate with them. "
        "Use get_messages to check for replies and incoming requests, "
        "or wait_for_message to block until a message arrives."
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


@mcp.custom_route("/api/register", methods=["POST"])
async def api_register(request):
    """Register an agent via REST API (used by hooks).

    Hooks can't use MCP (requires session handshake), so this provides
    the same registration functionality via a simple REST endpoint.

    Requires X-Machine-Name header (falls back to X-Agent-ID),
    optionally X-Project-Name and X-Session-ID.
    Returns the assigned agent_id (may differ from requested if collision resolved).
    """
    # Prefer X-Machine-Name, fall back to X-Agent-ID for old configs
    machine_name = request.headers.get("x-machine-name") or request.headers.get("x-agent-id")
    project_name = request.headers.get("x-project-name")
    session_id = request.headers.get("x-session-id")

    if not machine_name:
        return JSONResponse(
            {"error": "Missing X-Machine-Name header"},
            status_code=400,
        )

    # Construct full agent_id from components
    try:
        agent_id = _construct_agent_id(machine_name, project_name)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    # Validate agent_id format
    if not AGENT_ID_PATTERN.match(agent_id):
        return JSONResponse(
            {"error": "Invalid agent ID format"},
            status_code=400,
        )

    try:
        result = agent_manager.register_agent(agent_id, session_id)
        logger.info("rest_register agent_id=%s", result.get("id", agent_id))
        return JSONResponse(result)
    except Exception as e:
        return JSONResponse(
            {"error": str(e)},
            status_code=500,
        )


def _construct_agent_id(machine_name: str, project_name: Optional[str]) -> str:
    """Construct full agent_id from components.

    If project_name is provided, returns "machine_name/project_name".
    Otherwise returns machine_name as-is (caller already has a composite ID,
    e.g. from the session temp file).

    Args:
        machine_name: Machine identifier (may already include /project)
        project_name: Optional project name to append

    Returns:
        Full agent_id in format "machine/project"

    Raises:
        ValueError: If result would be a bare machine name (no slash)
    """
    if project_name and project_name.strip():
        return f"{machine_name}/{project_name.strip()}"
    if "/" not in machine_name:
        raise ValueError(
            f"Bare machine name '{machine_name}' is not a valid agent ID. "
            f"Provide X-Project-Name header or use a composite ID (machine/project)."
        )
    return machine_name


@mcp.custom_route("/api/pending", methods=["GET"])
async def api_pending(request):
    """Check pending requests for an agent without consuming them.

    Requires X-Agent-ID header, optionally X-Project-Name.
    Used by Stop hooks to check inbox.
    Does NOT consume messages - just peeks at the inbox.
    """
    base_id = request.headers.get("x-agent-id")
    project_name = request.headers.get("x-project-name")

    if not base_id:
        return JSONResponse(
            {"error": "Missing X-Agent-ID header"},
            status_code=400,
        )

    # Construct full agent_id from components
    try:
        agent_id = _construct_agent_id(base_id, project_name)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    # Validate agent_id format (same rules as MCP tools)
    if not AGENT_ID_PATTERN.match(agent_id):
        return JSONResponse(
            {"error": "Invalid agent ID format"},
            status_code=400,
        )

    try:
        requests = message_manager.peek_pending_requests(agent_id)
        logger.info("rest_pending agent_id=%s count=%d", agent_id, len(requests))
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

    Requires X-Agent-ID header, optionally X-Project-Name.
    Called by SessionEnd hook.
    Removes the agent from the registry so list_agents doesn't show stale entries.
    """
    base_id = request.headers.get("x-agent-id")
    project_name = request.headers.get("x-project-name")

    if not base_id:
        return JSONResponse(
            {"error": "Missing X-Agent-ID header"},
            status_code=400,
        )

    # Construct full agent_id from components
    try:
        agent_id = _construct_agent_id(base_id, project_name)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    # Validate agent_id format (same rules as MCP tools)
    if not AGENT_ID_PATTERN.match(agent_id):
        return JSONResponse(
            {"error": "Invalid agent ID format"},
            status_code=400,
        )

    try:
        removed = agent_manager.remove_agent(agent_id)
        logger.info("rest_unregister agent_id=%s removed=%s", agent_id, removed)
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
AGENT_ID_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_./-]{0,63}$")
MAX_MESSAGE_LENGTH = 50000  # 50KB max message size
MAX_WAIT_TIMEOUT = 3600  # 1 hour max


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
        logger.warning("send_rejected from=%s to=%s reason=rate_limited", from_agent, target)
        raise ToolError(f"{err.message} {err.suggestion}")

    # Check if target agent exists
    target_agent = agent_manager.get_agent(target)
    if target_agent is None:
        # Get list of available agents for helpful error
        available = agent_manager.list_agents()
        agent_ids = [a["id"] for a in available]
        err = agent_not_found(target, agent_ids)
        logger.warning("send_rejected from=%s to=%s reason=agent_not_found", from_agent, target)
        raise ToolError(f"{err.message} {err.suggestion}")

    # Record request for rate limiting
    msg_manager.record_request(from_agent)

    return msg_manager.send_request(from_agent, target, message, context)


def _get_messages_impl(
    msg_manager: MessageManager,
    agent_id: str,
    message_type: Optional[str] = None,
) -> list[dict]:
    """Get all pending messages (requests and/or responses) for an agent."""
    if message_type is not None and message_type not in ("request", "response"):
        err = invalid_request("type", "must be 'request', 'response', or omitted for both")
        raise ToolError(f"{err.message} {err.suggestion}")
    logger.info("get_messages agent=%s type=%s", agent_id, message_type)
    return msg_manager.get_messages(agent_id, message_type)


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


def _wait_for_message_impl(
    msg_manager: MessageManager,
    agent_id: str,
    timeout: int = 60,
    message_type: Optional[str] = None,
) -> dict:
    """Wait for any message (request or response) to arrive.

    Returns the messages directly, not a notification.
    """
    # Clamp timeout to valid range
    if timeout < 1:
        timeout = 1
    if timeout > MAX_WAIT_TIMEOUT:
        timeout = MAX_WAIT_TIMEOUT

    if message_type is not None and message_type not in ("request", "response"):
        err = invalid_request("type", "must be 'request', 'response', or omitted for both")
        raise ToolError(f"{err.message} {err.suggestion}")
    logger.info("wait_for_message agent=%s timeout=%d type=%s", agent_id, timeout, message_type)
    result = msg_manager.wait_for_message(agent_id, timeout, message_type)
    if result is None:
        return {
            "status": "timeout",
            "code": ErrorCodes.TIMEOUT,
            "message": f"No messages received within {timeout} seconds",
            "suggestion": "No agents have sent messages. You can continue with other work.",
        }
    return {"status": "received", "messages": result}


def _resolve_agent_id(ctx: Context, explicit_agent_id: Optional[str] = None) -> str:
    """Resolve the effective agent_id for a tool call.

    The agent_id must be provided explicitly (injected by the PreToolUse hook).
    Falls back to middleware header only if it contains a slash (full ID).
    Raises ToolError if no valid agent_id can be determined.

    Args:
        ctx: MCP context with state from middleware
        explicit_agent_id: Optional agent_id passed by Claude

    Returns:
        The effective agent_id to use

    Raises:
        ToolError: If no valid agent_id is available
    """
    if explicit_agent_id and explicit_agent_id.strip():
        resolved = explicit_agent_id.strip()
        logger.info("resolve_agent_id explicit=%s", resolved)
        return resolved

    # Middleware fallback — only accept full agent IDs (with slash)
    middleware_id = ctx.get_state("agent_id")
    if middleware_id and "/" in middleware_id:
        logger.warning("resolve_agent_id fallback_to_middleware=%s", middleware_id)
        return middleware_id

    # No valid agent_id — fail loudly
    logger.error(
        "resolve_agent_id_failed middleware_id=%s (missing slash — "
        "PreToolUse hook did not inject agent_id)",
        middleware_id,
    )
    raise ToolError(
        f"Could not determine your agent ID. The PreToolUse hook should inject "
        f"the agent_id parameter, but it didn't. Middleware only has base ID: "
        f"'{middleware_id}'. This usually means the ensure_agent_id hook is not "
        f"running or not finding the session file. Try restarting your session."
    )


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
    agent_id: Optional[str] = None,
) -> dict:
    """Send a request to another agent.

    Args:
        ctx: MCP context (injected automatically)
        target: The ID of the agent to send the request to
        message: The request message
        context: Optional context or background for the request
        agent_id: Your agent ID (from session start output). If not provided, uses header-based ID.

    Returns:
        Request data including id, status, and timestamp
    """
    from_agent = _resolve_agent_id(ctx, agent_id)
    return _send_request_impl(
        message_manager, agent_manager, from_agent, target, message, context
    )


@mcp.tool()
def get_messages(
    ctx: Context,
    type: Optional[str] = None,
    agent_id: Optional[str] = None,
) -> list[dict]:
    """Get all pending messages (requests and responses) for this agent.

    This consumes the messages - they will not be returned again.
    Returns both incoming requests (from other agents) and responses
    (to your previously sent requests) in a single unified list.
    Each message has a "type" field: "request" or "response".

    Args:
        ctx: MCP context (injected automatically)
        type: Optional filter - "request" for incoming requests only,
              "response" for responses only, or omit for both
        agent_id: Your agent ID (from session start output). If not provided, uses header-based ID.

    Returns:
        List of message dicts with type, id/request_id, from_agent, message/response, etc.
    """
    effective_id = _resolve_agent_id(ctx, agent_id)
    return _get_messages_impl(message_manager, effective_id, type)


@mcp.tool()
def respond_to_request(
    ctx: Context,
    request_id: str,
    response: str,
    status: str = "success",
    agent_id: Optional[str] = None,
) -> dict:
    """Respond to a request from another agent.

    Args:
        ctx: MCP context (injected automatically)
        request_id: The ID of the request to respond to
        response: Your response message
        status: Response status (default "success", can be "error" for failures)
        agent_id: Your agent ID (from session start output). If not provided, uses header-based ID.

    Returns:
        Response data including request_id, status, and timestamp
    """
    from_agent = _resolve_agent_id(ctx, agent_id)
    return _respond_to_request_impl(
        message_manager, from_agent, request_id, response, status
    )


@mcp.tool()
async def wait_for_message(
    ctx: Context,
    timeout: int = 60,
    type: Optional[str] = None,
    agent_id: Optional[str] = None,
) -> dict:
    """Wait for any message (request or response) to arrive.

    This is a blocking call - it will wait until a message arrives
    or the timeout is reached. Returns the messages directly.
    Use this instead of polling get_messages in a loop.

    Args:
        ctx: MCP context (injected automatically)
        timeout: Maximum seconds to wait (default 60, max 3600)
        type: Optional filter - "request" for incoming requests only,
              "response" for responses only, or omit for both
        agent_id: Your agent ID (from session start output). If not provided, uses header-based ID.

    Returns:
        Dict with status="received" and messages list, or timeout indicator
    """
    effective_id = _resolve_agent_id(ctx, agent_id)
    return await asyncio.to_thread(
        _wait_for_message_impl, message_manager, effective_id, timeout, type
    )


def main():
    """Run the coordinator server."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    port = int(os.environ.get("C3PO_PORT", "8420"))
    host = os.environ.get("C3PO_HOST", "0.0.0.0")

    logger.info("Starting C3PO coordinator on %s:%s", host, port)
    logger.info("Redis URL: %s", REDIS_URL)

    # Test Redis connection at startup with improved error message
    try:
        redis_client.ping()
        logger.info("Redis connection verified")
    except redis.ConnectionError as e:
        raise RedisConnectionError(REDIS_URL, e) from e
    except redis.RedisError as e:
        raise RedisConnectionError(REDIS_URL, e) from e

    mcp.run(transport="http", host=host, port=port)


if __name__ == "__main__":
    main()
