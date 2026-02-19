"""C3PO Coordinator - FastMCP server for multi-agent coordination."""

import asyncio
import concurrent.futures
import functools
import hmac
import hashlib
import json
import logging
import os
import re
import signal
import threading
import time
from datetime import datetime, timezone
from typing import Optional

import httpx
import redis
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_headers
from fastmcp.server.middleware import Middleware, MiddlewareContext
from starlette.responses import JSONResponse, Response

logger = logging.getLogger("c3po.server")

from coordinator.agents import AgentManager
from coordinator.audit import AuditLogger
from coordinator.auth import AuthManager
from coordinator.blobs import BlobManager, MAX_BLOB_SIZE
from coordinator.errors import (
    agent_not_found,
    anonymous_onboarding_required,
    blob_not_found,
    blob_too_large,
    invalid_request,
    rate_limited,
    RedisConnectionError,
    ErrorCodes,
)
from coordinator.messaging import MessageManager
from coordinator.rate_limit import RateLimiter


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

# Auth manager (with Redis for API key lookups)
auth_manager = AuthManager(redis_client)

# Rate limiter
rate_limiter = RateLimiter(redis_client)

# Audit logger
audit_logger = AuditLogger(redis_client)

# Blob manager
blob_manager = BlobManager(redis_client)

# Default asyncio thread pool is min(32, cpu_count+4) = only 5 on 1-CPU containers.
# wait_for_message blocks threads for up to 3600s, so it needs a dedicated larger pool.
_wait_pool = concurrent.futures.ThreadPoolExecutor(max_workers=50)

# Shutdown event: set on SIGTERM to gracefully drain wait_for_message calls.
_shutdown_event = threading.Event()

# Track active wait_for_message callers so the SIGTERM handler can wake them.
_active_waiters: set[str] = set()
_active_waiters_lock = threading.Lock()


def _fire_webhook(agent_id: str, webhook_url: str, webhook_secret: str) -> None:
    """Fire a webhook notification to an agent (fire-and-forget, non-blocking).

    Posts {"agent_id": "recipient-id"} with HMAC-SHA256 signature in X-C3PO-Signature header.
    Runs in background thread to avoid blocking message delivery.

    Args:
        agent_id: The recipient agent ID
        webhook_url: The webhook URL to POST to
        webhook_secret: The HMAC secret for signing the payload
    """
    def _post_webhook():
        try:
            body = json.dumps({"agent_id": agent_id}).encode("utf-8")
            signature = hmac.new(
                webhook_secret.encode("utf-8"),
                body,
                hashlib.sha256
            ).hexdigest()

            with httpx.Client(timeout=5.0) as client:
                client.post(
                    webhook_url,
                    content=body,
                    headers={
                        "Content-Type": "application/json",
                        "X-C3PO-Signature": signature,
                    }
                )
            logger.info("webhook_fired agent=%s url=%s", agent_id, webhook_url)
        except Exception as e:
            # Log but don't raise - webhook failure shouldn't affect message delivery
            logger.warning("webhook_failed agent=%s url=%s error=%s", agent_id, webhook_url, str(e))

    # Fire webhook in background thread (non-blocking)
    threading.Thread(target=_post_webhook, daemon=True).start()


def _determine_path_prefix(path: str) -> str:
    """Determine the auth path prefix from a request path.

    Returns one of: "/agent", "/oauth", "/admin", "/api", or "" for unknown.
    """
    if path.startswith("/agent/") or path.startswith("/agent"):
        return "/agent"
    elif path.startswith("/oauth/") or path.startswith("/oauth"):
        return "/oauth"
    elif path.startswith("/admin/") or path.startswith("/admin"):
        return "/admin"
    elif path.startswith("/api/health"):
        return "/api"
    else:
        return ""


def _authenticate_rest_request(request) -> dict:
    """Authenticate a REST API request.

    Returns auth_result dict from AuthManager.validate_request().
    """
    auth_header = request.headers.get("authorization", "")
    path_prefix = _determine_path_prefix(request.url.path)
    result = auth_manager.validate_request(auth_header, path_prefix)
    if result.get("valid"):
        audit_logger.auth_success(result.get("key_id", result.get("source", "")), result.get("agent_pattern", ""), source="rest")
    else:
        audit_logger.auth_failure(result.get("error", "unknown"), source="rest")
    return result


def _authenticate_mcp_headers(headers: dict, path_prefix: str = "/agent") -> dict:
    """Authenticate an MCP tool call from headers.

    Returns auth_result dict from AuthManager.validate_request().
    """
    auth_header = headers.get("authorization", "")
    result = auth_manager.validate_request(auth_header, path_prefix)
    if result.get("valid"):
        audit_logger.auth_success(result.get("key_id", result.get("source", "")), result.get("agent_pattern", ""), source="mcp")
    else:
        audit_logger.auth_failure(result.get("error", "unknown"), source="mcp")
    return result


class AgentIdentityMiddleware(Middleware):
    """Extract agent identity from headers and auto-register.

    Constructs full agent_id from components:
    - X-Machine-Name: Machine/base identifier (required)
    - X-Project-Name: Project name (optional, appended to agent_id)
    - X-Session-ID: Session identifier (for same-session detection)

    Full agent_id format: "{machine}/{project}" or just "{machine}" if no project.

    When project_name is missing (MCP calls from static config), we skip
    registration and rely on the PreToolUse hook's explicit agent_id parameter
    to provide the correct identity via _resolve_agent_id().

    For API key auth, enforces agent_pattern from the key metadata.

    Auth routing workaround (X-C3PO-Auth-Path header):
        MCP tool calls arrive at the coordinator on a single /mcp endpoint,
        losing the original URL path prefix that normally distinguishes
        /agent/* (API key auth) from /oauth/* (proxy token auth). Without
        the original path, the middleware can't determine which auth
        validator to use.

        To solve this, nginx injects an ``X-C3PO-Auth-Path`` header on
        /agent/* requests (value: "/agent"). The middleware reads this
        header to route to API key validation. When the header is absent
        (OAuth connections via mcp-auth-proxy, which bypass nginx and
        connect directly to the coordinator inside Docker), the middleware
        defaults to proxy token validation (/oauth).

        This is a routing hint only — not a security credential. Each
        auth validator independently verifies the token. Forging the header
        without a valid API key will still fail authentication.

        See also: nginx config in scripts/deploy.sh (proxy_set_header
        X-C3PO-Auth-Path) and the "Auth routing" section in CLAUDE.md.
    """

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        headers = get_http_headers()

        # --- Auth routing workaround ---
        # MCP tool calls lose the original URL path prefix after nginx
        # rewrites /agent/mcp → /mcp.  nginx injects X-C3PO-Auth-Path
        # to tell us which auth validator to use.  When the header is
        # absent (OAuth via mcp-auth-proxy, which connects directly to
        # the coordinator without nginx), default to /oauth.
        # See the class docstring above for the full explanation.
        auth_path = headers.get("x-c3po-auth-path", "")
        path_prefix = auth_path if auth_path in ("/agent", "/admin") else "/oauth"
        auth_result = _authenticate_mcp_headers(headers, path_prefix)
        if not auth_result.get("valid"):
            raise ToolError(
                f"Authentication failed: {auth_result.get('error', 'Invalid credentials')}. "
                f"Provide a valid Authorization header."
            )

        # Store auth info in context for pattern enforcement
        context.fastmcp_context.set_state("auth_source", auth_result.get("source", ""))
        context.fastmcp_context.set_state("auth_key_id", auth_result.get("key_id", ""))
        context.fastmcp_context.set_state("auth_agent_pattern", auth_result.get("agent_pattern", "*"))

        machine_name = headers.get("x-machine-name")
        project_name = headers.get("x-project-name")
        session_id = headers.get("x-session-id")

        logger.info(
            "middleware_headers machine_name=%s project_name=%s session_id=%s",
            machine_name, project_name, session_id,
        )

        if not machine_name:
            machine_name = "anonymous"
            logger.warning("no_machine_name: defaulting to 'anonymous' (client may not support custom headers)")

        # Construct full agent_id from components
        # Format: machine/project (e.g., "macbook/myproject")
        if project_name and project_name.strip():
            agent_id = f"{machine_name}/{project_name.strip()}"
            # Enforce agent_pattern from API key before registration
            agent_pattern = context.fastmcp_context.get_state("auth_agent_pattern") or "*"
            if agent_pattern != "*" and not AuthManager.validate_agent_pattern(agent_id, agent_pattern):
                raise ToolError(f"Agent ID '{agent_id}' does not match key pattern '{agent_pattern}'")
            # Suppress auto-registration for system/service projects (underscore prefix)
            if project_name.strip().startswith("_"):
                actual_agent_id = agent_id
                logger.info("system_project_skipped agent=%s (underscore prefix suppresses registration)", agent_id)
            else:
                # Register/heartbeat with full identity
                registration = agent_manager.register_agent(agent_id, session_id)
                actual_agent_id = registration["id"]
        elif machine_name == "anonymous":
            # No project name and no machine name — likely Claude Desktop/Claude.ai
            # which can't set custom headers or run hooks.
            # Set a placeholder; _resolve_agent_id() will check the explicit agent_id
            # parameter and require a UUID suffix (anonymous/chat-* pattern).
            agent_id = "anonymous"  # Placeholder (not a valid agent ID)
            actual_agent_id = "anonymous"  # Placeholder
            logger.info("anonymous_session session_id=%s (requires UUID suffix in agent_id parameter)", session_id)
        else:
            # Has machine name but no project name — Claude Code session where
            # the SessionStart hook already registered with full identity.
            # Store machine_name as placeholder; _resolve_agent_id() will prefer
            # the explicit agent_id parameter injected by the PreToolUse hook.
            logger.info(
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


# Proxy configuration
BEHIND_PROXY = os.environ.get("C3PO_BEHIND_PROXY", "").lower() in ("1", "true", "yes")


# Create the MCP server
mcp = FastMCP(
    name="c3po",
    instructions=(
        "C3PO coordinates multiple Claude Code instances. "
        "Use list_agents to see available agents, send_message to communicate with them. "
        "Use get_messages to check for replies and incoming messages, "
        "or wait_for_message to block until a message arrives. "
        "When you start a session, call set_description with a brief summary of what "
        "you can help with, so other agents know your capabilities.\n\n"
        "## Messaging workflow\n\n"
        "Messages use peek+ack semantics. get_messages and wait_for_message are "
        "non-destructive — messages stay in your inbox until you explicitly call "
        "ack_messages with their IDs. The typical flow is:\n\n"
        "1. Wait for messages: wait_for_message(timeout=300) — blocks until a message arrives\n"
        "2. Process: read the messages returned\n"
        "3. Reply: use reply(message_id=msg['id'], response='...') to respond to each message\n"
        "4. Acknowledge: ack_messages(message_ids=[msg['id']]) to remove them from your inbox\n\n"
        "If you skip step 4, the same messages will reappear on your next get_messages or "
        "wait_for_message call.\n\n"
        "Key distinctions:\n"
        "- reply() responds to a specific message (the sender gets it as a reply)\n"
        "- send_message() starts a new conversation thread\n"
        "- get_messages() returns immediately with whatever is pending (non-blocking)\n"
        "- wait_for_message() blocks until something arrives (use for long waits)\n"
        "- Messages have an 'id' field; replies have a 'reply_id' field — use the "
        "appropriate field when calling ack_messages\n\n"
        "## Anonymous sessions (Claude Desktop / Claude.ai)\n\n"
        "If you don't have a pre-configured agent ID, you must generate one and pass it "
        "as agent_id in every tool call. Run: python3 -c \"import uuid; print(uuid.uuid4())\" "
        "then use agent_id=\"anonymous/chat-<your-uuid>\" on all calls.\n\n"
        "## Blob transfers\n\n"
        "Use blobs to share files between agents. When to use blobs vs inline messages:\n"
        "- Inline in message: small text snippets under ~1KB composed in-memory\n"
        "- Blob: anything larger, binary content, or content from a file on disk\n\n"
        "Prefer scripts over MCP blob tools (token-efficient — content doesn't pass through context):\n"
        "- Upload: blob_id=$(c3po-upload /path/to/file)  then share the blob_id in a message\n"
        "- Download: c3po-download <blob_id> [output_path]\n"
        "- Find scripts: `which c3po-upload` OR ~/.claude/plugins/cache/michaelansel/c3po/*/scripts/\n\n"
        "Only use upload_blob/fetch_blob MCP tools when scripts are unavailable (set scripts_unavailable=True).\n"
        "fetch_blob returns inline content for blobs <=10KB; set inline_large=True for up to 100KB."
    ),
)
mcp.add_middleware(AgentIdentityMiddleware())


SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Cache-Control": "no-store",
}


