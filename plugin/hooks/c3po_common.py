"""
Shared utilities for C3PO hook scripts.

Provides common functionality used across SessionStart, PreToolUse, Stop,
and SessionEnd hooks: coordinator URL discovery, agent ID file I/O,
and stdin JSON parsing.
"""

import json
import os
import sys


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
