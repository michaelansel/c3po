#!/usr/bin/env python3
"""
C3PO Async Peek Hook - Check for new coordination messages with low latency.


This hook runs asynchronously after every tool call (PostToolUse event).
It peeks at the C-3PO inbox and surfaces new messages via systemMessage
on the next conversation turn, providing <10s interrupt latency.

Rate limiting: Uses a temp file to track the last injection time and
message IDs, ensuring we don't spam the same messages repeatedly.
Max 1 injection per minute for the same set of messages.

Exit codes:
- 0: Success (with or without output)

Environment variables:
- C3PO_COORDINATOR_URL: Coordinator URL (default: http://localhost:8420)
- TMPDIR: Temp directory for rate-limit tracking (default: /tmp)
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

from c3po_common import auth_headers, get_coordinator_url, get_session_id, read_agent_id, urlopen_with_ssl


# Configuration
COORDINATOR_URL = get_coordinator_url()
RATE_LIMIT_SECONDS = 60  # Max 1 injection per minute
TMPDIR = os.environ.get("TMPDIR", "/tmp")


def _get_rate_limit_file(session_id: str) -> Path:
    """Get path to rate-limit tracking file for this session."""
    return Path(TMPDIR) / f"c3po-peek-{session_id}.json"


def _should_inject(session_id: str, message_ids: list[str]) -> bool:
    """Check if we should inject based on rate limit and message novelty.

    Args:
        session_id: Current session ID
        message_ids: List of message IDs in current inbox

    Returns:
        True if we should inject (new messages or rate limit expired)
    """
    rate_file = _get_rate_limit_file(session_id)

    if not rate_file.exists():
        return True

    try:
        with open(rate_file) as f:
            state = json.load(f)

        last_injection = state.get("last_injection", 0)
        last_message_ids = set(state.get("message_ids", []))
        current_message_ids = set(message_ids)

        # If there are new messages not seen before, inject regardless of rate limit
        new_messages = current_message_ids - last_message_ids
        if new_messages:
            return True

        # Otherwise, respect rate limit for same messages
        elapsed = time.time() - last_injection
        return elapsed >= RATE_LIMIT_SECONDS

    except (json.JSONDecodeError, IOError):
        return True


def _update_rate_limit_state(session_id: str, message_ids: list[str]) -> None:
    """Update rate-limit tracking file after injection.

    Args:
        session_id: Current session ID
        message_ids: List of message IDs that were injected
    """
    rate_file = _get_rate_limit_file(session_id)

    try:
        state = {
            "last_injection": time.time(),
            "message_ids": message_ids,
        }
        with open(rate_file, "w") as f:
            json.dump(state, f)
    except IOError:
        pass  # Best effort - don't fail the hook


def main() -> None:
    # Read hook input from stdin
    try:
        stdin_data = json.load(sys.stdin)
    except json.JSONDecodeError:
        # Can't parse input, exit silently
        sys.exit(0)

    # Get session_id from stdin
    try:
        session_id = get_session_id(stdin_data)
    except ValueError:
        # No session_id, exit silently
        sys.exit(0)

    # Read agent ID using session_id
    assigned_id = read_agent_id(session_id)
    if not assigned_id:
        # No agent ID, exit silently
        sys.exit(0)

    # Check for pending messages via REST API
    try:
        pending_headers = {"X-Machine-Name": assigned_id}
        pending_headers.update(auth_headers())
        req = urllib.request.Request(
            f"{COORDINATOR_URL}/agent/api/pending",
            headers=pending_headers,
        )
        with urlopen_with_ssl(req, timeout=3) as resp:
            data = json.loads(resp.read())

        messages = data.get("messages", [])
        if not messages:
            sys.exit(0)

        message_ids = [msg.get("id") for msg in messages]

        # Check rate limit / novelty
        if not _should_inject(session_id, message_ids):
            # Rate-limited, exit silently
            sys.exit(0)

        # Format message summary for systemMessage
        count = len(messages)
        urgent_keywords = ["urgent", "interrupt", "cancel", "asap", "emergency", "critical"]

        # Prioritize urgent messages in summary
        urgent_messages = [
            msg for msg in messages
            if any(kw in msg.get("message", "").lower() for kw in urgent_keywords)
        ]
        normal_messages = [
            msg for msg in messages
            if msg not in urgent_messages
        ]

        summary_lines = []

        # Show urgent messages first
        for msg in urgent_messages[:2]:
            from_agent = msg.get("from_agent", "unknown")
            preview = msg.get("message", "")[:100]
            if len(msg.get("message", "")) > 100:
                preview += "..."
            summary_lines.append(f"  🔴 URGENT from {from_agent}: {preview}")

        # Then normal messages
        for msg in normal_messages[:2]:
            from_agent = msg.get("from_agent", "unknown")
            preview = msg.get("message", "")[:100]
            if len(msg.get("message", "")) > 100:
                preview += "..."
            summary_lines.append(f"  - From {from_agent}: {preview}")

        remaining = count - len(summary_lines)
        if remaining > 0:
            summary_lines.append(f"  ... and {remaining} more")

        summary = "\n".join(summary_lines)

        # Output systemMessage (async hook pattern)
        output = {
            "systemMessage": (
                f"🔔 New coordination message(s) ({count} total):\n\n"
                f"{summary}\n\n"
                "Use get_messages to retrieve full messages when convenient."
            )
        }
        print(json.dumps(output))

        # Update rate-limit state
        _update_rate_limit_state(session_id, message_ids)

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
