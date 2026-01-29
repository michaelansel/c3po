#!/usr/bin/env python3
"""
C3PO PreToolUse Hook - Ensure agent_id is set on MCP tool calls.

This hook intercepts c3po MCP tool calls and injects the correct agent_id
(assigned during session registration) into the tool input. This ensures
each Claude Code instance uses its unique agent ID for all coordination.

The agent_id is read from a file written by the SessionStart hook.

Exit codes:
- 0: Always (with JSON output to inject agent_id or allow)
"""

import json
import os
import sys

# Tools that need agent_id injection
TOOLS_NEEDING_AGENT_ID = {
    "mcp__c3po__send_request",
    "mcp__c3po__get_pending_requests",
    "mcp__c3po__respond_to_request",
    "mcp__c3po__wait_for_response",
    "mcp__c3po__wait_for_request",
}


def get_agent_id_file() -> str:
    """Get the path to the agent ID file for this session."""
    ppid = os.getppid()
    return os.path.join(os.environ.get("TMPDIR", "/tmp"), f"c3po-agent-id-{ppid}")


def read_agent_id() -> str | None:
    """Read the assigned agent_id from the session file."""
    path = get_agent_id_file()
    try:
        with open(path) as f:
            return f.read().strip() or None
    except FileNotFoundError:
        return None


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
        sys.exit(0)

    # Read the assigned agent_id from the session file
    agent_id = read_agent_id()
    if not agent_id:
        # No agent_id file - allow without injection
        # (coordinator will fall back to header-based lookup)
        sys.exit(0)

    # Inject agent_id into the tool input
    updated_input = dict(tool_input)
    updated_input["agent_id"] = agent_id

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
