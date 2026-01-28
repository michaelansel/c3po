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
    # Read hook input from stdin
    try:
        stdin_data = json.load(sys.stdin)
    except json.JSONDecodeError:
        # Can't parse input, fail open
        sys.exit(0)

    # Check if stop hook is already active (prevent infinite loops)
    # This flag is set when Claude is already continuing due to a stop hook
    if stdin_data.get("stop_hook_active"):
        sys.exit(0)

    # Check for pending requests
    try:
        req = urllib.request.Request(
            f"{COORDINATOR_URL}/api/pending",
            headers={"X-Agent-ID": AGENT_ID},
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
        # Coordinator not reachable - fail open (allow stop)
        pass
    except urllib.error.HTTPError:
        # API error - fail open
        pass
    except json.JSONDecodeError:
        # Invalid response - fail open
        pass
    except Exception:
        # Any other error - fail open
        pass

    sys.exit(0)


if __name__ == "__main__":
    main()
