"""Tests for the c3po_common shared utilities module."""

import json
import os
import platform
import re
import sys

import pytest

# Add hooks directory to path so we can import c3po_common
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from c3po_common import (
    auth_headers,
    get_agent_id_file,
    get_credentials,
    get_machine_name,
    get_coordinator_url,
    get_session_id,
    read_agent_id,
    sanitize_name,
    save_agent_id,
    save_credentials,
    delete_agent_id_file,
)


class TestGetAgentIdFile:
    def test_uses_session_id_in_filename(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        path = get_agent_id_file("my-session-123")
        assert path == os.path.join(str(tmp_path), "c3po-agent-id-my-session-123")

    def test_uses_tmp_fallback(self, monkeypatch):
        monkeypatch.delenv("TMPDIR", raising=False)
        path = get_agent_id_file("abc")
        assert path == "/tmp/c3po-agent-id-abc"


class TestSaveAndReadAgentId:
    def test_round_trip(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        save_agent_id("sess-1", "machine/project")
        assert read_agent_id("sess-1") == "machine/project"

    def test_read_missing_file_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        assert read_agent_id("nonexistent") is None

    def test_read_empty_file_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        path = os.path.join(str(tmp_path), "c3po-agent-id-empty")
        with open(path, "w") as f:
            f.write("")
        assert read_agent_id("empty") is None

    def test_different_sessions_different_files(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        save_agent_id("sess-a", "machine/proj-a")
        save_agent_id("sess-b", "machine/proj-b")
        assert read_agent_id("sess-a") == "machine/proj-a"
        assert read_agent_id("sess-b") == "machine/proj-b"


class TestDeleteAgentIdFile:
    def test_deletes_existing_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        save_agent_id("del-test", "machine/project")
        assert read_agent_id("del-test") == "machine/project"
        delete_agent_id_file("del-test")
        assert read_agent_id("del-test") is None

    def test_no_error_on_missing_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        # Should not raise
        delete_agent_id_file("never-existed")


class TestGetCoordinatorUrl:
    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("C3PO_COORDINATOR_URL", "http://custom:9999")
        assert get_coordinator_url() == "http://custom:9999"

    def test_reads_from_credentials_file(self, tmp_path, monkeypatch):
        monkeypatch.delenv("C3PO_COORDINATOR_URL", raising=False)
        creds_file = tmp_path / ".claude" / "c3po-credentials.json"
        creds_file.parent.mkdir(parents=True)
        creds_file.write_text(json.dumps({
            "coordinator_url": "http://creds-host:8420",
            "api_token": "sec.key",
        }))
        import c3po_common
        monkeypatch.setattr(c3po_common, "CREDENTIALS_FILE", str(creds_file))
        monkeypatch.setenv("HOME", str(tmp_path))  # No .claude.json
        assert get_coordinator_url() == "http://creds-host:8420"

    def test_reads_from_claude_json(self, tmp_path, monkeypatch):
        monkeypatch.delenv("C3PO_COORDINATOR_URL", raising=False)
        # No credentials file
        import c3po_common
        monkeypatch.setattr(c3po_common, "CREDENTIALS_FILE", str(tmp_path / "nonexistent"))
        claude_json = tmp_path / ".claude.json"
        claude_json.write_text(json.dumps({
            "mcpServers": {
                "c3po": {"url": "http://myhost:8420/mcp"}
            }
        }))
        monkeypatch.setenv("HOME", str(tmp_path))
        assert get_coordinator_url() == "http://myhost:8420"

    def test_fallback_to_localhost(self, tmp_path, monkeypatch):
        monkeypatch.delenv("C3PO_COORDINATOR_URL", raising=False)
        import c3po_common
        monkeypatch.setattr(c3po_common, "CREDENTIALS_FILE", str(tmp_path / "nonexistent"))
        monkeypatch.setenv("HOME", str(tmp_path))  # No .claude.json
        assert get_coordinator_url() == "http://localhost:8420"


class TestGetMachineName:
    def test_env_c3po_machine_name_takes_priority(self, monkeypatch):
        monkeypatch.setenv("C3PO_MACHINE_NAME", "my-machine")
        assert get_machine_name() == "my-machine"

    def test_reads_x_machine_name_header(self, tmp_path, monkeypatch):
        monkeypatch.delenv("C3PO_MACHINE_NAME", raising=False)
        claude_json = tmp_path / ".claude.json"
        claude_json.write_text(json.dumps({
            "mcpServers": {
                "c3po": {
                    "url": "http://myhost:8420/mcp",
                    "headers": {
                        "X-Machine-Name": "${C3PO_MACHINE_NAME:-haos}"
                    }
                }
            }
        }))
        monkeypatch.setenv("HOME", str(tmp_path))
        assert get_machine_name() == "haos"

    def test_reads_plain_header_value(self, tmp_path, monkeypatch):
        monkeypatch.delenv("C3PO_MACHINE_NAME", raising=False)
        claude_json = tmp_path / ".claude.json"
        claude_json.write_text(json.dumps({
            "mcpServers": {
                "c3po": {
                    "url": "http://myhost:8420/mcp",
                    "headers": {
                        "X-Machine-Name": "plain-name"
                    }
                }
            }
        }))
        monkeypatch.setenv("HOME", str(tmp_path))
        assert get_machine_name() == "plain-name"

    def test_fallback_to_hostname(self, tmp_path, monkeypatch):
        monkeypatch.delenv("C3PO_MACHINE_NAME", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))  # No .claude.json
        expected = platform.node().split('.')[0]
        assert get_machine_name() == expected

    def test_ignores_unresolvable_shell_var(self, tmp_path, monkeypatch):
        """If header is just a shell variable with no default, fall back to hostname."""
        monkeypatch.delenv("C3PO_MACHINE_NAME", raising=False)
        claude_json = tmp_path / ".claude.json"
        claude_json.write_text(json.dumps({
            "mcpServers": {
                "c3po": {
                    "url": "http://myhost:8420/mcp",
                    "headers": {
                        "X-Machine-Name": "$C3PO_MACHINE_NAME"
                    }
                }
            }
        }))
        monkeypatch.setenv("HOME", str(tmp_path))
        expected = platform.node().split('.')[0]
        assert get_machine_name() == expected


class TestGetSessionId:
    def test_returns_session_id_from_stdin(self):
        assert get_session_id({"session_id": "abc-123"}) == "abc-123"

    def test_raises_on_missing_session_id(self):
        with pytest.raises(ValueError, match="session_id missing"):
            get_session_id({})

    def test_raises_on_empty_session_id(self):
        with pytest.raises(ValueError, match="session_id missing"):
            get_session_id({"session_id": ""})


class TestGetCredentials:
    def test_returns_credentials_from_file(self, tmp_path, monkeypatch):
        creds_file = tmp_path / "creds.json"
        creds_data = {
            "coordinator_url": "http://example.com:8420",
            "api_token": "my-secret.my-key",
            "key_id": "kid-123",
            "agent_pattern": "machine/*",
        }
        creds_file.write_text(json.dumps(creds_data))
        import c3po_common
        monkeypatch.setattr(c3po_common, "CREDENTIALS_FILE", str(creds_file))
        result = get_credentials()
        assert result == creds_data

    def test_returns_empty_dict_when_file_missing(self, tmp_path, monkeypatch):
        import c3po_common
        monkeypatch.setattr(c3po_common, "CREDENTIALS_FILE", str(tmp_path / "nonexistent"))
        assert get_credentials() == {}

    def test_returns_empty_dict_on_invalid_json(self, tmp_path, monkeypatch):
        creds_file = tmp_path / "creds.json"
        creds_file.write_text("not valid json")
        import c3po_common
        monkeypatch.setattr(c3po_common, "CREDENTIALS_FILE", str(creds_file))
        assert get_credentials() == {}


class TestSaveCredentials:
    def test_saves_and_reads_back(self, tmp_path, monkeypatch):
        creds_file = tmp_path / ".claude" / "c3po-credentials.json"
        import c3po_common
        monkeypatch.setattr(c3po_common, "CREDENTIALS_FILE", str(creds_file))
        creds_data = {
            "coordinator_url": "http://example.com:8420",
            "api_token": "sec.key",
        }
        save_credentials(creds_data)
        assert get_credentials() == creds_data

    def test_file_has_restricted_permissions(self, tmp_path, monkeypatch):
        creds_file = tmp_path / ".claude" / "c3po-credentials.json"
        import c3po_common
        monkeypatch.setattr(c3po_common, "CREDENTIALS_FILE", str(creds_file))
        save_credentials({"test": "data"})
        mode = os.stat(str(creds_file)).st_mode & 0o777
        assert mode == 0o600


class TestAuthHeaders:
    def test_returns_bearer_token_from_api_token(self, tmp_path, monkeypatch):
        """New format: api_token (composite) used directly."""
        creds_file = tmp_path / "creds.json"
        creds_file.write_text(json.dumps({
            "api_token": "my-secret.my-key",
        }))
        import c3po_common
        monkeypatch.setattr(c3po_common, "CREDENTIALS_FILE", str(creds_file))
        headers = auth_headers()
        assert headers == {"Authorization": "Bearer my-secret.my-key"}

    def test_returns_bearer_token_legacy_format(self, tmp_path, monkeypatch):
        """Legacy format: server_secret + api_key combined."""
        creds_file = tmp_path / "creds.json"
        creds_file.write_text(json.dumps({
            "server_secret": "my-secret",
            "api_key": "my-key",
        }))
        import c3po_common
        monkeypatch.setattr(c3po_common, "CREDENTIALS_FILE", str(creds_file))
        headers = auth_headers()
        assert headers == {"Authorization": "Bearer my-secret.my-key"}

    def test_returns_empty_when_no_credentials(self, tmp_path, monkeypatch):
        import c3po_common
        monkeypatch.setattr(c3po_common, "CREDENTIALS_FILE", str(tmp_path / "nonexistent"))
        assert auth_headers() == {}

    def test_returns_empty_when_partial_credentials(self, tmp_path, monkeypatch):
        """Should return empty if only server_secret but no api_key or api_token."""
        creds_file = tmp_path / "creds.json"
        creds_file.write_text(json.dumps({
            "server_secret": "my-secret",
        }))
        import c3po_common
        monkeypatch.setattr(c3po_common, "CREDENTIALS_FILE", str(creds_file))
        assert auth_headers() == {}

    def test_api_token_takes_priority_over_legacy(self, tmp_path, monkeypatch):
        """If both api_token and legacy fields exist, api_token wins."""
        creds_file = tmp_path / "creds.json"
        creds_file.write_text(json.dumps({
            "api_token": "new-token",
            "server_secret": "old-secret",
            "api_key": "old-key",
        }))
        import c3po_common
        monkeypatch.setattr(c3po_common, "CREDENTIALS_FILE", str(creds_file))
        headers = auth_headers()
        assert headers == {"Authorization": "Bearer new-token"}


class TestSanitizeName:
    """Tests for the sanitize_name function."""

    def test_preserves_simple_names(self):
        assert sanitize_name("my-project") == "my-project"

    def test_preserves_alphanumeric_with_dots_and_underscores(self):
        assert sanitize_name("my_project.v2") == "my_project.v2"

    def test_replaces_spaces_with_hyphens(self):
        assert sanitize_name("my project") == "my-project"

    def test_replaces_special_characters(self):
        assert sanitize_name("my@project#name!") == "my-project-name"

    def test_collapses_consecutive_hyphens(self):
        assert sanitize_name("my@@project") == "my-project"
        assert sanitize_name("a!!!b") == "a-b"

    def test_strips_leading_and_trailing_hyphens(self):
        assert sanitize_name("@project@") == "project"
        assert sanitize_name("!!name!!") == "name"

    def test_empty_string_returns_empty(self):
        assert sanitize_name("") == ""

    def test_all_special_chars_returns_empty(self):
        assert sanitize_name("@#$%") == ""

    def test_preserves_slashes(self):
        """Slashes are valid in agent IDs (machine/project format)."""
        assert sanitize_name("machine/project") == "machine/project"

    def test_result_matches_coordinator_pattern(self):
        """Sanitized names should match the coordinator's AGENT_ID_PATTERN."""
        # Pattern from coordinator/server.py
        pattern = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_./-]{0,63}$")

        test_names = ["my-project", "Code", "c3po", "My Project v2.0"]
        for name in test_names:
            result = sanitize_name(name)
            assert pattern.match(result), f"sanitize_name({name!r}) = {result!r} doesn't match AGENT_ID_PATTERN"


class TestHooksJsonValidation:
    """Validate that hooks.json is structurally correct and references existing files."""

    HOOKS_JSON = os.path.join(os.path.dirname(__file__), "..", "hooks.json")
    PLUGIN_ROOT = os.path.join(os.path.dirname(__file__), "..", "..")

    # Known valid hook event types from Claude Code docs
    KNOWN_EVENTS = {
        "Setup",  # Plugin-specific
        "SessionStart",
        "UserPromptSubmit",
        "PreToolUse",
        "PermissionRequest",
        "PostToolUse",
        "PostToolUseFailure",
        "Notification",
        "SubagentStart",
        "SubagentStop",
        "Stop",
        "TeammateIdle",
        "TaskCompleted",
        "PreCompact",
        "SessionEnd",
    }

    KNOWN_HOOK_FIELDS = {"type", "command", "timeout", "async", "statusMessage", "once", "prompt", "model"}

    def _load_hooks_json(self):
        with open(self.HOOKS_JSON) as f:
            return json.load(f)

    def test_valid_json(self):
        """hooks.json should be valid JSON."""
        self._load_hooks_json()  # Raises on invalid JSON

    def test_has_hooks_key(self):
        data = self._load_hooks_json()
        assert "hooks" in data

    def test_uses_known_event_types(self):
        """All event types in hooks.json should be recognized by Claude Code."""
        data = self._load_hooks_json()
        for event_name in data["hooks"]:
            assert event_name in self.KNOWN_EVENTS, (
                f"Unknown hook event type: {event_name!r}. "
                f"Known types: {sorted(self.KNOWN_EVENTS)}"
            )

    def test_hook_entries_have_valid_structure(self):
        """Each hook entry should have a 'hooks' array with valid hook definitions."""
        data = self._load_hooks_json()
        for event_name, entries in data["hooks"].items():
            assert isinstance(entries, list), f"{event_name} should be a list"
            for i, entry in enumerate(entries):
                assert "hooks" in entry, f"{event_name}[{i}] missing 'hooks' key"
                assert isinstance(entry["hooks"], list), f"{event_name}[{i}].hooks should be a list"
                for j, hook in enumerate(entry["hooks"]):
                    assert "type" in hook, f"{event_name}[{i}].hooks[{j}] missing 'type'"
                    assert hook["type"] in ("command", "prompt", "agent"), (
                        f"{event_name}[{i}].hooks[{j}] has unknown type: {hook['type']}"
                    )

    def test_hook_fields_are_known(self):
        """Hook definitions should only use known fields."""
        data = self._load_hooks_json()
        for event_name, entries in data["hooks"].items():
            for i, entry in enumerate(entries):
                for j, hook in enumerate(entry["hooks"]):
                    unknown = set(hook.keys()) - self.KNOWN_HOOK_FIELDS
                    assert not unknown, (
                        f"{event_name}[{i}].hooks[{j}] has unknown fields: {unknown}"
                    )

    def test_referenced_files_exist(self):
        """All script files referenced in hooks.json should exist."""
        data = self._load_hooks_json()
        for event_name, entries in data["hooks"].items():
            for entry in entries:
                for hook in entry["hooks"]:
                    if hook["type"] == "command":
                        command = hook["command"]
                        # Extract the Python script path from the command
                        # Format: python3 "${CLAUDE_PLUGIN_ROOT}/path/to/script.py"
                        if "${CLAUDE_PLUGIN_ROOT}" in command:
                            relative_path = command.split("${CLAUDE_PLUGIN_ROOT}/")[1].rstrip('"')
                            full_path = os.path.join(self.PLUGIN_ROOT, relative_path)
                            assert os.path.exists(full_path), (
                                f"{event_name} references non-existent file: {relative_path}"
                            )
