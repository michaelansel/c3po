#!/usr/bin/env python3
"""
C3PO SessionStart Hook - Register agent and show coordination context.

This hook runs when a Claude Code session starts. It registers the agent
with a unique session ID, displays connection status, and provides context
about available coordination features.

Exit codes:
- 0: Always (hooks should not block session start)

Environment variables:
- C3PO_COORDINATOR_URL: Coordinator URL (default: http://localhost:8420)
- C3PO_AGENT_ID: Machine/agent identifier (default: hostname)
- C3PO_MACHINE_NAME: Machine name override (default: hostname)
"""

import json
import os
import platform
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

# Machine ID: defaults to hostname (matches user-scope MCP config)
MACHINE_NAME = os.environ.get("C3PO_MACHINE_NAME", platform.node().split('.')[0])
AGENT_ID = os.environ.get("C3PO_AGENT_ID", MACHINE_NAME)

# Project context (for display, not part of agent ID)
PROJECT_NAME = os.path.basename(os.getcwd())

# Session ID: unique per Claude Code process (parent PID since hook is subprocess)
SESSION_ID = str(os.getppid())


def register_with_coordinator() -> dict | None:
    """Register this session with the coordinator via REST API.

    Sends headers that coordinator uses to construct full agent_id:
    - X-Agent-ID: Machine identifier (base)
    - X-Project-Name: Project name (appended to make full agent_id)
    - X-Session-ID: Session identifier (for same-session detection)

    Returns:
        Registration result dict (with assigned agent_id) or None if failed
    """
    req = urllib.request.Request(
        f"{COORDINATOR_URL}/api/register",
        data=b"",  # POST with empty body
        headers={
            "X-Agent-ID": AGENT_ID,
            "X-Project-Name": PROJECT_NAME,
            "X-Session-ID": SESSION_ID,
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read())
    except urllib.error.URLError as e:
        if os.environ.get("C3PO_DEBUG"):
            print(f"[c3po:debug] URLError registering: {e.reason}", file=sys.stderr)
    except urllib.error.HTTPError as e:
        if os.environ.get("C3PO_DEBUG"):
            print(f"[c3po:debug] HTTPError registering: {e.code} {e.read().decode()}", file=sys.stderr)
    except Exception as e:
        if os.environ.get("C3PO_DEBUG"):
            print(f"[c3po:debug] Exception registering: {type(e).__name__}: {e}", file=sys.stderr)
    return None


def main() -> None:
    """Register with coordinator and output session context."""
    try:
        # Register this session with the coordinator
        registration = register_with_coordinator()

        if registration:
            # The coordinator returns the assigned agent_id (may have collision suffix)
            assigned_id = registration.get("id", f"{AGENT_ID}/{PROJECT_NAME}")

            # Get agent count from health endpoint
            req = urllib.request.Request(
                f"{COORDINATOR_URL}/api/health",
                headers={"X-Agent-ID": AGENT_ID},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                health = json.loads(resp.read())

            agents_online = health.get("agents_online", 0)

            # Output context for Claude - the assigned_id is critical
            # Claude must use this as agent_id parameter in MCP tool calls
            print(f"[c3po] Connected to coordinator at {COORDINATOR_URL}")
            print(f"[c3po] Your agent ID: {assigned_id}")
            print(f"[c3po] IMPORTANT: Pass agent_id=\"{assigned_id}\" "
                  f"to all c3po MCP tool calls (send_request, get_pending_requests, etc.)")
            print(f"[c3po] {agents_online} agent(s) currently online")
        else:
            print(f"[c3po] Could not register with coordinator at {COORDINATOR_URL}")
            print(f"[c3po] Running in local mode. Set C3PO_DEBUG=1 for more details.")

    except urllib.error.URLError as e:
        print(f"[c3po] Coordinator not available at {COORDINATOR_URL}")
        if os.environ.get("C3PO_DEBUG"):
            print(f"[c3po:debug] URLError: {e.reason}", file=sys.stderr)
        print(f"[c3po] Running in local mode.")
    except urllib.error.HTTPError as e:
        print(f"[c3po] Coordinator error ({e.code}) at {COORDINATOR_URL}")
        if os.environ.get("C3PO_DEBUG"):
            print(f"[c3po:debug] HTTPError: {e.read().decode()}", file=sys.stderr)
        print(f"[c3po] Running in local mode.")
    except json.JSONDecodeError as e:
        print(f"[c3po] Invalid coordinator response from {COORDINATOR_URL}")
        if os.environ.get("C3PO_DEBUG"):
            print(f"[c3po:debug] JSONDecodeError: {e}", file=sys.stderr)
        print(f"[c3po] Running in local mode.")
    except Exception as e:
        print(f"[c3po] Coordinator check failed for {COORDINATOR_URL}")
        if os.environ.get("C3PO_DEBUG"):
            print(f"[c3po:debug] Exception: {type(e).__name__}: {e}", file=sys.stderr)
        print(f"[c3po] Running in local mode.")

    # Always exit successfully - don't block session start
    sys.exit(0)


if __name__ == "__main__":
    main()
