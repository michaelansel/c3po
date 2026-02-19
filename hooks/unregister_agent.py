#!/usr/bin/env python3
"""
C3PO SessionEnd Hook - Unregister agent on graceful disconnect.

This hook runs when a Claude Code session ends. It notifies the coordinator
that this agent is disconnecting so it can be removed from the registry
immediately (rather than waiting for the heartbeat timeout).

When C3PO_KEEP_REGISTERED=1 is set, the hook calls unregister with ?keep=true
instead of a full removal. The coordinator keeps the registry entry but marks
the agent immediately offline. This supports the watcher pattern where an
external process (e.g. wait-for-trigger.py) polls for messages on behalf of
the offline agent and wakes it when they arrive.

Exit codes:
- 0: Always (hooks should not block session exit)

Environment variables:
- C3PO_COORDINATOR_URL: Coordinator URL (default: http://localhost:8420)
- C3PO_KEEP_REGISTERED: Set to "1", "true", or "yes" to keep registry entry
    on exit (marks agent offline instead of removing). Used with watcher pattern.
"""

import json
import os
import sys
import urllib.request
import urllib.error

from c3po_common import auth_headers, get_coordinator_url, get_session_id, parse_hook_input, read_agent_id, delete_agent_id_file, urlopen_with_ssl


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
            unreg_headers = {"X-Machine-Name": assigned_id}
            unreg_headers.update(auth_headers())
            url = f"{COORDINATOR_URL}/agent/api/unregister"
            keep = os.environ.get("C3PO_KEEP_REGISTERED", "").strip().lower() in ("1", "true", "yes")
            if keep:
                url += "?keep=true"
            req = urllib.request.Request(
                url,
                headers=unreg_headers,
                method="POST",
            )
            urlopen_with_ssl(req, timeout=5)
        except (urllib.error.URLError, urllib.error.HTTPError, Exception):
            # Best effort - don't block exit
            pass

    # Clean up the agent_id file
    delete_agent_id_file(session_id)

    sys.exit(0)


if __name__ == "__main__":
    main()
