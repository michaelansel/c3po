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
- C3PO_MACHINE_NAME: Machine name identifier (default: from MCP config or hostname)
- C3PO_AGENT_ID: Deprecated alias for C3PO_MACHINE_NAME
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
import urllib.error

from c3po_common import auth_headers, get_coordinator_url, get_machine_name, get_session_id, parse_hook_input, save_agent_id, urlopen_with_ssl


# Configuration
COORDINATOR_URL = get_coordinator_url()
MACHINE_NAME = get_machine_name()

# Project context (for display, not part of agent ID)
PROJECT_NAME = os.path.basename(os.getcwd())


def register_with_coordinator(session_id: str) -> dict | None:
    """Register this session with the coordinator via REST API.

    Sends headers that coordinator uses to construct full agent_id:
    - X-Machine-Name: Machine identifier (base)
    - X-Project-Name: Project name (appended to make full agent_id)
    - X-Session-ID: Session identifier (for same-session detection)

    Returns:
        Registration result dict (with assigned agent_id) or None if failed
    """
    headers = {
        "X-Machine-Name": MACHINE_NAME,
        "X-Project-Name": PROJECT_NAME,
        "X-Session-ID": session_id,
    }
    headers.update(auth_headers())
    req = urllib.request.Request(
        f"{COORDINATOR_URL}/api/register",
        data=b"",  # POST with empty body
        headers=headers,
        method="POST",
    )

    try:
        with urlopen_with_ssl(req, timeout=5) as resp:
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
    try:
        session_id = get_session_id(stdin_data)
    except ValueError:
        print("[c3po] Cannot register without session_id. Running in local mode.")
        sys.exit(0)

    try:
        # Register this session with the coordinator
        registration = register_with_coordinator(session_id)

        if registration:
            # The coordinator returns the assigned agent_id (may have collision suffix)
            assigned_id = registration.get("id", f"{MACHINE_NAME}/{PROJECT_NAME}")

            # Save assigned agent_id keyed by session_id for other hooks to read
            save_agent_id(session_id, assigned_id)

            # Get agent count from health endpoint (no auth required for health)
            req = urllib.request.Request(
                f"{COORDINATOR_URL}/api/health",
            )
            with urlopen_with_ssl(req, timeout=5) as resp:
                health = json.loads(resp.read())

            agents_online = health.get("agents_online", 0)

            # Output context for Claude
            print(f"[c3po] Connected to coordinator at {COORDINATOR_URL}")
            print(f"[c3po] Your agent ID: {assigned_id}")
            print(f"[c3po] {agents_online} agent(s) currently online")
            print(f"[c3po] Tip: call set_description to tell other agents what you can help with")
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
