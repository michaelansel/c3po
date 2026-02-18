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

# Tool name prefix for the OAuth (Claude.ai) MCP connection — hooks don't work here.
OAUTH_TOOL_PREFIX = "mcp__claude_ai_c3po__"

# Tools that need agent_id injection.
# NOTE: Also update the PreToolUse matcher in hooks.json when adding tools.
TOOLS_NEEDING_AGENT_ID = {
    "mcp__c3po__set_description",
    "mcp__c3po__register_webhook",
    "mcp__c3po__unregister_webhook",
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


def _deny(explanation: str) -> None:
    """Block the tool call and show explanation to the model via stderr.

    Exit code 2: show stderr to model and block tool call.
    """
    print(f"C3PO: {explanation}", file=sys.stderr)
    sys.exit(2)


def main() -> None:
    try:
        stdin_data = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError) as e:
        _log(f"STDIN PARSE FAILED: {type(e).__name__}: {e}")
        _deny(f"Hook stdin parse failed: {type(e).__name__}")

    tool_name = stdin_data.get("tool_name", "")
    _log(f"HOOK CALLED: tool_name={tool_name!r}")

    # Reject calls using the Claude.ai OAuth MCP connection — hooks don't work
    # there (agent_id injection is skipped), and it bypasses the direct API key path.
    # See: https://github.com/anthropics/claude-code/issues/20412
    if tool_name.startswith(OAUTH_TOOL_PREFIX):
        base_tool = tool_name[len(OAUTH_TOOL_PREFIX):]
        msg = (
            f"You're using the Claude.ai OAuth MCP connection (mcp__claude_ai_c3po__*), "
            f"which bypasses C3PO's hooks. "
            f"Use the direct connection instead: mcp__c3po__{base_tool} "
            f"(if that tool isn't available, run /c3po setup to register the direct connection). "
            f"Also disable the C3PO OAuth MCP connector in your Claude.ai account settings "
            f"(Settings → Integrations) to avoid this conflict."
        )
        _log(f"OAUTH REJECTED: {tool_name} — {msg}")
        _deny(msg)

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
        msg = (
            f"no session_id in hook stdin. Keys present: {sorted(stdin_data.keys())}. "
            "Claude Code should provide session_id automatically."
        )
        _log(f"INJECTION FAILED: {msg}")
        _deny(msg)

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
        msg = (
            f"SessionStart hook did not register agent for session {session_id}. "
            f"Expected agent ID file at: {path}. "
            f"Check that SessionStart hook ran successfully."
        )
        _log(f"INJECTION FAILED: {msg}")
        _deny(msg)

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
