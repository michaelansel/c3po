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
import time

from c3po_common import get_agent_id_file, get_session_id, read_agent_id

# Tools that need agent_id injection.
# NOTE: Also update the PreToolUse matcher in hooks.json when adding tools.
TOOLS_NEEDING_AGENT_ID = {
    "mcp__c3po__set_description",
    "mcp__c3po__send_message",
    "mcp__c3po__get_messages",
    "mcp__c3po__reply",
    "mcp__c3po__wait_for_message",
    "mcp__c3po__ack_messages",
    "mcp__c3po__upload_blob",
    "mcp__c3po__fetch_blob",
}


def _debug(msg: str) -> None:
    """Write debug info to stderr (visible in Claude Code debug logs)."""
    if os.environ.get("C3PO_DEBUG"):
        print(f"[c3po:ensure_agent_id] {msg}", file=sys.stderr)


LOG_FILE = os.path.join(os.environ.get("TMPDIR", "/tmp"), "c3po-ensure-agent-id.log")


def _log(msg: str) -> None:
    """Append to log file for diagnostics (survives across invocations)."""
    try:
        with open(LOG_FILE, "a") as f:
            f.write(f"{msg}\n")
    except Exception:
        pass
    print(f"[c3po:ensure_agent_id] {msg}", file=sys.stderr)


def main() -> None:
    try:
        stdin_data = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError) as e:
        _log(f"STDIN PARSE FAILED: {type(e).__name__}: {e}")
        sys.exit(0)

    tool_name = stdin_data.get("tool_name", "")
    _log(f"HOOK CALLED: tool_name={tool_name!r}")

    # Only intercept c3po MCP tools that need agent_id
    if tool_name not in TOOLS_NEEDING_AGENT_ID:
        _log(f"SKIPPED: tool_name not in TOOLS_NEEDING_AGENT_ID")
        sys.exit(0)

    tool_input = stdin_data.get("tool_input", {})

    # If agent_id is already set, allow
    if tool_input.get("agent_id"):
        _log(f"ALREADY SET: agent_id={tool_input['agent_id']}")
        sys.exit(0)

    # Get session_id from stdin data
    try:
        session_id = get_session_id(stdin_data)
    except ValueError:
        _log(
            f"INJECTION FAILED: no session_id in hook stdin. "
            f"Keys present: {sorted(stdin_data.keys())}. "
            f"Claude Code should provide session_id automatically."
        )
        sys.exit(0)

    # Read the assigned agent_id from the session file (retry to handle race with SessionStart hook)
    path = get_agent_id_file(session_id)
    agent_id = None
    for attempt in range(5):
        agent_id = read_agent_id(session_id)
        if agent_id:
            if attempt > 0:
                _log(f"LOOKUP: found agent_id on attempt {attempt + 1}")
            break
        time.sleep(0.1 * (2 ** attempt))  # 0.1, 0.2, 0.4, 0.8, 1.6s = ~3s total
    _log(f"LOOKUP: session_id={session_id} path={path} exists={os.path.exists(path)} agent_id={agent_id!r}")

    if not agent_id:
        _log(
            f"INJECTION FAILED: no agent ID file for session {session_id} after 5 retries. "
            f"Expected at: {path}. exists={os.path.exists(path)}. "
            f"SessionStart hook may not have run or failed to register."
        )
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
    output = json.dumps(result)
    _log(f"INJECTING: agent_id={agent_id} into {tool_name}. output={output[:200]}")
    print(output)
    sys.exit(0)


if __name__ == "__main__":
    main()
