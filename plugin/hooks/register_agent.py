#!/usr/bin/env python3
"""
C3PO SessionStart Hook - Register agent and show coordination context.

This hook runs when a Claude Code session starts. It registers the agent
with the coordinator, displays connection status, and provides context
about available coordination features.

Exit codes:
- 0: Always (hooks should not block session start)

Environment variables:
- C3PO_COORDINATOR_URL: Coordinator URL (default: http://localhost:8420)
- C3PO_AGENT_ID: Machine/agent identifier (default: from MCP config or hostname)
- C3PO_MACHINE_NAME: Machine name override
"""

import json
import os
import sys
import urllib.request
import urllib.error

from c3po_common import get_coordinator_url, get_configured_machine_name, get_session_id, parse_hook_input, save_agent_id


# Configuration
COORDINATOR_URL = get_coordinator_url()
AGENT_ID = get_configured_machine_name()

# Project context (for display, not part of agent ID)
PROJECT_NAME = os.path.basename(os.getcwd())


def register_with_coordinator(session_id: str) -> dict | None:
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
            "X-Session-ID": session_id,
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
    # Parse stdin to get session_id from Claude Code
    stdin_data = parse_hook_input()
    session_id = get_session_id(stdin_data)

    try:
        # Register this session with the coordinator
        registration = register_with_coordinator(session_id)

        if registration:
            # The coordinator returns the assigned agent_id (may have collision suffix)
            assigned_id = registration.get("id", f"{AGENT_ID}/{PROJECT_NAME}")

            # Save assigned agent_id keyed by session_id for other hooks to read
            save_agent_id(session_id, assigned_id)

            # Get agent count from health endpoint
            req = urllib.request.Request(
                f"{COORDINATOR_URL}/api/health",
                headers={"X-Agent-ID": AGENT_ID},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                health = json.loads(resp.read())

            agents_online = health.get("agents_online", 0)

            # Output context for Claude
            print(f"[c3po] Connected to coordinator at {COORDINATOR_URL}")
            print(f"[c3po] Your agent ID: {assigned_id}")
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
