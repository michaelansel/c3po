"""Tests for the ensure_agent_id PreToolUse hook."""

import json
import subprocess
import sys
import os
import tempfile

import pytest


HOOK_SCRIPT = os.path.join(os.path.dirname(__file__), "..", "ensure_agent_id.py")

TEST_SESSION_ID = "test-session-uuid-ensure"


def run_hook(stdin_data: dict, env: dict = None) -> tuple[int, str, str]:
    """Run the hook script and return (exit_code, stdout, stderr)."""
    full_env = os.environ.copy()
    if env:
        full_env.update(env)

    if "session_id" not in stdin_data:
        stdin_data["session_id"] = TEST_SESSION_ID

    result = subprocess.run(
        [sys.executable, HOOK_SCRIPT],
        input=json.dumps(stdin_data),
        capture_output=True,
        text=True,
        env=full_env,
        timeout=10,
    )
    return result.returncode, result.stdout, result.stderr


class TestEnsureAgentIdHook:
    """Tests for the ensure_agent_id PreToolUse hook."""

    def test_stdin_parse_error_outputs_deny(self):
        """Hook should output deny decision when stdin parse fails."""
        result = subprocess.run(
            [sys.executable, HOOK_SCRIPT],
            input="invalid json {",
            capture_output=True,
            text=True,
            timeout=10,
        )

        assert result.returncode == 0
        output = json.loads(result.stdout)
        assert output["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "parse failed" in output["hookSpecificOutput"]["explanation"]

    def test_missing_session_id_outputs_deny(self):
        """Hook should output deny decision when session_id is missing."""
        # Directly test without using run_hook which adds default session_id
        result = subprocess.run(
            [sys.executable, HOOK_SCRIPT],
            input=json.dumps({
                "tool_name": "mcp__c3po__set_description",
                "tool_input": {},
                # no session_id field at all
            }),
            capture_output=True,
            text=True,
            timeout=10,
        )

        assert result.returncode == 0
        output = json.loads(result.stdout)
        assert output["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "session_id" in output["hookSpecificOutput"]["explanation"]

    def test_missing_agent_id_file_outputs_deny(self):
        """Hook should output deny decision when agent_id file doesn't exist."""
        exit_code, stdout, stderr = run_hook({
            "tool_name": "mcp__c3po__set_description",
            "tool_input": {},
            "session_id": "nonexistent-session-that-never-registered",
        })

        assert exit_code == 0
        output = json.loads(stdout)
        assert output["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "SessionStart hook" in output["hookSpecificOutput"]["explanation"]

    def test_skips_non_c3po_tools(self):
        """Hook should exit silently for non-c3po tools."""
        exit_code, stdout, stderr = run_hook({
            "tool_name": "some_other_tool",
            "tool_input": {},
        })

        assert exit_code == 0
        assert stdout.strip() == ""

    def test_skips_c3po_tools_not_needing_agent_id(self):
        """Hook should exit silently for c3po tools that don't need agent_id."""
        exit_code, stdout, stderr = run_hook({
            "tool_name": "mcp__c3po__ping",
            "tool_input": {},
        })

        assert exit_code == 0
        assert stdout.strip() == ""

    def test_allows_when_agent_id_already_set(self):
        """Hook should exit silently when agent_id is already in tool_input."""
        exit_code, stdout, stderr = run_hook({
            "tool_name": "mcp__c3po__set_description",
            "tool_input": {"agent_id": "machine/project"},
        })

        assert exit_code == 0
        assert stdout.strip() == ""

    def test_injects_agent_id_when_available(self):
        """Hook should inject agent_id when file exists."""
        # Create a temporary agent_id file
        with tempfile.NamedTemporaryFile(
            mode="w",
            prefix="c3po-agent-id-",
            delete=False,
            dir=os.environ.get("TMPDIR", "/tmp"),
        ) as f:
            f.write("test-machine/test-project")
            temp_file = f.name

        try:
            session_id = os.path.basename(temp_file).replace("c3po-agent-id-", "")

            exit_code, stdout, stderr = run_hook({
                "tool_name": "mcp__c3po__set_description",
                "tool_input": {"description": "test"},
                "session_id": session_id,
            })

            assert exit_code == 0
            output = json.loads(stdout)
            assert output["hookSpecificOutput"]["permissionDecision"] == "allow"
            assert output["hookSpecificOutput"]["updatedInput"]["agent_id"] == "test-machine/test-project"
            assert output["hookSpecificOutput"]["updatedInput"]["description"] == "test"
        finally:
            os.unlink(temp_file)

    def test_oauth_tools_are_rejected(self):
        """Hook should reject claude.ai OAuth MCP tools with directions to use direct connection."""
        oauth_tools = [
            "mcp__claude_ai_c3po__send_message",
            "mcp__claude_ai_c3po__get_messages",
            "mcp__claude_ai_c3po__ping",
            "mcp__claude_ai_c3po__list_agents",
        ]

        for tool_name in oauth_tools:
            exit_code, stdout, stderr = run_hook({
                "tool_name": tool_name,
                "tool_input": {},
            })

            assert exit_code == 0
            output = json.loads(stdout)
            assert output["hookSpecificOutput"]["permissionDecision"] == "deny"
            explanation = output["hookSpecificOutput"]["explanation"]
            # Should name the direct-connection equivalent
            base_tool = tool_name.replace("mcp__claude_ai_c3po__", "")
            assert f"mcp__c3po__{base_tool}" in explanation
            # Should mention /c3po setup and account settings
            assert "/c3po setup" in explanation
            assert "Settings" in explanation

    def test_all_tools_needing_agent_id(self):
        """Hook should intercept all tools in TOOLS_NEEDING_AGENT_ID."""
        tools_needing_agent_id = [
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
        ]

        for tool_name in tools_needing_agent_id:
            exit_code, stdout, stderr = run_hook({
                "tool_name": tool_name,
                "tool_input": {},
                "session_id": "nonexistent",
            })

            # Should output deny (no agent_id file found), not skip silently
            assert exit_code == 0
            output = json.loads(stdout)
            assert output["hookSpecificOutput"]["permissionDecision"] == "deny"