def _get_client_ip(request) -> str:
    """Get client IP, respecting proxy headers when configured."""
    if BEHIND_PROXY:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
        real_ip = request.headers.get("x-real-ip", "")
        if real_ip:
            return real_ip
    return request.client.host if request.client else "unknown"


def SecureJSONResponse(content, status_code=200):
    """JSONResponse with security headers."""
    resp = JSONResponse(content, status_code=status_code)
    for k, v in SECURITY_HEADERS.items():
        resp.headers[k] = v
    return resp


def _check_rest_rate_limit(request, operation: str, identity: str) -> JSONResponse | None:
    """Check rate limit for a REST endpoint. Returns 429 response if exceeded, None if OK."""
    allowed, count = rate_limiter.check_and_record(operation, identity)
    if not allowed:
        return JSONResponse(
            {"error": "Rate limit exceeded", "code": ErrorCodes.RATE_LIMITED},
            status_code=429,
        )
    return None


# ============================================================
# Public endpoints (no auth)
# ============================================================

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


# ============================================================
# Agent endpoints (/agent/api/*) — API key auth
# ============================================================

@mcp.custom_route("/agent/api/register", methods=["POST"])
async def api_register(request):
    """Register an agent via REST API (used by hooks).

    Hooks can't use MCP (requires session handshake), so this provides
    the same registration functionality via a simple REST endpoint.

    Requires X-Machine-Name header, optionally X-Project-Name and X-Session-ID.
    Returns the assigned agent_id (may differ from requested if collision resolved).
    """
    auth_result = _authenticate_rest_request(request)
    if not auth_result.get("valid"):
        return JSONResponse(
            {"error": auth_result.get("error", "Authentication required")},
            status_code=401,
        )

    # Rate limit by client IP
    client_ip = _get_client_ip(request)
    rate_resp = _check_rest_rate_limit(request, "rest_register", client_ip)
    if rate_resp:
        return rate_resp

    machine_name = request.headers.get("x-machine-name")
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

    # Enforce agent_pattern from API key
    agent_pattern = auth_result.get("agent_pattern", "*")
    if not AuthManager.validate_agent_pattern(agent_id, agent_pattern):
        return JSONResponse(
            {"error": f"Agent ID '{agent_id}' does not match key pattern '{agent_pattern}'"},
            status_code=403,
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


@mcp.custom_route("/agent/api/pending", methods=["GET"])
async def api_pending(request):
    """Check pending messages for an agent without consuming them.

    Requires X-Machine-Name header (with optional X-Project-Name),
    or a composite machine/project in X-Machine-Name.
    Used by Stop hooks to check inbox.
    Does NOT consume messages - just peeks at the inbox.
    """
    auth_result = _authenticate_rest_request(request)
    if not auth_result.get("valid"):
        return JSONResponse(
            {"error": auth_result.get("error", "Authentication required")},
            status_code=401,
        )

    # Rate limit by client IP
    client_ip = _get_client_ip(request)
    rate_resp = _check_rest_rate_limit(request, "rest_pending", client_ip)
    if rate_resp:
        return rate_resp

    base_id = request.headers.get("x-machine-name")
    project_name = request.headers.get("x-project-name")

    if not base_id:
        return JSONResponse(
            {"error": "Missing X-Machine-Name header"},
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
        messages = message_manager.get_messages(agent_id)
        logger.info("rest_pending agent_id=%s count=%d", agent_id, len(messages))
        return JSONResponse({
            "count": len(messages),
            "messages": messages,
        })
    except Exception as e:
        return JSONResponse(
            {"error": str(e)},
            status_code=500,
        )


@mcp.custom_route("/agent/api/wait", methods=["GET"])
async def api_wait(request):
    """Long-poll for pending messages. Blocks until messages arrive or timeout.

    Serves external watcher processes (e.g. wait-for-trigger.py) that monitor an
    offline agent's inbox and wake the agent when messages arrive.

    **Does NOT update agent heartbeat** — callers are external watchers, not the
    agent itself. The agent correctly shows as offline while the watcher runs.
    Use the MCP wait_for_message tool if you need heartbeat updates.

    Auth: Bearer token + X-Machine-Name header (same as all /agent/* endpoints).

    Query params:
        timeout: seconds to wait (1-3600, default 30)

    Returns:
        {"count": N, "messages": [...], "status": "received"} when messages arrive
        {"count": 0, "status": "timeout"} on timeout
        {"count": 0, "status": "retry"} with Retry-After: 15 header on server shutdown
    """
    auth_result = _authenticate_rest_request(request)
    if not auth_result.get("valid"):
        return JSONResponse(
            {"error": auth_result.get("error", "Authentication required")},
            status_code=401,
        )

    # Rate limit by client IP
    client_ip = _get_client_ip(request)
    rate_resp = _check_rest_rate_limit(request, "rest_wait", client_ip)
    if rate_resp:
        return rate_resp

    base_id = request.headers.get("x-machine-name")
    project_name = request.headers.get("x-project-name")

    if not base_id:
        return JSONResponse(
            {"error": "Missing X-Machine-Name header"},
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

    # Clamp timeout: 1–3600
    try:
        timeout = int(request.query_params.get("timeout", "30"))
    except (ValueError, TypeError):
        timeout = 30
    timeout = min(3600, max(1, timeout))

    with _active_waiters_lock:
        _active_waiters.add(agent_id)
    try:
        # Run blocking wait in thread pool — no heartbeat_fn passed (intentional)
        result = await asyncio.get_running_loop().run_in_executor(
            _wait_pool,
            functools.partial(
                message_manager.wait_for_message,
                agent_id, timeout,
                heartbeat_fn=None,       # explicitly no heartbeat
                shutdown_event=_shutdown_event,
            ),
        )
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    finally:
        with _active_waiters_lock:
            _active_waiters.discard(agent_id)

    if result == "shutdown":
        return JSONResponse(
            {"count": 0, "status": "retry"},
            headers={"Retry-After": "15"},
        )
    if result is None:
        return JSONResponse({"count": 0, "status": "timeout"})
    return JSONResponse({"count": len(result), "messages": result, "status": "received"})


@mcp.custom_route("/agent/api/unregister", methods=["POST"])
async def api_unregister(request):
    """Unregister an agent when it disconnects gracefully.

    Requires X-Machine-Name header (with optional X-Project-Name).
    Called by SessionEnd hook.

    Behavior:
    - If ?keep=true is passed (or C3PO_KEEP_REGISTERED=1 env var is set in the hook),
      the agent entry is kept in registry but marked immediately offline. This supports
      the watcher pattern where an external process polls for messages on behalf of an
      offline agent.
    - If pending messages exist in the inbox, the agent is also kept and marked offline
      (even without ?keep=true). This preserves queued messages.
    - Otherwise, the agent is fully removed and all associated Redis keys are cleaned up.
    """
    auth_result = _authenticate_rest_request(request)
    if not auth_result.get("valid"):
        return JSONResponse(
            {"error": auth_result.get("error", "Authentication required")},
            status_code=401,
        )

    # Rate limit by client IP
    client_ip = _get_client_ip(request)
    rate_resp = _check_rest_rate_limit(request, "rest_unregister", client_ip)
    if rate_resp:
        return rate_resp

    base_id = request.headers.get("x-machine-name")
    project_name = request.headers.get("x-project-name")

    if not base_id:
        return JSONResponse(
            {"error": "Missing X-Machine-Name header"},
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
        keep = request.query_params.get("keep", "").lower() in ("1", "true", "yes")
        has_messages = message_manager.has_pending_messages(agent_id)

        if keep or has_messages:
            marked = agent_manager.mark_offline(agent_id)
            logger.info(
                "rest_unregister agent_id=%s kept=True has_messages=%s keep_param=%s marked=%s",
                agent_id, has_messages, keep, marked,
            )
            return JSONResponse({
                "status": "ok",
                "message": f"Agent '{agent_id}' marked offline and kept in registry",
                "pending_messages": has_messages,
                "kept": True,
            })
        else:
            removed = agent_manager.remove_agent(agent_id, cleanup_keys=True)
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


@mcp.custom_route("/agent/api/validate", methods=["GET"])
async def api_validate(request):
    """Validate an API token and optionally check agent_pattern compatibility.

    Used by entrypoints (e.g. claude-code-docker) to verify credentials
    before launching a session. Returns proper HTTP status codes unlike
    the MCP endpoint which always returns 200 at the transport level.

    Optional query param: machine_name — if provided, probes
    "{machine_name}/probe" against the token's agent_pattern.

    Returns:
        200: {"valid": true, "key_id": "...", "agent_pattern": "..."}
        401: invalid/missing token
        403: token valid but pattern doesn't match machine_name
        429: rate limited
    """
    auth_result = _authenticate_rest_request(request)
    if not auth_result.get("valid"):
        return JSONResponse(
            {"error": auth_result.get("error", "Authentication required")},
            status_code=401,
        )

    # Rate limit by client IP
    client_ip = _get_client_ip(request)
    rate_resp = _check_rest_rate_limit(request, "rest_validate", client_ip)
    if rate_resp:
        return rate_resp

    key_id = auth_result.get("key_id", "")
    agent_pattern = auth_result.get("agent_pattern", "*")

    # Optional: check if machine_name matches agent_pattern
    machine_name = request.query_params.get("machine_name", "").strip()
    if machine_name:
        probe_id = f"{machine_name}/probe"
        if agent_pattern != "*" and not AuthManager.validate_agent_pattern(probe_id, agent_pattern):
            return JSONResponse(
                {"error": f"Token pattern '{agent_pattern}' does not authorize '{machine_name}' agents"},
                status_code=403,
            )

    return SecureJSONResponse({
        "valid": True,
        "key_id": key_id,
        "agent_pattern": agent_pattern,
    })


# ============================================================
# Blob endpoints (/agent/api/blob*) — API key auth
# ============================================================

@mcp.custom_route("/agent/api/blob", methods=["POST"])
async def api_blob_upload(request):
    """Upload a blob via REST API.

    Accepts multipart form data (file field) or raw body.
    Returns blob metadata including blob_id.
    """
    auth_result = _authenticate_rest_request(request)
    if not auth_result.get("valid"):
        return JSONResponse(
            {"error": auth_result.get("error", "Authentication required")},
            status_code=401,
        )

    client_ip = _get_client_ip(request)
    rate_resp = _check_rest_rate_limit(request, "rest_blob_upload", client_ip)
    if rate_resp:
        return rate_resp

    content_type = request.headers.get("content-type", "")

    try:
        if "multipart/form-data" in content_type:
            form = await request.form()
            upload = form.get("file")
            if upload is None:
                return JSONResponse(
                    {"error": "Missing 'file' field in multipart form"},
                    status_code=400,
                )
            content = await upload.read()
            filename = form.get("filename", upload.filename or "upload")
            mime_type = form.get("mime_type", upload.content_type or "application/octet-stream")
        else:
            content = await request.body()
            filename = request.headers.get("x-filename", "upload")
            mime_type = request.headers.get("x-mime-type", content_type or "application/octet-stream")

        if not content:
            return JSONResponse({"error": "Empty content"}, status_code=400)

        uploader = request.headers.get("x-machine-name", "rest-upload")
        project = request.headers.get("x-project-name")
        if project:
            uploader = f"{uploader}/{project}"

        meta = blob_manager.store_blob(content, filename, mime_type, uploader)
        audit_logger.blob_upload(meta["blob_id"], filename, len(content), uploader, source="rest")
        return SecureJSONResponse(meta, status_code=201)

    except ValueError as e:
        err = blob_too_large(0, MAX_BLOB_SIZE)
        return JSONResponse(err.to_dict(), status_code=413)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/agent/api/blob/{blob_id}", methods=["GET"])
async def api_blob_download(request):
    """Download a blob via REST API.

    Returns raw content with Content-Type and Content-Disposition headers.
    """
    auth_result = _authenticate_rest_request(request)
    if not auth_result.get("valid"):
        return JSONResponse(
            {"error": auth_result.get("error", "Authentication required")},
            status_code=401,
        )

    client_ip = _get_client_ip(request)
    rate_resp = _check_rest_rate_limit(request, "rest_blob_download", client_ip)
    if rate_resp:
        return rate_resp

    blob_id = request.path_params.get("blob_id", "")
    if not blob_id:
        return JSONResponse({"error": "Missing blob_id"}, status_code=400)

    result = blob_manager.get_blob(blob_id)
    if result is None:
        err = blob_not_found(blob_id)
        return JSONResponse(err.to_dict(), status_code=404)

    content, metadata = result
    audit_logger.blob_download(blob_id, client_ip, source="rest")

    resp = Response(
        content=content,
        media_type=metadata.get("mime_type", "application/octet-stream"),
    )
    filename = metadata.get("filename", "download")
    resp.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    for k, v in SECURITY_HEADERS.items():
        if k != "Cache-Control":
            resp.headers[k] = v
    resp.headers["Cache-Control"] = "no-store"
    return resp


# ============================================================
# Admin endpoints (/admin/api/*) — admin key auth
# ============================================================

@mcp.custom_route("/admin/api/keys", methods=["POST"])
async def admin_create_key(request):
    """Create a new API key for agent authentication.

    Requires admin key authentication.
    Body JSON: {"agent_pattern": "macbook/*", "description": "My laptop"}
    Returns: {"key_id": "...", "api_key": "...", "agent_pattern": "...", "created_at": "..."}
    """
    auth_result = _authenticate_rest_request(request)
    if not auth_result.get("valid"):
        return JSONResponse(
            {"error": auth_result.get("error", "Authentication required")},
            status_code=401,
        )

    try:
        body = await request.json()
    except Exception:
        body = {}

    agent_pattern = body.get("agent_pattern", "*")
    description = body.get("description", "")

    try:
        result = auth_manager.create_api_key(agent_pattern=agent_pattern, description=description)
        audit_logger.admin_key_create(result["key_id"], agent_pattern)
        return SecureJSONResponse(result, status_code=201)
    except Exception as e:
        return JSONResponse(
            {"error": str(e)},
            status_code=500,
        )


@mcp.custom_route("/admin/api/keys", methods=["GET"])
async def admin_list_keys(request):
    """List all API keys (metadata only).

    Requires admin key authentication.
    Returns: {"keys": [...]}
    """
    auth_result = _authenticate_rest_request(request)
    if not auth_result.get("valid"):
        return JSONResponse(
            {"error": auth_result.get("error", "Authentication required")},
            status_code=401,
        )

    try:
        keys = auth_manager.list_api_keys()
        return SecureJSONResponse({"keys": keys})
    except Exception as e:
        return JSONResponse(
            {"error": str(e)},
            status_code=500,
        )


@mcp.custom_route("/admin/api/keys/{key_id}", methods=["DELETE"])
async def admin_revoke_key(request):
    """Revoke an API key.

    Requires admin key authentication.
    Returns: {"status": "ok"} or 404
    """
    auth_result = _authenticate_rest_request(request)
    if not auth_result.get("valid"):
        return JSONResponse(
            {"error": auth_result.get("error", "Authentication required")},
            status_code=401,
        )

    key_id = request.path_params.get("key_id", "")
    if not key_id:
        return JSONResponse({"error": "Missing key_id"}, status_code=400)

    try:
        revoked = auth_manager.revoke_api_key(key_id)
        if revoked:
            audit_logger.admin_key_revoke(key_id)
            return SecureJSONResponse({"status": "ok", "key_id": key_id})
        else:
            return JSONResponse({"error": f"Key '{key_id}' not found"}, status_code=404)
    except Exception as e:
        return JSONResponse(
            {"error": str(e)},
            status_code=500,
        )


@mcp.custom_route("/admin/api/audit", methods=["GET"])
async def api_admin_audit(request):
    """Query recent audit events. Requires admin key authentication."""
    auth_result = _authenticate_rest_request(request)
    if not auth_result.get("valid"):
        return JSONResponse(
            {"error": auth_result.get("error", "Authentication required")},
            status_code=401,
        )

    # Parse query params
    limit = min(int(request.query_params.get("limit", "100")), 1000)
    event_filter = request.query_params.get("event")

    entries = audit_logger.get_recent(limit=limit, event_filter=event_filter)
    return JSONResponse({"entries": entries, "count": len(entries)})


@mcp.custom_route("/admin/api/agents", methods=["GET"])
async def admin_list_agents(request):
    """List all agents with optional filtering by status and pattern.

    Requires admin key authentication.
    Query params:
        status: "online" or "offline" (optional)
        pattern: fnmatch glob pattern (optional, e.g. "stress/*")
    Returns: {"agents": [...], "count": N}
    """
    auth_result = _authenticate_rest_request(request)
    if not auth_result.get("valid"):
        return JSONResponse(
            {"error": auth_result.get("error", "Authentication required")},
            status_code=401,
        )

    # Rate limit by client IP
    client_ip = _get_client_ip(request)
    rate_resp = _check_rest_rate_limit(request, "admin_list_agents", client_ip)
    if rate_resp:
        return rate_resp

    agents = agent_manager.list_agents()

    # Filter by status
    status_filter = request.query_params.get("status", "").strip().lower()
    if status_filter:
        if status_filter not in ("online", "offline"):
            return JSONResponse(
                {"error": "Invalid status filter. Must be 'online' or 'offline'."},
                status_code=400,
            )
        agents = [a for a in agents if a.get("status") == status_filter]

    # Filter by pattern
    import fnmatch
    pattern = request.query_params.get("pattern", "").strip()
    if pattern:
        agents = [a for a in agents if fnmatch.fnmatch(a["id"], pattern)]

    return SecureJSONResponse({"agents": agents, "count": len(agents)})


@mcp.custom_route("/admin/api/agents", methods=["DELETE"])
async def admin_bulk_remove_agents(request):
    """Bulk-remove agents matching an fnmatch glob pattern and/or status filter.

    Requires admin key authentication.
    Query params:
        pattern: fnmatch glob (e.g. ?pattern=stress/*) — required unless status is set
        status: "offline" — when set, only removes agents with this status
    Returns: {"status": "ok", "pattern": "...", "removed": N, "agent_ids": [...]}
    """
    auth_result = _authenticate_rest_request(request)
    if not auth_result.get("valid"):
        return JSONResponse(
            {"error": auth_result.get("error", "Authentication required")},
            status_code=401,
        )

    # Rate limit by client IP
    client_ip = _get_client_ip(request)
    rate_resp = _check_rest_rate_limit(request, "admin_bulk_remove", client_ip)
    if rate_resp:
        return rate_resp

    pattern = request.query_params.get("pattern", "").strip()
    status_filter = request.query_params.get("status", "").strip().lower()

    if status_filter and status_filter not in ("online", "offline"):
        return JSONResponse(
            {"error": "Invalid status filter. Must be 'online' or 'offline'."},
            status_code=400,
        )

    if not pattern and not status_filter:
        return JSONResponse(
            {"error": "Missing required query parameter: pattern (or status)"},
            status_code=400,
        )

    # When status filter is provided, use list_agents + filter approach
    if status_filter:
        import fnmatch as fnmatch_mod
        agents = agent_manager.list_agents()
        # Filter by status
        agents = [a for a in agents if a.get("status") == status_filter]
        # Filter by pattern (default to * if not provided)
        effective_pattern = pattern or "*"
        agents = [a for a in agents if fnmatch_mod.fnmatch(a["id"], effective_pattern)]

        ids_to_remove = [a["id"] for a in agents]
        removed_ids = agent_manager.remove_agents_by_ids(ids_to_remove) if ids_to_remove else []
        audit_logger.admin_bulk_remove(effective_pattern, len(removed_ids), removed_ids)

        return SecureJSONResponse({
            "status": "ok",
            "pattern": effective_pattern,
            "status_filter": status_filter,
            "removed": len(removed_ids),
            "agent_ids": removed_ids,
        })

    # Without status filter: existing pattern-only behavior
    if not pattern:
        return JSONResponse(
            {"error": "Missing required query parameter: pattern"},
            status_code=400,
        )

    if pattern == "*":
        return JSONResponse(
            {"error": "Refusing to remove all agents. Use a more specific pattern, or add status=offline."},
            status_code=400,
        )

    removed_ids = agent_manager.remove_agents_by_pattern(pattern)
    audit_logger.admin_bulk_remove(pattern, len(removed_ids), removed_ids)

    return SecureJSONResponse({
        "status": "ok",
        "pattern": pattern,
        "removed": len(removed_ids),
        "agent_ids": removed_ids,
    })


# ============================================================
# Tool implementations (testable standalone functions)
# ============================================================

def _ping_impl() -> dict:
    """Check coordinator health. Returns pong with timestamp."""
    return {
        "pong": True,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def _strip_secrets(agent_data: dict) -> dict:
    """Strip webhook_secret from agent data before returning to callers."""
    agent_data.pop("webhook_secret", None)
    return agent_data


def _list_agents_impl(manager: AgentManager) -> list[dict]:
    """List all registered agents.

    `webhook_secret` is stripped from every agent dict before returning;
    webhook configuration is not visible to callers.

    Agent `status` ("online"/"offline") is computed dynamically from `last_seen`
    at call time using a 15-minute threshold — it is not stored in Redis.

    No heartbeat is updated and no rate limit is applied inside this function.
    """
    return [_strip_secrets(a) for a in manager.list_agents()]


def _register_agent_impl(
    manager: AgentManager,
    agent_id: str,
    session_id: Optional[str] = None,
    name: Optional[str] = None,
    capabilities: Optional[list[str]] = None,
) -> dict:
    """Register an agent with optional name and capabilities.

    Collision handling: if `agent_id` is already taken by a different, currently-online
    session (different `session_id` and `last_seen` within 15 minutes), the returned
    `id` will have a suffix added (`-2`, `-3`, etc.). Callers must inspect the returned
    `id` — it may differ from the requested `agent_id`.

    Same-session reconnect: if `session_id` matches the existing entry, only `last_seen`
    (and optionally `capabilities`) is updated; `registered_at` is not changed.

    Offline takeover: if the existing agent's `last_seen` is older than 15 minutes, the
    entry is fully overwritten (new `registered_at`, new `session_id`).

    `webhook_secret` is stripped from the returned dict.
    """
    return _strip_secrets(manager.register_agent(agent_id, session_id, capabilities))


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


def _validate_message_id(message_id: str, field_name: str = "message_id") -> None:
    """Validate message/reply ID format: from_agent::to_agent::uuid

    Args:
        message_id: The ID to validate
        field_name: Field name for error context

    Raises:
        ToolError: If validation fails
    """
    if not message_id:
        err = invalid_request(field_name, "must be a non-empty message/reply ID")
        raise ToolError(f"{err.message} {err.suggestion}")

    parts = message_id.split("::")
    if len(parts) != 3:
        err = invalid_request(
            field_name,
            "invalid format - must be in format 'from_agent::to_agent::uuid'"
        )
        raise ToolError(f"{err.message} {err.suggestion}")

    from_agent, to_agent, uuid_part = parts

    if not from_agent or not to_agent:
        err = invalid_request(
            field_name,
            "from_agent and to_agent must be non-empty"
        )
        raise ToolError(f"{err.message} {err.suggestion}")

    if len(from_agent) > 64 or len(to_agent) > 64:
        err = invalid_request(
            field_name,
            "agent IDs must be 64 characters or less"
        )
        raise ToolError(f"{err.message} {err.suggestion}")

    import re
    if not re.match(r"^[a-f0-9]{8}$", uuid_part):
        err = invalid_request(
            field_name,
            "UUID must be exactly 8 hex characters"
        )
        raise ToolError(f"{err.message} {err.suggestion}")


def _enforce_agent_pattern(ctx: Context, agent_id: str) -> None:
    """Enforce that agent_id matches the API key's agent_pattern.

    Args:
        ctx: MCP context with auth state
        agent_id: The agent ID being used

    Raises:
        ToolError: If agent_id doesn't match the pattern
    """
    pattern = ctx.get_state("auth_agent_pattern") or "*"
    if pattern != "*" and not AuthManager.validate_agent_pattern(agent_id, pattern):
        audit_logger.authorization_denied(agent_id, ctx.get_state("auth_key_id") or "", pattern)
        raise ToolError(
            f"Your API key (pattern: '{pattern}') does not authorize agent ID '{agent_id}'. "
            f"Use an agent ID that matches the pattern."
        )


def _set_description_impl(
    manager: AgentManager,
    agent_id: str,
    description: str,
) -> dict:
    """Set the description for an agent.

    Raises ToolError (not KeyError) if agent is not found; the error message
    includes a list of currently available agent IDs as a suggestion.

    Does not update the agent's heartbeat (`last_seen` is unchanged).
    `webhook_secret` is stripped from the returned dict.
    """
    try:
        return _strip_secrets(manager.set_description(agent_id, description))
    except KeyError:
        available = manager.list_agents()
        agent_ids = [a["id"] for a in available]
        err = agent_not_found(agent_id, agent_ids)
        raise ToolError(f"{err.message} {err.suggestion}")


def _register_webhook_impl(
    manager: AgentManager,
    agent_id: str,
    url: str,
    secret: str,
) -> dict:
    """Register webhook configuration for an agent.

    Validation (raises ToolError before touching Redis):
    - `url` must start with "http://" or "https://"
    - `secret` must be at least 16 characters

    Silently overwrites any previously configured webhook — no conflict check.
    URL reachability is not verified at registration time; the webhook is
    fire-and-forget with a 5-second HTTP timeout when messages arrive.

    Raises ToolError (with list of known agents) if `agent_id` is not found.
    `webhook_secret` is stripped from the returned dict.
    """
    if not url or not url.startswith(("http://", "https://")):
        err = invalid_request("url", "must be a valid HTTP(S) URL")
        raise ToolError(f"{err.message} {err.suggestion}")
    if not secret or len(secret) < 16:
        err = invalid_request("secret", "must be at least 16 characters")
        raise ToolError(f"{err.message} {err.suggestion}")
    try:
        return _strip_secrets(manager.set_webhook(agent_id, url, secret))
    except KeyError:
        available = manager.list_agents()
        agent_ids = [a["id"] for a in available]
        err = agent_not_found(agent_id, agent_ids)
        raise ToolError(f"{err.message} {err.suggestion}")


def _unregister_webhook_impl(
    manager: AgentManager,
    agent_id: str,
) -> dict:
    """Remove webhook configuration for an agent.

    Idempotent: clears `webhook_url` and `webhook_secret` to empty strings
    regardless of whether a webhook was previously registered.

    Raises ToolError (with list of known agents) if `agent_id` is not found.
    """
    try:
        return _strip_secrets(manager.clear_webhook(agent_id))
    except KeyError:
        available = manager.list_agents()
        agent_ids = [a["id"] for a in available]
        err = agent_not_found(agent_id, agent_ids)
        raise ToolError(f"{err.message} {err.suggestion}")


def _send_message_impl(
    msg_manager: MessageManager,
    agent_manager: AgentManager,
    from_agent: str,
    to: str,
    message: str,
    context: Optional[str] = None,
    deliver_offline: bool = False,
) -> dict:
    """Send a message to another agent.

    Delivery behavior:
    - Target registered (online or offline): queue unconditionally.
    - Target not registered, deliver_offline=True: create a placeholder and queue.
    - Target not registered, deliver_offline=False: raise ToolError with hint.

    Rate limiting: sliding-window limit keyed by `from_agent` (see RATE_LIMITS in
    rate_limit.py). Exceeding it raises ToolError before the message is queued.

    Offline indicator: if the target agent is registered but offline, the returned
    dict includes `offline_delivery=True` and a `note` string.

    Webhook: if the target agent has a webhook URL registered, a fire-and-forget
    HTTP POST is dispatched. Webhook failure never raises an exception to the caller.

    Size limits: `message` and `context` each capped at MAX_MESSAGE_LENGTH (50KB).
    Messages expire from Redis after 24 hours.
    """
    # Validate inputs
    _validate_agent_id(to, "to")
    _validate_message(message)
    if context and len(context) > MAX_MESSAGE_LENGTH:
        err = invalid_request(
            "context",
            f"exceeds maximum length of {MAX_MESSAGE_LENGTH} characters"
        )
        raise ToolError(f"{err.message} {err.suggestion}")

    # Check rate limit (uses central rate_limiter, not legacy msg_manager limits)
    allowed, count = rate_limiter.check_and_record("send_message", from_agent)
    if not allowed:
        from coordinator.rate_limit import RATE_LIMITS
        limit_config = RATE_LIMITS.get("send_message", (200, 60))
        err = rate_limited(from_agent, limit_config[0], limit_config[1])
        logger.warning("send_rejected from=%s to=%s reason=rate_limited", from_agent, to)
        raise ToolError(f"{err.message} {err.suggestion}")

    # Check if target agent exists
    resolved_target = agent_manager.get_agent(to)
    if resolved_target is None:
        if not deliver_offline:
            # Get list of available agents for helpful error
            available = agent_manager.list_agents()
            agent_ids = [a["id"] for a in available]
            err = agent_not_found(to, agent_ids)
            logger.warning("send_rejected from=%s to=%s reason=agent_not_found", from_agent, to)
            raise ToolError(
                f"{err.message} {err.suggestion} "
                f"Pass deliver_offline=True to queue for an unregistered agent."
            )
        # deliver_offline=True: create placeholder entry and queue message
        agent_manager.ensure_placeholder(to)
        resolved_target = agent_manager.get_agent(to)
        logger.info("send_offline_delivery from=%s to=%s (placeholder created)", from_agent, to)

    # Send message to Redis inbox
    result = msg_manager.send_message(from_agent, to, message, context)

    # Indicate offline delivery in result if target is offline
    is_offline = resolved_target.get("status") == "offline"
    if is_offline:
        result["offline_delivery"] = True
        result["note"] = f"Agent '{to}' is offline. Message queued for delivery when they reconnect."

    # Fire webhook if recipient has one registered
    webhook_url = resolved_target.get("webhook_url")
    webhook_secret = resolved_target.get("webhook_secret")
    if webhook_url and webhook_secret:
        _fire_webhook(to, webhook_url, webhook_secret)

    return result


def _get_messages_impl(
    msg_manager: MessageManager,
    agent_id: str,
) -> list[dict]:
    """Get all pending messages (incoming and replies) for an agent.

    Non-destructive: messages remain until acked. Repeated calls may
    return the same messages.

    Already-acked message IDs (from `c3po:acked:{agent_id}`) are filtered
    out before returning, so messages acked in a previous call won't reappear
    even before the next compaction. Expired messages (older than 24 hours)
    are also filtered at the peek layer.
    """
    logger.info("get_messages agent=%s", agent_id)
    return msg_manager.get_messages(agent_id)


def _reply_impl(
    msg_manager: MessageManager,
    from_agent: str,
    message_id: str,
    response: str,
    status: str = "success",
) -> dict:
    """Send a reply to a previous message.

    Authorization: only the original recipient of the message can reply.
    `from_agent` is compared against the `to_agent` embedded in `message_id`
    (`from_agent::to_agent::uuid` format). If they don't match, `MessageManager.reply`
    raises `ValueError`, which propagates as a tool error.

    Routing: the reply is delivered to the original sender's inbox
    (`c3po:messages:{original_sender}`), not to `from_agent`. A notification
    signal is also pushed to `c3po:notify:{original_sender}` to wake any
    blocked `wait_for_message` call on the original sender.

    Return value: the dict contains `id` (the reply's unique ID, used for acking),
    `reply_to` (the original message ID), `from_agent`, and `to_agent` (original sender).

    Note: webhook notifications for replies are fired in the MCP-level `reply` tool
    wrapper, not here. REST callers that invoke this function directly do not trigger
    webhook delivery.

    `response` must be non-empty and under MAX_MESSAGE_LENGTH (50KB).
    `message_id` format is validated before calling MessageManager.
    """
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

    # Validate message_id format
    _validate_message_id(message_id)

    return msg_manager.reply(message_id, from_agent, response, status)


def _wait_for_message_impl(
    msg_manager: MessageManager,
    agent_id: str,
    timeout: int = 60,
    heartbeat_fn: Optional[callable] = None,
    shutdown_event: Optional[threading.Event] = None,
) -> dict:
    """Wait for any message (incoming or reply) to arrive.

    Returns a dict (not a list) with one of three shapes depending on outcome:

    - Messages arrived:
        {"status": "received", "messages": [...], "elapsed_seconds": N}
    - Timeout with no messages:
        {"status": "timeout", "code": ErrorCodes.TIMEOUT, "message": "...",
         "suggestion": "...", "elapsed_seconds": N}
    - Server shutdown (SIGTERM received):
        {"status": "retry", "message": "...", "retry_after": 15, "elapsed_seconds": N}
        Callers must detect status="retry" and call wait_for_message again after 15s.

    Immediate return: if messages are already pending in the inbox, returns them
    immediately without blocking on BLPOP.

    Heartbeat: `heartbeat_fn` (when provided) is called on each BLPOP cycle (every
    ≤10s), keeping the agent "online" during a long wait. The REST `/api/wait`
    endpoint intentionally passes `heartbeat_fn=None` so watchers don't update
    the heartbeat. The MCP `wait_for_message` tool passes a heartbeat updater.

    Shutdown behavior: when `_shutdown_event` is set by the SIGTERM handler,
    `MessageManager.wait_for_message` returns the sentinel string "shutdown",
    which this function translates to status="retry".

    Thread pool: runs in `_wait_pool` (50-thread ThreadPoolExecutor) because BLPOP
    can block a thread for up to 3600 seconds. Active waiters are tracked in
    `_active_waiters` so the SIGTERM handler can push shutdown signals to all of them.

    Timeout is clamped to the valid range (1-3600 seconds) if out of bounds.
    """
    # Clamp timeout to valid range
    if timeout < 1:
        timeout = 1
    if timeout > MAX_WAIT_TIMEOUT:
        timeout = MAX_WAIT_TIMEOUT

    logger.info("wait_for_message_impl started agent=%s timeout=%d", agent_id, timeout)

    start_time = time.monotonic()
    with _active_waiters_lock:
        _active_waiters.add(agent_id)
    try:
        result = msg_manager.wait_for_message(
            agent_id, timeout,
            heartbeat_fn=heartbeat_fn,
            shutdown_event=shutdown_event,
        )
    except BaseException as e:
        # Catch thread termination (e.g., when thread is killed by SIGTERM)
        # This logs the exception but doesn't break the function
        logger.error("wait_for_message_impl_terminated agent=%s exception_type=%s exception=%s",
                     agent_id, type(e).__name__, e)
        raise  # Re-raise so the async wrapper catches it
    finally:
        elapsed = time.monotonic() - start_time
        with _active_waiters_lock:
            _active_waiters.discard(agent_id)
    if result == "shutdown":
        logger.info("wait_for_message_done agent=%s result=shutdown elapsed=%.1fs", agent_id, elapsed)
        return {
            "status": "retry",
            "message": "Server is restarting. Please call wait_for_message again in 15 seconds.",
            "retry_after": 15,
            "elapsed_seconds": round(elapsed, 1),
        }
    if result is None:
        logger.info("wait_for_message_done agent=%s result=timeout elapsed=%.1fs", agent_id, elapsed)
        return {
            "status": "timeout",
            "code": ErrorCodes.TIMEOUT,
            "message": f"No messages received within {timeout} seconds",
            "suggestion": "No agents have sent messages. You can continue with other work.",
            "elapsed_seconds": round(elapsed, 1),
        }
    logger.info("wait_for_message_done agent=%s result=received count=%d elapsed=%.1fs",
                agent_id, len(result), elapsed)
    return {"status": "received", "messages": result, "elapsed_seconds": round(elapsed, 1)}


def _resolve_agent_id(ctx: Context, explicit_agent_id: Optional[str] = None) -> str:
    """Resolve the effective agent_id for a tool call.

    The agent_id must be provided explicitly (injected by the PreToolUse hook).
    Falls back to middleware header only if it contains a slash (full ID).
    Raises ToolError if no valid agent_id can be determined.

    Special handling for anonymous sessions:
    - Rejects bare "anonymous/chat" (requires UUID suffix)
    - Accepts "anonymous/chat-*" pattern (e.g., "anonymous/chat-a1b2c3d4")
    - Auto-registers anonymous/chat-* agents on first call (they can't use
      the SessionStart hook because they lack custom headers).

    Heartbeat side effect: calls `agent_manager.touch_heartbeat(resolved)` on every
    successful resolution. This keeps the agent "online" across all tool calls, not
    just `wait_for_message` (which has its own internal heartbeat loop).

    Args:
        ctx: MCP context with state from middleware
        explicit_agent_id: Optional agent_id passed by Claude

    Returns:
        The effective agent_id to use

    Raises:
        ToolError: If no valid agent_id is available or anonymous onboarding required
    """
    if explicit_agent_id and explicit_agent_id.strip():
        resolved = explicit_agent_id.strip()

        # Check for bare anonymous/chat (reject with onboarding instructions)
        if resolved == "anonymous/chat":
            err = anonymous_onboarding_required()
            logger.warning("anonymous_onboarding_required session_id=%s", ctx.get_state("session_id"))
            raise ToolError(f"{err.message}\n\n{err.suggestion}")

        # Register anonymous/chat-* agents on first use
        # (they can't use the SessionStart hook because they lack headers)
        if resolved.startswith("anonymous/chat-"):
            session_id = ctx.get_state("session_id")
            registration = agent_manager.register_agent(resolved, session_id)
            logger.info("anonymous_agent_registered agent_id=%s", registration["id"])

        logger.debug("resolve_agent_id explicit=%s", resolved)
    elif (middleware_id := ctx.get_state("agent_id")) and "/" in middleware_id:
        # Middleware fallback — only accept full agent IDs (with slash)
        logger.warning("resolve_agent_id fallback_to_middleware=%s", middleware_id)
        resolved = middleware_id
    else:
        # No valid agent_id — fail loudly
        middleware_id = ctx.get_state("agent_id")

        # Special case: anonymous session without explicit agent_id
        # (middleware set placeholder "anonymous")
        if middleware_id == "anonymous":
            err = anonymous_onboarding_required()
            logger.warning("anonymous_onboarding_required_no_explicit_id session_id=%s", ctx.get_state("session_id"))
            raise ToolError(f"{err.message}\n\n{err.suggestion}")

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

    # Refresh heartbeat so the agent stays "online" across all tool calls,
    # not just wait_for_message (which has its own heartbeat loop).
    agent_manager.touch_heartbeat(resolved)

    return resolved


# ============================================================
# MCP Tools
# ============================================================
# NOTE: When adding a new tool, also update:
#   - hooks/hooks.json  (PreToolUse matcher list)
#   - hooks/ensure_agent_id.py  (TOOLS_NEEDING_AGENT_ID, if it uses agent_id)

@mcp.tool()
def ping() -> dict:
    """Check coordinator health. Returns pong with timestamp."""
    return _ping_impl()


@mcp.tool()
def list_agents(ctx: Context) -> list[dict]:
    """List all registered agents with their status (online/offline)."""
    # Rate limit by agent_id (or machine_name as fallback)
    identity = ctx.get_state("agent_id") or ctx.get_state("machine_name") or "unknown"
    allowed, _ = rate_limiter.check_and_record("list_agents", identity)
    if not allowed:
        err = rate_limited(identity, 30, 60)
        raise ToolError(f"{err.message} {err.suggestion}")
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
    _enforce_agent_pattern(ctx, agent_id)
    session_id = ctx.get_state("session_id")
    return _register_agent_impl(agent_manager, agent_id, session_id, name, capabilities)


@mcp.tool()
def set_description(
    ctx: Context,
    description: str,
    agent_id: Optional[str] = None,
) -> dict:
    """Set a description for this agent so others know what it does.

    Args:
        ctx: MCP context (injected automatically)
        description: A short description of what this agent does
        agent_id: Your agent ID (from session start output). If not provided, uses header-based ID.

    Returns:
        Updated agent data including the description
    """
    effective_id = _resolve_agent_id(ctx, agent_id)
    _enforce_agent_pattern(ctx, effective_id)
    return _set_description_impl(agent_manager, effective_id, description)


@mcp.tool()
def register_webhook(
    ctx: Context,
    url: str,
    secret: str,
    agent_id: Optional[str] = None,
) -> dict:
    """Register a webhook for instant message notifications.

    When a message or reply arrives for this agent, the coordinator will POST
    to the webhook URL with HMAC-SHA256 signature for verification.

    POST body: {"agent_id": "recipient-id"}
    Header: X-C3PO-Signature: <hex-encoded HMAC-SHA256(secret, body_bytes)>

    The webhook is a wake-up notification only. Use get_messages + ack_messages
    to retrieve actual message content.

    Args:
        ctx: MCP context (injected automatically)
        url: The webhook URL to POST to (must be http:// or https://)
        secret: The HMAC secret for signing payloads (min 16 characters)
        agent_id: Your agent ID (from session start output). If not provided, uses header-based ID.

    Returns:
        Updated agent data including webhook configuration
    """
    effective_id = _resolve_agent_id(ctx, agent_id)
    _enforce_agent_pattern(ctx, effective_id)

    allowed, _ = rate_limiter.check_and_record("register_webhook", effective_id)
    if not allowed:
        err = rate_limited(effective_id, 5, 60)
        raise ToolError(f"{err.message} {err.suggestion}")

    return _register_webhook_impl(agent_manager, effective_id, url, secret)


@mcp.tool()
def unregister_webhook(
    ctx: Context,
    agent_id: Optional[str] = None,
) -> dict:
    """Remove the webhook registration for this agent.

    After unregistering, message delivery notifications will no longer
    be sent to the webhook URL. Messages can still be retrieved via
    get_messages and wait_for_message.

    Args:
        ctx: MCP context (injected automatically)
        agent_id: Your agent ID (from session start output). If not provided, uses header-based ID.

    Returns:
        Updated agent data confirming webhook removal
    """
    effective_id = _resolve_agent_id(ctx, agent_id)
    _enforce_agent_pattern(ctx, effective_id)
    return _unregister_webhook_impl(agent_manager, effective_id)


@mcp.tool()
def send_message(
    ctx: Context,
    to: str,
    message: str,
    context: Optional[str] = None,
    agent_id: Optional[str] = None,
    deliver_offline: bool = False,
) -> dict:
    """Send a message to another agent.

    Args:
        ctx: MCP context (injected automatically)
        to: The ID of the agent to send the message to
        message: The message content
        context: Optional context or background for the message
        agent_id: Your agent ID (from session start output). If not provided, uses header-based ID.
        deliver_offline: If True, queue message even if target agent is not registered.
            A placeholder entry will be created so the agent can find its messages on reconnect.

    Returns:
        Message data including id, status, and timestamp.
        If offline delivery, includes offline_delivery=True and a note.
    """
    from_agent = _resolve_agent_id(ctx, agent_id)
    _enforce_agent_pattern(ctx, from_agent)
    return _send_message_impl(
        message_manager, agent_manager, from_agent, to=to, message=message,
        context=context, deliver_offline=deliver_offline,
    )


@mcp.tool()
def get_messages(
    ctx: Context,
    agent_id: Optional[str] = None,
) -> list[dict]:
    """Get all pending messages (incoming messages and replies) for this agent.

    Messages are NOT consumed. Call ack_messages with the message IDs to
    remove them. Repeated calls may return the same messages until acked.

    Args:
        ctx: MCP context (injected automatically)
        agent_id: Your agent ID (from session start output). If not provided, uses header-based ID.

    Returns:
        List of message dicts with id/message_id, from_agent, to_agent, message/response, etc.
    """
    effective_id = _resolve_agent_id(ctx, agent_id)
    _enforce_agent_pattern(ctx, effective_id)
    return _get_messages_impl(message_manager, effective_id)


@mcp.tool()
def reply(
    ctx: Context,
    message_id: str,
    response: str,
    status: str = "success",
    agent_id: Optional[str] = None,
) -> dict:
    """Reply to a message from another agent.

    Args:
        ctx: MCP context (injected automatically)
        message_id: Message/reply ID to reply to (format: from_agent::to_agent::uuid)
        response: Your reply message
        status: Reply status (default "success", can be "error" for failures)
        agent_id: Your agent ID (from session start output). If not provided, uses header-based ID.

    Returns:
        Reply data including message_id, from_agent, to_agent, response, status, and timestamp

    Raises:
        ToolError: If message_id format is invalid or response is empty
    """
    from_agent = _resolve_agent_id(ctx, agent_id)
    _enforce_agent_pattern(ctx, from_agent)
    result = _reply_impl(
        message_manager, from_agent, message_id, response, status
    )

    # Fire webhook if reply recipient has one registered
    to_agent = result.get("to_agent")
    if to_agent:
        target_agent = agent_manager.get_agent(to_agent)
        if target_agent:
            webhook_url = target_agent.get("webhook_url")
            webhook_secret = target_agent.get("webhook_secret")
            if webhook_url and webhook_secret:
                _fire_webhook(to_agent, webhook_url, webhook_secret)

    return result


@mcp.tool()
async def wait_for_message(
    ctx: Context,
    timeout: int = 60,
    agent_id: Optional[str] = None,
) -> dict:
    """Wait for any message (incoming message or reply) to arrive.

    This is a blocking call - it will wait until a message arrives
    or the timeout is reached. Returns the messages directly.
    Use this instead of polling get_messages in a loop.

    Messages are NOT consumed. Call ack_messages with the message IDs to
    remove them. Repeated calls may return the same messages until acked.

    Args:
        ctx: MCP context (injected automatically)
        timeout: Maximum seconds to wait (1-3600, default 60)
        agent_id: Your agent ID (from session start output). If not provided, uses header-based ID.

    Returns:
        Dict with status="received" and messages list, or timeout indicator

    Raises:
        ToolError: If timeout is out of valid range (1-3600 seconds)
    """
    effective_id = _resolve_agent_id(ctx, agent_id)
    _enforce_agent_pattern(ctx, effective_id)

    with _active_waiters_lock:
        waiter_count = len(_active_waiters)
    logger.info("wait_for_message_start agent=%s timeout=%d waiters=%d", effective_id, timeout, waiter_count)

    try:
        result = await asyncio.get_running_loop().run_in_executor(
            _wait_pool,
            functools.partial(
                _wait_for_message_impl, message_manager, effective_id, timeout,
                heartbeat_fn=lambda: agent_manager.touch_heartbeat(effective_id),
                shutdown_event=_shutdown_event,
            ),
        )
        logger.info("wait_for_message_returned agent=%s status=%s", effective_id, result.get("status"))
        return result
    except asyncio.CancelledError:
        logger.warning("wait_for_message_cancelled agent=%s (asyncio CancelledError)", effective_id)
        return {
            "status": "retry",
            "message": "Server is restarting. Please call wait_for_message again in 15 seconds.",
            "retry_after": 15,
        }
    except BaseException as e:
        # Catch thread termination exceptions (e.g., when thread is killed by SIGTERM)
        # These are not caught by the current exception handler
        logger.error("wait_for_message_thread_terminated agent=%s exception_type=%s", effective_id, type(e).__name__)
        return {
            "status": "error",
            "code": ErrorCodes.INTERNAL_ERROR,
            "message": "Session terminated while waiting for messages. Please call wait_for_message again.",
            "retry_after": 15,
        }


def _ack_messages_impl(
    msg_manager: MessageManager,
    agent_id: str,
    message_ids: list[str],
) -> dict:
    """Acknowledge messages so they no longer appear in get_messages/wait_for_message.

    Empty list is a no-op: returns {"acked": 0, "compacted": False} immediately.

    All-or-nothing validation: all `message_ids` are format-validated before any
    Redis writes. If any are invalid, a ToolError is raised listing the first five
    invalid IDs, and no messages are acked.

    Compaction: when the agent's acked set (`c3po:acked:{agent_id}`) exceeds 20
    entries (COMPACT_THRESHOLD), the acked entries are atomically removed from the
    underlying Redis list via a Lua script, and the acked set is cleared. The
    returned `compacted` field indicates whether this occurred.

    The acked set TTL is reset to 24 hours on each call.
    """
    if not message_ids:
        return {"acked": 0, "compacted": False}

    # Validate each message_id
    invalid_ids = []
    for msg_id in message_ids:
        try:
            _validate_message_id(msg_id)
        except ToolError:
            invalid_ids.append(msg_id)

    if invalid_ids:
        err = invalid_request(
            "message_ids",
            f"contains {len(invalid_ids)} invalid ID(s): {', '.join(invalid_ids[:5])}"
        )
        raise ToolError(f"{err.message} {err.suggestion}")

    return msg_manager.ack_messages(agent_id, message_ids)


INLINE_BLOB_THRESHOLD = 10 * 1024   # 10KB: "protect you from yourself" limit; use c3po-download for larger blobs
HARD_BLOB_THRESHOLD = 100 * 1024    # 100KB: absolute cap, even with inline_large=True


def _upload_blob_impl(
    blob_mgr: BlobManager,
    content: bytes,
    filename: str,
    mime_type: str = "application/octet-stream",
    uploader: str = "",
) -> dict:
    """Store a blob and return metadata.

    Raises ToolError if `content` exceeds MAX_BLOB_SIZE (defined in blobs.py).
    The returned metadata does not include the content itself; callers retrieve
    it separately via `_fetch_blob_impl`.

    Audit logging is the caller's responsibility (done in the MCP tool wrapper,
    not here).
    """
    if len(content) > MAX_BLOB_SIZE:
        err = blob_too_large(len(content), MAX_BLOB_SIZE)
        raise ToolError(f"{err.message} {err.suggestion}")

    return blob_mgr.store_blob(content, filename, mime_type, uploader)


def _fetch_blob_impl(
    blob_mgr: BlobManager,
    blob_id: str,
    coordinator_url: str = "",
    inline_large: bool = False,
) -> dict:
    """Fetch a blob by ID. Returns inline content for small blobs, metadata for large.

    Three-tier size logic:
    - size <= INLINE_BLOB_THRESHOLD (10KB): always returned inline
    - INLINE_BLOB_THRESHOLD < size <= HARD_BLOB_THRESHOLD (100KB) and inline_large=True:
      returned inline (caller explicitly opted in)
    - size > HARD_BLOB_THRESHOLD OR (size > INLINE_BLOB_THRESHOLD and inline_large=False):
      returns metadata + download_url + actionable note (hard cap, inline_large ignored above 100KB)

    Encoding of inline content: UTF-8 decode is attempted first; on UnicodeDecodeError,
    content is base64-encoded. The `encoding` field in the returned dict is either
    "utf-8" or "base64" so the caller knows how to interpret `content`.

    Large blob `download_url`: absolute URL if `C3PO_COORDINATOR_URL` env var is set;
    otherwise a relative path `/agent/api/blob/{blob_id}`. The `note` field in the
    non-inline response gives an actionable `c3po-download` command with the exact blob_id.

    Raises ToolError if `blob_id` is not found.
    Audit logging is the caller's responsibility (done in the MCP tool wrapper).
    """
    result = blob_mgr.get_blob(blob_id)
    if result is None:
        err = blob_not_found(blob_id)
        raise ToolError(f"{err.message} {err.suggestion}")

    content, metadata = result
    size = len(content)

    # Determine whether to return inline
    return_inline = (
        size <= INLINE_BLOB_THRESHOLD
        or (inline_large and size <= HARD_BLOB_THRESHOLD)
    )

    if return_inline:
        try:
            text = content.decode("utf-8")
            return {**metadata, "content": text, "encoding": "utf-8"}
        except UnicodeDecodeError:
            import base64
            b64 = base64.b64encode(content).decode("ascii")
            return {**metadata, "content": b64, "encoding": "base64"}
    else:
        # Redirect to download script with actionable note
        download_url = f"{coordinator_url}/agent/api/blob/{blob_id}" if coordinator_url else f"/agent/api/blob/{blob_id}"
        size_kb = size // 1024
        if size > HARD_BLOB_THRESHOLD:
            reason = f"Blob is {size_kb}KB — exceeds hard cap of {HARD_BLOB_THRESHOLD // 1024}KB (inline_large is ignored above this limit)."
        else:
            reason = f"Blob is {size_kb}KB — too large to return inline by default (limit: {INLINE_BLOB_THRESHOLD // 1024}KB)."
        above_hard_cap = size > HARD_BLOB_THRESHOLD
        note_lines = [
            reason,
            f"Use the download script: c3po-download {blob_id} [output_path]",
            "Script location: which c3po-download OR ~/.claude/plugins/cache/michaelansel/c3po/*/scripts/c3po-download",
        ]
        if not above_hard_cap:
            note_lines.append(f"To read inline anyway (up to {HARD_BLOB_THRESHOLD // 1024}KB): set inline_large=True on this fetch_blob call.")
            note_lines.append("If you don't have shell access: set scripts_unavailable=True and inline_large=True.")
        else:
            note_lines.append("If you don't have shell access: contact the sender to split content into smaller pieces.")
        return {
            **metadata,
            "download_url": download_url,
            "note": "\n".join(note_lines),
        }


@mcp.tool()
def ack_messages(
    ctx: Context,
    message_ids: list[str],
    agent_id: Optional[str] = None,
) -> dict:
    """Acknowledge messages so they no longer appear in get_messages/wait_for_message.

    After calling get_messages or wait_for_message, call this with the message IDs
    (the "id" field for messages, "reply_id" field for replies) to mark them as
    processed. Unacked messages will reappear on subsequent get_messages calls.

    Args:
        ctx: MCP context (injected automatically)
        message_ids: List of message/reply IDs to acknowledge (format: from_agent::to_agent::uuid)
        agent_id: Your agent ID (from session start output). If not provided, uses header-based ID.

    Returns:
        Dict with acked count and whether compaction was triggered

    Raises:
        ToolError: If message_ids is empty or contains invalid IDs
    """
    effective_id = _resolve_agent_id(ctx, agent_id)
    _enforce_agent_pattern(ctx, effective_id)

    # Rate limit
    allowed, _ = rate_limiter.check_and_record("ack_messages", effective_id)
    if not allowed:
        err = rate_limited(effective_id, 30, 60)
        raise ToolError(f"{err.message} {err.suggestion}")

    return _ack_messages_impl(message_manager, effective_id, message_ids)


@mcp.tool()
def upload_blob(
    ctx: Context,
    content: str,
    filename: str,
    mime_type: str = "application/octet-stream",
    encoding: str = "utf-8",
    agent_id: Optional[str] = None,
    scripts_unavailable: bool = False,
) -> dict:
    """Upload a blob for storage and sharing with other agents.

    PREFER the c3po-upload shell script when you have shell access — it uploads
    directly via curl without passing file contents through the MCP context window,
    saving tokens. Only use this MCP tool for small text blobs you've composed
    in-memory, or when shell scripts are unavailable.

      blob_id=$(c3po-upload /path/to/file)
      # Script location: which c3po-upload OR ~/.claude/plugins/cache/michaelansel/c3po/*/scripts/c3po-upload

    Set scripts_unavailable=True if you've confirmed you don't have shell access
    (e.g. Claude Desktop or claude.ai without a terminal).

    Args:
        ctx: MCP context (injected automatically)
        content: The content to store (text or base64-encoded binary)
        filename: Filename for the blob
        mime_type: MIME type (default: application/octet-stream)
        encoding: Content encoding - "utf-8" for text, "base64" for binary (default: utf-8)
        agent_id: Your agent ID (from session start output). If not provided, uses header-based ID.
        scripts_unavailable: Set True if you've confirmed shell scripts are not accessible
            (suppresses the script tip in the response).

    Returns:
        Blob metadata including blob_id, filename, size, expires_in
    """
    effective_id = _resolve_agent_id(ctx, agent_id)
    _enforce_agent_pattern(ctx, effective_id)

    # Rate limit
    allowed, _ = rate_limiter.check_and_record("upload_blob", effective_id)
    if not allowed:
        err = rate_limited(effective_id, 10, 60)
        raise ToolError(f"{err.message} {err.suggestion}")

    # Decode content
    if encoding == "base64":
        import base64
        try:
            raw_content = base64.b64decode(content)
        except Exception:
            err = invalid_request("content", "invalid base64 encoding")
            raise ToolError(f"{err.message} {err.suggestion}")
    else:
        raw_content = content.encode("utf-8")

    meta = _upload_blob_impl(blob_manager, raw_content, filename, mime_type, effective_id)
    audit_logger.blob_upload(meta["blob_id"], filename, len(raw_content), effective_id, source="mcp")

    if not scripts_unavailable:
        meta["_scripts_tip"] = (
            "Prefer `c3po-upload /path/to/file` — it avoids passing file contents through MCP "
            "(token-efficient). Set scripts_unavailable=True if you genuinely don't have shell access."
        )

    return meta


@mcp.tool()
def fetch_blob(
    ctx: Context,
    blob_id: str,
    agent_id: Optional[str] = None,
    scripts_unavailable: bool = False,
    inline_large: bool = False,
) -> dict:
    """Fetch a blob by its ID.

    For small blobs (<=10KB), content is returned inline.
    For larger blobs (10KB–100KB), use the download script — or set inline_large=True to read inline anyway.
    For blobs >100KB, use the download script (hard cap, inline_large is ignored).

      c3po-download <blob_id> [output_path]
      # Script location: which c3po-download OR ~/.claude/plugins/cache/michaelansel/c3po/*/scripts/c3po-download

    Set scripts_unavailable=True if you don't have shell access (suppresses script tip in responses).

    Args:
        ctx: MCP context (injected automatically)
        blob_id: The blob ID to fetch
        agent_id: Your agent ID (from session start output). If not provided, uses header-based ID.
        scripts_unavailable: Set True if shell scripts are not accessible (suppresses script tip).
        inline_large: Set True to return blobs between 10KB–100KB inline instead of redirecting
            to the download script. Ignored for blobs >100KB (hard cap).

    Returns:
        Blob metadata with content (small blobs or inline_large) or download_url (large blobs)
    """
    effective_id = _resolve_agent_id(ctx, agent_id)
    _enforce_agent_pattern(ctx, effective_id)

    # Rate limit
    allowed, _ = rate_limiter.check_and_record("fetch_blob", effective_id)
    if not allowed:
        err = rate_limited(effective_id, 30, 60)
        raise ToolError(f"{err.message} {err.suggestion}")

    # Determine coordinator URL for download_url
    coordinator_url = os.environ.get("C3PO_COORDINATOR_URL", "")

    result = _fetch_blob_impl(blob_manager, blob_id, coordinator_url, inline_large=inline_large)
    audit_logger.blob_download(blob_id, effective_id, source="mcp")
    return result


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
    # Redact credentials from Redis URL in logs
    redacted_url = re.sub(r"://[^@]+@", "://***@", REDIS_URL) if "@" in REDIS_URL else REDIS_URL
    logger.info("Redis URL: %s", redacted_url)

    # Log authentication configuration
    if auth_manager.auth_enabled:
        has_server_secret = bool(os.environ.get("C3PO_SERVER_SECRET"))
        has_proxy_token = bool(os.environ.get("C3PO_PROXY_BEARER_TOKEN"))
        has_admin_key = bool(os.environ.get("C3PO_ADMIN_KEY"))
        logger.info(
            "Auth configured: server_secret=%s proxy_token=%s admin_key=%s",
            "yes" if has_server_secret else "no",
            "yes" if has_proxy_token else "no",
            "yes" if has_admin_key else "no",
        )
        if has_admin_key and not has_server_secret:
            logger.error(
                "C3PO_ADMIN_KEY is set but C3PO_SERVER_SECRET is not. "
                "Both must be set for admin authentication to work. "
                "Admin endpoints will reject all requests."
            )
    else:
        logger.warning(
            "No auth tokens configured (C3PO_SERVER_SECRET, C3PO_PROXY_BEARER_TOKEN, C3PO_ADMIN_KEY). "
            "Authentication is DISABLED. Anyone with network access can use this coordinator. "
            "This is only appropriate for local development."
        )

    # Test Redis connection at startup with improved error message
    try:
        redis_client.ping()
        logger.info("Redis connection verified")
    except redis.ConnectionError as e:
        raise RedisConnectionError(REDIS_URL, e) from e
    except redis.RedisError as e:
        raise RedisConnectionError(REDIS_URL, e) from e

    # Intercept signal.signal so that when uvicorn installs its SIGTERM handler
    # (overriding ours), we wrap it to also set _shutdown_event.  This lets
    # BLPOP threads detect shutdown within one polling cycle (~10s) instead of
    # waiting for the full graceful-shutdown timeout.
    _real_signal_fn = signal.signal

    def _wrap_sigterm_handler(signum, handler):
        if signum == signal.SIGTERM and callable(handler):
            wrapped_handler = handler

            def _combined(sig, frame):
                logger.info("SIGTERM received, shutting down gracefully")
                # Set shutdown event — BLPOP threads check this between iterations.
                _shutdown_event.set()
                # Do NOT do Redis or lock operations in the signal handler.
                # Spawn a thread that wakes BLPOP threads via Redis, then after
                # a grace period triggers uvicorn's shutdown. This gives FastMCP
                # time to send HTTP responses before its session manager cancels
                # in-flight tasks.
                def _drain_and_shutdown():
                    # Wake all BLPOP threads immediately by pushing to their notify
                    # channels. This makes them detect _shutdown_event within ms
                    # instead of waiting up to 10s for the BLPOP timeout.
                    with _active_waiters_lock:
                        waiters = list(_active_waiters)
                    logger.info("Waking %d active wait_for_message callers", len(waiters))
                    for agent_id in waiters:
                        try:
                            redis_client.rpush(f"c3po:notify:{agent_id}", "shutdown")
                        except Exception:
                            pass  # Best-effort; thread will detect on next BLPOP cycle
                    # Give the event loop time to process BLPOP future resolutions
                    # and send HTTP responses before shutdown begins.
                    time.sleep(2)
                    wrapped_handler(sig, frame)
                threading.Thread(target=_drain_and_shutdown, daemon=True).start()

            return _real_signal_fn(signum, _combined)
        return _real_signal_fn(signum, handler)

    signal.signal = _wrap_sigterm_handler  # type: ignore[assignment]
    try:
        mcp.run(
            transport="http", host=host, port=port, stateless_http=True,
            uvicorn_config={"timeout_graceful_shutdown": 12},
        )
    finally:
        signal.signal = _real_signal_fn  # type: ignore[assignment]


if __name__ == "__main__":
    main()
