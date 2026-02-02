"""
Shared utilities for C3PO hook scripts.

Provides common functionality used across SessionStart, PreToolUse, Stop,
and SessionEnd hooks: coordinator URL discovery, agent ID file I/O,
credentials management, and stdin JSON parsing.
"""

from __future__ import annotations

import json
import os
import platform
import re
import ssl
import sys
import urllib.request


CREDENTIALS_FILE = os.path.expanduser("~/.claude/c3po-credentials.json")


def get_credentials() -> dict:
    """Load credentials from ~/.claude/c3po-credentials.json.

    Returns:
        Dict with coordinator_url, server_secret, api_key, key_id, agent_pattern.
        Returns empty dict if file doesn't exist or is invalid.
    """
    try:
        with open(CREDENTIALS_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, PermissionError):
        return {}


def save_credentials(credentials: dict) -> None:
    """Save credentials to ~/.claude/c3po-credentials.json with 0o600 perms.

    Args:
        credentials: Dict with coordinator_url, server_secret, api_key, etc.
    """
    os.makedirs(os.path.dirname(CREDENTIALS_FILE), exist_ok=True)
    fd = os.open(CREDENTIALS_FILE, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(credentials, f, indent=2)
        f.write("\n")


def get_machine_name() -> str:
    """Get the configured machine name from MCP headers or environment.

    Priority:
    1. C3PO_MACHINE_NAME environment variable
    2. X-Machine-Name header default from ~/.claude.json MCP config
       (parses shell syntax like "${C3PO_MACHINE_NAME:-Michaels-Mac-mini}")
    3. Fallback to hostname
    """
    if machine_name := os.environ.get("C3PO_MACHINE_NAME"):
        return machine_name

    # Read from ~/.claude.json MCP header config
    claude_json = os.path.expanduser("~/.claude.json")
    try:
        with open(claude_json) as f:
            config = json.load(f)
        mcp_servers = config.get("mcpServers", {})
        c3po_config = mcp_servers.get("c3po", {})
        headers = c3po_config.get("headers", {})
        header_value = headers.get("X-Machine-Name", "")
        if header_value:
            # Parse shell variable syntax: "${C3PO_MACHINE_NAME:-default}"
            match = re.match(r'\$\{[^:}]+:-([^}]+)\}', header_value)
            if match:
                return match.group(1)
            # Plain value (no shell syntax)
            if not header_value.startswith("$"):
                return header_value
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        pass

    return platform.node().split('.')[0]


def get_coordinator_url() -> str:
    """Get coordinator URL from credentials file, environment, or claude.json MCP config.

    Priority:
    1. C3PO_COORDINATOR_URL environment variable (allows override)
    2. Credentials file (~/.claude/c3po-credentials.json)
    3. MCP server URL from ~/.claude.json
    4. Fallback to localhost
    """
    if url := os.environ.get("C3PO_COORDINATOR_URL"):
        return url

    # Try credentials file
    creds = get_credentials()
    if url := creds.get("coordinator_url"):
        return url

    claude_json = os.path.expanduser("~/.claude.json")
    try:
        with open(claude_json) as f:
            config = json.load(f)
        mcp_servers = config.get("mcpServers", {})
        c3po_config = mcp_servers.get("c3po", {})
        url = c3po_config.get("url", "")
        if url:
            # Strip /agent/mcp or /oauth/mcp suffix to get base URL
            for suffix in ("/agent/mcp", "/oauth/mcp", "/mcp-headless", "/mcp"):
                if url.endswith(suffix):
                    return url[:-len(suffix)]
            return url
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


def auth_headers() -> dict:
    """Return authentication headers for hook REST calls.

    Reads credentials from ~/.claude/c3po-credentials.json and returns
    Authorization: Bearer <server_secret>.<api_key> header.
    Returns empty dict if no credentials are set (dev mode).
    """
    creds = get_credentials()
    server_secret = creds.get("server_secret", "")
    api_key = creds.get("api_key", "")

    if server_secret and api_key:
        return {"Authorization": f"Bearer {server_secret}.{api_key}"}

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
    for all hooks. Raises an error if missing.
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
