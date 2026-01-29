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


def get_coordinator_url() -> str:
    """Get coordinator URL from environment or claude.json MCP config.

    Priority:
    1. C3PO_COORDINATOR_URL environment variable (allows override)
    2. MCP server URL from ~/.claude.json
    3. Fallback to localhost
    """
    # First check environment (allows override)
    if url := os.environ.get("C3PO_COORDINATOR_URL"):
        return url

    # Try to read from ~/.claude.json MCP config
    claude_json = os.path.expanduser("~/.claude.json")
    try:
        with open(claude_json) as f:
            config = json.load(f)
        mcp_servers = config.get("mcpServers", {})
        c3po_config = mcp_servers.get("c3po", {})
        url = c3po_config.get("url", "")
        if url:
            # URL is like "http://host:port/mcp", strip /mcp suffix
            return url.rsplit("/mcp", 1)[0]
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        pass

    # Fallback to localhost
    return "http://localhost:8420"


# Configuration
COORDINATOR_URL = get_coordinator_url()


def _get_agent_id_file() -> str:
    """Get the path to the agent ID file for this session."""
    ppid = os.getppid()
    return os.path.join(os.environ.get("TMPDIR", "/tmp"), f"c3po-agent-id-{ppid}")


def _read_agent_id() -> str | None:
    """Read the assigned agent_id from the session file."""
    try:
        with open(_get_agent_id_file()) as f:
            return f.read().strip() or None
    except FileNotFoundError:
        return None


def _heartbeat() -> None:
    """Ping the coordinator to refresh last_seen for this agent.

    The Stop hook is the most reliable periodic signal we get from
    Claude Code (fires every turn), so we use it as a heartbeat to
    keep the agent marked as online.
    """
    assigned_id = _read_agent_id()
    if not assigned_id:
        print("[c3po] Warning: no agent ID file found, skipping heartbeat", file=sys.stderr)
        return
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
    # Refresh last_seen so the agent stays marked as online.
    # The Stop hook fires every turn, making it a natural heartbeat.
    _heartbeat()

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
    assigned_id = _read_agent_id()
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
