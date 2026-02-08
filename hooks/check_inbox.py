#!/usr/bin/env python3
"""
C3PO Stop Hook - Check for pending coordination messages.

This hook runs when Claude finishes responding. If there are pending
messages in the agent's inbox, it blocks Claude from stopping and
instructs it to process the pending messages.

Exit codes:
- 0: Allow stop (no pending messages, or check failed - fail open)
- 0 with JSON {"decision": "block", "reason": "..."}: Block stop

Environment variables:
- C3PO_COORDINATOR_URL: Coordinator URL (default: http://localhost:8420)
"""

import json
import os
import sys
import urllib.request
import urllib.error

from c3po_common import auth_headers, get_coordinator_url, get_session_id, read_agent_id, urlopen_with_ssl


# Configuration
COORDINATOR_URL = get_coordinator_url()


def _heartbeat(assigned_id: str) -> None:
    """Ping the coordinator to refresh last_seen for this agent.

    The Stop hook is the most reliable periodic signal we get from
    Claude Code (fires every turn), so we use it as a heartbeat to
    keep the agent marked as online.
    """
    try:
        headers = {"X-Machine-Name": assigned_id}
        headers.update(auth_headers())
        req = urllib.request.Request(
            f"{COORDINATOR_URL}/agent/api/register",
            data=b"",
            headers=headers,
            method="POST",
        )
        urlopen_with_ssl(req, timeout=5)
    except Exception:
        pass  # Best effort - don't block stop


def main() -> None:
    # Read hook input from stdin FIRST to get session_id
    try:
        stdin_data = json.load(sys.stdin)
    except json.JSONDecodeError:
        # Can't parse input, fail open
        sys.exit(0)

    # Get session_id from stdin
    try:
        session_id = get_session_id(stdin_data)
    except ValueError:
        # No session_id — can't look up agent ID, fail open
        sys.exit(0)

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
        pending_headers = {"X-Machine-Name": assigned_id}
        pending_headers.update(auth_headers())
        req = urllib.request.Request(
            f"{COORDINATOR_URL}/agent/api/pending",
            headers=pending_headers,
        )
        with urlopen_with_ssl(req, timeout=5) as resp:
            data = json.loads(resp.read())

        count = data.get("count", 0)
        if count > 0:
            # Format the pending messages for Claude with richer previews
            messages = data.get("messages", [])
            message_summary = []
            urgent_keywords = ["urgent", "interrupt", "cancel", "asap", "emergency", "critical"]

            for msg_data in messages[:3]:  # Show first 3
                from_agent = msg_data.get("from_agent", "unknown")
                full_message = msg_data.get("message", "")
                context = msg_data.get("context", "")

                # Check for urgency keywords
                is_urgent = any(kw in full_message.lower() for kw in urgent_keywords)
                urgency_marker = "🔴 URGENT: " if is_urgent else ""

                # Show more preview for urgent messages (200 chars vs 150)
                preview_len = 200 if is_urgent else 150
                message_preview = full_message[:preview_len]
                if len(full_message) > preview_len:
                    message_preview += "..."

                # Include context preview if available
                context_preview = ""
                if context:
                    context_preview = f" (context: {context[:50]}{'...' if len(context) > 50 else ''})"

                message_summary.append(
                    f"  - {urgency_marker}From {from_agent}: {message_preview}{context_preview}"
                )

            if count > 3:
                message_summary.append(f"  ... and {count - 3} more")

            summary = "\n".join(message_summary)

            # Output JSON to block Claude from stopping
            output = {
                "decision": "block",
                "reason": (
                    f"You have {count} pending coordination message(s) from other agents:\n\n"
                    f"{summary}\n\n"
                    "Use the get_messages tool to retrieve the full message(s), "
                    "then use reply to send your response. "
                    "After responding to all messages, you may stop."
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
