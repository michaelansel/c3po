"""
Shared utilities for C3PO hook scripts.

Provides common functionality used across SessionStart, PreToolUse, Stop,
and SessionEnd hooks: coordinator URL discovery, agent ID file I/O,
and stdin JSON parsing.
"""

import json
import os
import platform
import re
import sys


def get_configured_machine_name() -> str:
    """Get the configured machine name from MCP headers or environment.

    Priority:
    1. C3PO_AGENT_ID environment variable
    2. C3PO_MACHINE_NAME environment variable
    3. X-Agent-ID header default from ~/.claude.json MCP config
       (parses shell syntax like "${C3PO_AGENT_ID:-Michaels-Mac-mini}")
    4. Fallback to hostname

    This ensures hooks use the same machine name that was configured
    during setup, rather than independently computing the hostname
    (which may differ, e.g. inside containers).
    """
    if agent_id := os.environ.get("C3PO_AGENT_ID"):
        return agent_id

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
        agent_id_header = headers.get("X-Agent-ID", "")
        if agent_id_header:
            # Parse shell variable syntax: "${C3PO_AGENT_ID:-default}"
            match = re.match(r'\$\{[^:}]+:-([^}]+)\}', agent_id_header)
            if match:
                return match.group(1)
            # Plain value (no shell syntax)
            if not agent_id_header.startswith("$"):
                return agent_id_header
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        pass

    return platform.node().split('.')[0]


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


def get_agent_id_file(session_id: str) -> str:
    """Get the path to the agent ID file for a given session."""
    return os.path.join(os.environ.get("TMPDIR", "/tmp"), f"c3po-agent-id-{session_id}")


def read_agent_id(session_id: str) -> str | None:
    """Read the assigned agent_id from the session file."""
    path = get_agent_id_file(session_id)
    try:
        with open(path) as f:
            return f.read().strip() or None
    except FileNotFoundError:
        return None


def save_agent_id(session_id: str, agent_id: str) -> None:
    """Save the assigned agent_id for other hooks to read."""
    try:
        path = get_agent_id_file(session_id)
        with open(path, "w") as f:
            f.write(agent_id)
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
    """Extract session_id from parsed hook stdin, falling back to PPID.

    Claude Code provides session_id (a stable UUID) in the stdin JSON
    for all hooks. Falls back to os.getppid() for backward compatibility.
    """
    return stdin_data.get("session_id", str(os.getppid()))
