"""Tests for the c3po_common shared utilities module."""

import json
import os
import platform
import sys

import pytest

# Add hooks directory to path so we can import c3po_common
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from c3po_common import (
    auth_headers,
    get_agent_id_file,
    get_hook_secret,
    get_machine_name,
    get_coordinator_url,
    get_session_id,
    read_agent_id,
    save_agent_id,
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

    def test_reads_from_claude_json(self, tmp_path, monkeypatch):
        monkeypatch.delenv("C3PO_COORDINATOR_URL", raising=False)
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
        monkeypatch.setenv("HOME", str(tmp_path))  # No .claude.json
        assert get_coordinator_url() == "http://localhost:8420"


class TestGetMachineName:
    def test_env_c3po_machine_name_takes_priority(self, monkeypatch):
        monkeypatch.setenv("C3PO_MACHINE_NAME", "my-machine")
        monkeypatch.setenv("C3PO_AGENT_ID", "should-not-use")
        assert get_machine_name() == "my-machine"

    def test_env_c3po_agent_id_deprecated_fallback(self, monkeypatch, capsys):
        monkeypatch.delenv("C3PO_MACHINE_NAME", raising=False)
        monkeypatch.setenv("C3PO_AGENT_ID", "my-custom-id")
        assert get_machine_name() == "my-custom-id"
        captured = capsys.readouterr()
        assert "deprecated" in captured.err.lower()

    def test_reads_x_machine_name_header(self, tmp_path, monkeypatch):
        monkeypatch.delenv("C3PO_AGENT_ID", raising=False)
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

    def test_falls_back_to_x_agent_id_header(self, tmp_path, monkeypatch):
        """Old configs with X-Agent-ID should still work."""
        monkeypatch.delenv("C3PO_AGENT_ID", raising=False)
        monkeypatch.delenv("C3PO_MACHINE_NAME", raising=False)
        claude_json = tmp_path / ".claude.json"
        claude_json.write_text(json.dumps({
            "mcpServers": {
                "c3po": {
                    "url": "http://myhost:8420/mcp",
                    "headers": {
                        "X-Agent-ID": "${C3PO_AGENT_ID:-haos}"
                    }
                }
            }
        }))
        monkeypatch.setenv("HOME", str(tmp_path))
        assert get_machine_name() == "haos"

    def test_reads_plain_header_value(self, tmp_path, monkeypatch):
        monkeypatch.delenv("C3PO_AGENT_ID", raising=False)
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
        monkeypatch.delenv("C3PO_AGENT_ID", raising=False)
        monkeypatch.delenv("C3PO_MACHINE_NAME", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))  # No .claude.json
        expected = platform.node().split('.')[0]
        assert get_machine_name() == expected

    def test_ignores_unresolvable_shell_var(self, tmp_path, monkeypatch):
        """If header is just a shell variable with no default, fall back to hostname."""
        monkeypatch.delenv("C3PO_AGENT_ID", raising=False)
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


class TestGetHookSecret:
    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("C3PO_HOOK_SECRET", "my-secret")
        assert get_hook_secret() == "my-secret"

    def test_reads_from_claude_json(self, tmp_path, monkeypatch):
        monkeypatch.delenv("C3PO_HOOK_SECRET", raising=False)
        claude_json = tmp_path / ".claude.json"
        claude_json.write_text(json.dumps({
            "mcpServers": {
                "c3po": {
                    "url": "http://myhost:8420/mcp",
                    "headers": {
                        "X-C3PO-Hook-Secret": "${C3PO_HOOK_SECRET:-actual-secret}"
                    }
                }
            }
        }))
        monkeypatch.setenv("HOME", str(tmp_path))
        assert get_hook_secret() == "actual-secret"

    def test_reads_plain_header_value(self, tmp_path, monkeypatch):
        monkeypatch.delenv("C3PO_HOOK_SECRET", raising=False)
        claude_json = tmp_path / ".claude.json"
        claude_json.write_text(json.dumps({
            "mcpServers": {
                "c3po": {
                    "url": "http://myhost:8420/mcp",
                    "headers": {
                        "X-C3PO-Hook-Secret": "plain-secret"
                    }
                }
            }
        }))
        monkeypatch.setenv("HOME", str(tmp_path))
        assert get_hook_secret() == "plain-secret"

    def test_returns_none_when_not_configured(self, tmp_path, monkeypatch):
        monkeypatch.delenv("C3PO_HOOK_SECRET", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))  # No .claude.json
        assert get_hook_secret() is None

    def test_ignores_unresolvable_shell_var(self, tmp_path, monkeypatch):
        monkeypatch.delenv("C3PO_HOOK_SECRET", raising=False)
        claude_json = tmp_path / ".claude.json"
        claude_json.write_text(json.dumps({
            "mcpServers": {
                "c3po": {
                    "url": "http://myhost:8420/mcp",
                    "headers": {
                        "X-C3PO-Hook-Secret": "$C3PO_HOOK_SECRET"
                    }
                }
            }
        }))
        monkeypatch.setenv("HOME", str(tmp_path))
        assert get_hook_secret() is None


class TestAuthHeaders:
    def test_returns_hook_secret_header(self, monkeypatch):
        monkeypatch.setenv("C3PO_HOOK_SECRET", "my-secret")
        headers = auth_headers()
        assert headers == {"X-C3PO-Hook-Secret": "my-secret"}

    def test_returns_empty_when_no_secret(self, tmp_path, monkeypatch):
        monkeypatch.delenv("C3PO_HOOK_SECRET", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))  # No .claude.json
        assert auth_headers() == {}
