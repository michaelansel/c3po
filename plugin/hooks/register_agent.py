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
- C3PO_AGENT_ID: Agent identifier (default: machine/project format)
- C3PO_MACHINE_NAME: Machine name (default: hostname)
"""

import json
import os
import platform
import sys
import urllib.request
import urllib.error

# Configuration from environment
COORDINATOR_URL = os.environ.get("C3PO_COORDINATOR_URL", "http://localhost:8420")

# Build agent ID: machine/project format
MACHINE_NAME = os.environ.get("C3PO_MACHINE_NAME", platform.node().split('.')[0])
PROJECT_NAME = os.path.basename(os.getcwd())
AGENT_ID = os.environ.get("C3PO_AGENT_ID", f"{MACHINE_NAME}/{PROJECT_NAME}")

# Session ID: unique per Claude Code process (parent PID since hook is subprocess)
SESSION_ID = str(os.getppid())


def register_with_coordinator() -> dict | None:
    """Register this session with the coordinator via MCP.

    Returns:
        Registration result dict or None if failed
    """
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "register_agent",
            "arguments": {}
        }
    }

    req = urllib.request.Request(
        f"{COORDINATOR_URL}/mcp",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "X-Agent-ID": AGENT_ID,
            "X-Session-ID": SESSION_ID,
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            result = json.loads(resp.read())
            if "result" in result:
                return result["result"]
    except Exception:
        pass
    return None


def main() -> None:
    """Register with coordinator and output session context."""
    try:
        # Register this session with the coordinator
        registration = register_with_coordinator()

        if registration:
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
            print(f"[c3po] Agent: {AGENT_ID}")
            print(f"[c3po] {agents_online} agent(s) currently online")
            print(
                "[c3po] Use list_agents to see available agents, "
                "send_request to collaborate."
            )
        else:
            print(f"[c3po] Could not register with coordinator. Running in local mode.")

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
