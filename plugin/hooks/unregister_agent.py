#!/usr/bin/env python3
"""
C3PO SessionEnd Hook - Unregister agent on graceful disconnect.

This hook runs when a Claude Code session ends. It notifies the coordinator
that this agent is disconnecting so it can be removed from the registry
immediately (rather than waiting for the heartbeat timeout).

Exit codes:
- 0: Always (hooks should not block session exit)

Environment variables:
- C3PO_COORDINATOR_URL: Coordinator URL (default: http://localhost:8420)
- C3PO_AGENT_ID: Agent identifier (default: current directory name)
"""

import json
import os
import sys
import urllib.request
import urllib.error


def get_coordinator_url() -> str:
    """Get coordinator URL from environment or claude.json MCP config.

    Priority:
    1. C3PO_COORDINATOR_URL environment variable (allows override)
    2. MCP server URL from ~/.claude.json
    3. Fallback to localhost
    """
    # First check environment (allows override)
    if url := os.environ.get("C3PO_COORDINATOR_URL"):
        return url

    # Try to read from ~/.claude.json MCP config
    claude_json = os.path.expanduser("~/.claude.json")
    try:
        with open(claude_json) as f:
            config = json.load(f)
        mcp_servers = config.get("mcpServers", {})
        c3po_config = mcp_servers.get("c3po", {})
        url = c3po_config.get("url", "")
        if url:
            # URL is like "http://host:port/mcp", strip /mcp suffix
            return url.rsplit("/mcp", 1)[0]
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        pass

    # Fallback to localhost
    return "http://localhost:8420"


# Configuration
COORDINATOR_URL = get_coordinator_url()
AGENT_ID = os.environ.get("C3PO_AGENT_ID", os.path.basename(os.getcwd()))


def _get_agent_id_file() -> str:
    """Get the path to the agent ID file for this session."""
    ppid = os.getppid()
    return os.path.join(os.environ.get("TMPDIR", "/tmp"), f"c3po-agent-id-{ppid}")


def _read_agent_id() -> str | None:
    """Read the assigned agent_id from the session file."""
    try:
        with open(_get_agent_id_file()) as f:
            return f.read().strip() or None
    except FileNotFoundError:
        return None


def main() -> None:
    """Unregister agent from coordinator and clean up session file."""
    # Read the assigned agent_id (may differ from AGENT_ID due to collision)
    assigned_id = _read_agent_id() or AGENT_ID

    try:
        req = urllib.request.Request(
            f"{COORDINATOR_URL}/api/unregister",
            headers={"X-Agent-ID": assigned_id},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=5)
    except (urllib.error.URLError, urllib.error.HTTPError, Exception):
        # Best effort - don't block exit
        pass

    # Clean up the agent_id file
    try:
        os.unlink(_get_agent_id_file())
    except OSError:
        pass

    sys.exit(0)


if __name__ == "__main__":
    main()
