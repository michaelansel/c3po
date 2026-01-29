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

import os
import sys
import urllib.request
import urllib.error

# Configuration from environment
COORDINATOR_URL = os.environ.get("C3PO_COORDINATOR_URL", "http://localhost:8420")
AGENT_ID = os.environ.get("C3PO_AGENT_ID", os.path.basename(os.getcwd()))


def main() -> None:
    """Unregister agent from coordinator."""
    try:
        req = urllib.request.Request(
            f"{COORDINATOR_URL}/api/unregister",
            headers={"X-Agent-ID": AGENT_ID},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=5)
        # Silent success - don't output during exit
    except (urllib.error.URLError, urllib.error.HTTPError, Exception):
        # Best effort - don't block exit or output errors
        pass

    # Always exit successfully - don't block session exit
    sys.exit(0)


if __name__ == "__main__":
    main()
