"""Tests for the c3po_common shared utilities module."""

import json
import os
import sys

import pytest

# Add hooks directory to path so we can import c3po_common
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from c3po_common import (
    get_agent_id_file,
    read_agent_id,
    save_agent_id,
    delete_agent_id_file,
    get_coordinator_url,
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
        # get_coordinator_url uses expanduser("~/.claude.json")
        # We need to mock expanduser or set HOME
        assert get_coordinator_url() == "http://myhost:8420"

    def test_fallback_to_localhost(self, tmp_path, monkeypatch):
        monkeypatch.delenv("C3PO_COORDINATOR_URL", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))  # No .claude.json
        assert get_coordinator_url() == "http://localhost:8420"
