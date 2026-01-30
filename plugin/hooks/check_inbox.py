#!/usr/bin/env python3
"""
C3PO Stop Hook - Check for pending coordination requests.

This hook runs when Claude finishes responding. If there are pending
requests in the agent's inbox, it blocks Claude from stopping and
instructs it to process the pending requests.

Exit codes:
- 0: Allow stop (no pending requests, or check failed - fail open)
- 0 with JSON {"decision": "block", "reason": "..."}: Block stop

Environment variables:
- C3PO_COORDINATOR_URL: Coordinator URL (default: http://localhost:8420)
"""

import json
import os
import sys
import urllib.request
import urllib.error

from c3po_common import get_coordinator_url, read_agent_id


# Configuration
COORDINATOR_URL = get_coordinator_url()


def _heartbeat(assigned_id: str) -> None:
    """Ping the coordinator to refresh last_seen for this agent.

    The Stop hook is the most reliable periodic signal we get from
    Claude Code (fires every turn), so we use it as a heartbeat to
    keep the agent marked as online.
    """
    try:
        req = urllib.request.Request(
            f"{COORDINATOR_URL}/api/register",
            data=b"",
            headers={"X-Agent-ID": assigned_id},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass  # Best effort - don't block stop


def main() -> None:
    # Read hook input from stdin FIRST to get session_id
    try:
        stdin_data = json.load(sys.stdin)
    except json.JSONDecodeError:
        # Can't parse input, fail open
        sys.exit(0)

    # Get session_id from stdin, fall back to ppid
    session_id = stdin_data.get("session_id", str(os.getppid()))

    # Read agent ID using session_id
    assigned_id = read_agent_id(session_id)

    # Refresh last_seen so the agent stays marked as online
    if assigned_id:
        _heartbeat(assigned_id)
    else:
        print("[c3po] Warning: no agent ID file found, skipping heartbeat", file=sys.stderr)

    # Check if stop hook is already active (prevent infinite loops)
    if stdin_data.get("stop_hook_active"):
        sys.exit(0)

    if not assigned_id:
        print("[c3po] Warning: no agent ID file found, skipping pending check", file=sys.stderr)
        sys.exit(0)

    try:
        req = urllib.request.Request(
            f"{COORDINATOR_URL}/api/pending",
            headers={"X-Agent-ID": assigned_id},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())

        count = data.get("count", 0)
        if count > 0:
            # Format the pending requests for Claude
            requests = data.get("requests", [])
            request_summary = []
            for req_data in requests[:3]:  # Show first 3
                from_agent = req_data.get("from_agent", "unknown")
                message_preview = req_data.get("message", "")[:100]
                if len(req_data.get("message", "")) > 100:
                    message_preview += "..."
                request_summary.append(f"  - From {from_agent}: {message_preview}")

            if count > 3:
                request_summary.append(f"  ... and {count - 3} more")

            summary = "\n".join(request_summary)

            # Output JSON to block Claude from stopping
            output = {
                "decision": "block",
                "reason": (
                    f"You have {count} pending coordination request(s) from other agents:\n"
                    f"{summary}\n\n"
                    "Use the get_pending_requests tool to retrieve the full request(s), "
                    "then use respond_to_request to send your response. "
                    "After responding to all requests, you may stop."
                ),
            }
            print(json.dumps(output))

    except urllib.error.URLError:
        pass
    except urllib.error.HTTPError:
        pass
    except json.JSONDecodeError:
        pass
    except Exception:
        pass

    sys.exit(0)


if __name__ == "__main__":
    main()
