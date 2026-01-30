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
"""

import json
import os
import sys
import urllib.request
import urllib.error

from c3po_common import get_coordinator_url, get_session_id, parse_hook_input, read_agent_id, delete_agent_id_file


# Configuration
COORDINATOR_URL = get_coordinator_url()


def main() -> None:
    """Unregister agent from coordinator and clean up session file."""
    # Parse stdin to get session_id from Claude Code
    stdin_data = parse_hook_input()
    try:
        session_id = get_session_id(stdin_data)
    except ValueError:
        # No session_id — can't find agent ID file, skip cleanup
        sys.exit(0)

    # Read the assigned agent_id (written by SessionStart hook)
    assigned_id = read_agent_id(session_id)
    if not assigned_id:
        print("[c3po] Warning: no agent ID file found, skipping unregister", file=sys.stderr)
    else:
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
    delete_agent_id_file(session_id)

    sys.exit(0)


if __name__ == "__main__":
    main()
