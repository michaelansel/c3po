#!/usr/bin/env python3
"""
C3PO PreToolUse Hook - Ensure agent_id is set on MCP tool calls.

This hook intercepts c3po MCP tool calls and injects the correct agent_id
(assigned during session registration) into the tool input. This ensures
each Claude Code instance uses its unique agent ID for all coordination.

The agent_id is read from a file written by the SessionStart hook,
keyed by the session_id provided in stdin.

Exit codes:
- 0: Always (with JSON output to inject agent_id or allow)
"""

import json
import os
import sys

from c3po_common import get_agent_id_file, get_session_id, read_agent_id

# Tools that need agent_id injection
TOOLS_NEEDING_AGENT_ID = {
    "mcp__c3po__send_request",
    "mcp__c3po__get_pending_requests",
    "mcp__c3po__respond_to_request",
    "mcp__c3po__wait_for_response",
    "mcp__c3po__wait_for_request",
}


def _debug(msg: str) -> None:
    """Write debug info to stderr (visible in Claude Code debug logs)."""
    if os.environ.get("C3PO_DEBUG"):
        print(f"[c3po:ensure_agent_id] {msg}", file=sys.stderr)


def _warn(msg: str) -> None:
    """Write warning to stderr (always visible in hook logs)."""
    print(f"[c3po:ensure_agent_id] {msg}", file=sys.stderr)


def main() -> None:
    try:
        stdin_data = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        sys.exit(0)

    tool_name = stdin_data.get("tool_name", "")

    # Only intercept c3po MCP tools that need agent_id
    if tool_name not in TOOLS_NEEDING_AGENT_ID:
        sys.exit(0)

    tool_input = stdin_data.get("tool_input", {})

    # If agent_id is already set, allow
    if tool_input.get("agent_id"):
        _debug(f"agent_id already set: {tool_input['agent_id']}")
        sys.exit(0)

    # Get session_id from stdin data
    try:
        session_id = get_session_id(stdin_data)
    except ValueError:
        _warn(
            f"INJECTION FAILED: no session_id in hook stdin. "
            f"Keys present: {sorted(stdin_data.keys())}. "
            f"Claude Code should provide session_id automatically."
        )
        sys.exit(0)

    # Read the assigned agent_id from the session file
    path = get_agent_id_file(session_id)
    _debug(f"session_id={session_id} file={path} exists={os.path.exists(path)}")
    agent_id = read_agent_id(session_id)
    if not agent_id:
        _warn(
            f"INJECTION FAILED: no agent ID file for session {session_id}. "
            f"Expected at: {path}. exists={os.path.exists(path)}. "
            f"SessionStart hook may not have run or failed to register."
        )
        sys.exit(0)

    # Inject agent_id into the tool input
    updated_input = dict(tool_input)
    updated_input["agent_id"] = agent_id

    _debug(f"injecting agent_id={agent_id} into {tool_name}")

    result = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "updatedInput": updated_input,
        }
    }
    print(json.dumps(result))
    sys.exit(0)


if __name__ == "__main__":
    main()
