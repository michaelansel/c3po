#!/usr/bin/env python3
"""
C3PO SessionStart Hook - Register agent and show coordination context.

This hook runs when a Claude Code session starts. It checks the coordinator
health, displays connection status, and provides context about available
coordination features.

Exit codes:
- 0: Always (hooks should not block session start)

Environment variables:
- C3PO_COORDINATOR_URL: Coordinator URL (default: http://localhost:8420)
- C3PO_AGENT_ID: Agent identifier (default: current directory name)
"""

import json
import os
import sys
import urllib.request
import urllib.error

# Configuration from environment
COORDINATOR_URL = os.environ.get("C3PO_COORDINATOR_URL", "http://localhost:8420")
AGENT_ID = os.environ.get("C3PO_AGENT_ID", os.path.basename(os.getcwd()))


def main() -> None:
    """Check coordinator and output session context."""
    try:
        # Check coordinator health
        req = urllib.request.Request(
            f"{COORDINATOR_URL}/api/health",
            headers={"X-Agent-ID": AGENT_ID},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())

        agents_online = data.get("agents_online", 0)

        # Output context for Claude
        print(f"[c3po] Connected to coordinator at {COORDINATOR_URL}")
        print(f"[c3po] Agent ID: {AGENT_ID}")
        print(f"[c3po] {agents_online} agent(s) currently online")
        print(
            "[c3po] Use list_agents to see available agents, "
            "send_request to collaborate."
        )

    except urllib.error.URLError as e:
        print(f"[c3po] Coordinator not available ({e.reason}). Running in local mode.")
    except urllib.error.HTTPError as e:
        print(f"[c3po] Coordinator error ({e.code}). Running in local mode.")
    except json.JSONDecodeError:
        print("[c3po] Invalid coordinator response. Running in local mode.")
    except Exception as e:
        print(f"[c3po] Coordinator check failed ({e}). Running in local mode.")

    # Always exit successfully - don't block session start
    sys.exit(0)


if __name__ == "__main__":
    main()
