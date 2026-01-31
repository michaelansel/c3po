"""
Shared utilities for C3PO hook scripts.

Provides common functionality used across SessionStart, PreToolUse, Stop,
and SessionEnd hooks: coordinator URL discovery, agent ID file I/O,
and stdin JSON parsing.
"""

from __future__ import annotations

import json
import os
import platform
import re
import ssl
import sys
import urllib.request


def get_machine_name() -> str:
    """Get the configured machine name from MCP headers or environment.

    Priority:
    1. C3PO_MACHINE_NAME environment variable
    2. C3PO_AGENT_ID environment variable (deprecated fallback)
    3. X-Machine-Name header default from ~/.claude.json MCP config
       (parses shell syntax like "${C3PO_MACHINE_NAME:-Michaels-Mac-mini}")
       Falls back to X-Agent-ID header for old configs.
    4. Fallback to hostname

    This ensures hooks use the same machine name that was configured
    during setup, rather than independently computing the hostname
    (which may differ, e.g. inside containers).
    """
    if machine_name := os.environ.get("C3PO_MACHINE_NAME"):
        return machine_name

    if agent_id := os.environ.get("C3PO_AGENT_ID"):
        print("[c3po] Warning: C3PO_AGENT_ID is deprecated, use C3PO_MACHINE_NAME instead", file=sys.stderr)
        return agent_id

    # Read from ~/.claude.json MCP header config
    claude_json = os.path.expanduser("~/.claude.json")
    try:
        with open(claude_json) as f:
            config = json.load(f)
        mcp_servers = config.get("mcpServers", {})
        c3po_config = mcp_servers.get("c3po", {})
        headers = c3po_config.get("headers", {})
        # Try X-Machine-Name first, fall back to X-Agent-ID for old configs
        header_value = headers.get("X-Machine-Name", "") or headers.get("X-Agent-ID", "")
        if header_value:
            # Parse shell variable syntax: "${C3PO_MACHINE_NAME:-default}" or "${C3PO_AGENT_ID:-default}"
            match = re.match(r'\$\{[^:}]+:-([^}]+)\}', header_value)
            if match:
                return match.group(1)
            # Plain value (no shell syntax)
            if not header_value.startswith("$"):
                return header_value
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        pass

    return platform.node().split('.')[0]


# Deprecated alias for backwards compatibility
get_configured_machine_name = get_machine_name


def get_coordinator_url() -> str:
    """Get coordinator URL from environment or claude.json MCP config.

    Priority:
    1. C3PO_COORDINATOR_URL environment variable (allows override)
    2. MCP server URL from ~/.claude.json
    3. Fallback to localhost
    """
    if url := os.environ.get("C3PO_COORDINATOR_URL"):
        return url

    claude_json = os.path.expanduser("~/.claude.json")
    try:
        with open(claude_json) as f:
            config = json.load(f)
        mcp_servers = config.get("mcpServers", {})
        c3po_config = mcp_servers.get("c3po", {})
        url = c3po_config.get("url", "")
        if url:
            return url.rsplit("/mcp", 1)[0]
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        pass

    return "http://localhost:8420"


def get_ssl_context() -> ssl.SSLContext | None:
    """Get SSL context with custom CA cert if configured.

    Returns None if no custom CA cert is set (uses default verification).
    Set C3PO_CA_CERT to the path of a PEM CA certificate file.
    """
    ca_cert = os.environ.get("C3PO_CA_CERT")
    if ca_cert:
        ctx = ssl.create_default_context(cafile=ca_cert)
        return ctx
    return None


def urlopen_with_ssl(req, timeout=5):
    """Open a URL request with optional custom SSL context."""
    ctx = get_ssl_context()
    if ctx:
        return urllib.request.urlopen(req, timeout=timeout, context=ctx)
    return urllib.request.urlopen(req, timeout=timeout)


def get_hook_secret() -> str | None:
    """Get the hook secret for authenticating REST calls to the coordinator.

    Hooks can't do OAuth (they run outside MCP sessions), so they
    authenticate via a shared secret that nginx validates and converts
    to the proxy bearer token.

    Priority:
    1. C3PO_HOOK_SECRET environment variable
    2. Hook secret stored in ~/.claude.json MCP config headers
       (written by setup.py during enrollment)
    """
    if secret := os.environ.get("C3PO_HOOK_SECRET"):
        return secret

    # Read from ~/.claude.json MCP header config
    claude_json = os.path.expanduser("~/.claude.json")
    try:
        with open(claude_json) as f:
            config = json.load(f)
        mcp_servers = config.get("mcpServers", {})
        c3po_config = mcp_servers.get("c3po", {})
        headers = c3po_config.get("headers", {})
        hook_secret = headers.get("X-C3PO-Hook-Secret", "")
        if hook_secret:
            # Handle shell variable syntax: "${C3PO_HOOK_SECRET:-actual_secret}"
            match = re.match(r'\$\{[^:}]+:-([^}]+)\}', hook_secret)
            if match:
                return match.group(1)
            # Plain value (no shell syntax)
            if not hook_secret.startswith("$"):
                return hook_secret
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        pass

    return None


def auth_headers() -> dict:
    """Return authentication headers for hook REST calls.

    Returns X-C3PO-Hook-Secret header if configured.
    Returns empty dict if no secret is set (dev mode / backwards compatibility).
    """
    secret = get_hook_secret()
    if secret:
        return {"X-C3PO-Hook-Secret": secret}
    return {}


def _get_runtime_dir() -> str:
    """Get the best directory for runtime files.

    Prefers XDG_RUNTIME_DIR (user-specific, tmpfs, proper permissions)
    over TMPDIR or /tmp.
    """
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    if runtime_dir and os.path.isdir(runtime_dir):
        return runtime_dir
    return os.environ.get("TMPDIR", "/tmp")


def get_agent_id_file(session_id: str) -> str:
    """Get the path to the agent ID file for a given session."""
    return os.path.join(_get_runtime_dir(), f"c3po-agent-id-{session_id}")


def read_agent_id(session_id: str) -> str | None:
    """Read the assigned agent_id from the session file."""
    path = get_agent_id_file(session_id)
    try:
        with open(path) as f:
            return f.read().strip() or None
    except FileNotFoundError:
        return None


def save_agent_id(session_id: str, agent_id: str) -> None:
    """Save the assigned agent_id for other hooks to read.

    Uses os.open with 0o600 permissions to prevent other users from
    reading agent identity files.
    """
    try:
        path = get_agent_id_file(session_id)
        fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(agent_id)
            f.flush()
            os.fsync(f.fileno())
    except OSError:
        pass  # Best effort


def delete_agent_id_file(session_id: str) -> None:
    """Remove the agent ID file for a session."""
    try:
        os.unlink(get_agent_id_file(session_id))
    except OSError:
        pass


def parse_hook_input() -> dict:
    """Read and parse JSON from stdin. Returns {} on failure."""
    try:
        return json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError, ValueError):
        return {}


def get_session_id(stdin_data: dict) -> str:
    """Extract session_id from parsed hook stdin.

    Claude Code provides session_id (a stable UUID) in the stdin JSON
    for all hooks. Raises an error if missing — the PPID fallback was
    unreliable and masked bugs.
    """
    session_id = stdin_data.get("session_id")
    if not session_id:
        print(
            "[c3po] ERROR: no session_id in hook stdin. "
            "Claude Code should provide this automatically. "
            "stdin keys: " + ", ".join(sorted(stdin_data.keys())),
            file=sys.stderr,
        )
        raise ValueError("session_id missing from hook stdin")
    return session_id
