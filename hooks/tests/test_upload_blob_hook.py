"""Tests for the upload_blob PreToolUse hook."""

import json
import subprocess
import sys
import os
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

import pytest


HOOK_SCRIPT = os.path.join(os.path.dirname(__file__), "..", "upload_blob.py")

TEST_SESSION_ID = "test-session-uuid-blob"


class MockBlobHandler(BaseHTTPRequestHandler):
    """Mock HTTP handler for blob upload endpoint."""

    upload_response = {"blob_id": "blob-abc123def456", "filename": "test.txt", "size": 5}
    upload_status = 201
    last_request_body = None
    last_request_headers = {}

    def log_message(self, format, *args):
        pass

    def do_POST(self):
        if self.path == "/agent/api/blob":
            content_length = int(self.headers.get("Content-Length", 0))
            MockBlobHandler.last_request_body = self.rfile.read(content_length)
            MockBlobHandler.last_request_headers = dict(self.headers)

            self.send_response(self.upload_status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(self.upload_response).encode())
        else:
            self.send_response(404)
            self.end_headers()


@pytest.fixture
def mock_coordinator():
    """Start a mock coordinator server."""
    MockBlobHandler.upload_response = {"blob_id": "blob-abc123def456", "filename": "test.txt", "size": 5}
    MockBlobHandler.upload_status = 201
    MockBlobHandler.last_request_body = None
    MockBlobHandler.last_request_headers = {}

    server = HTTPServer(("127.0.0.1", 0), MockBlobHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever)
    thread.daemon = True
    thread.start()

    yield f"http://127.0.0.1:{port}"

    server.shutdown()


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


class TestUploadBlobHook:
    """Tests for the upload_blob PreToolUse hook."""

    def test_passthrough_for_non_send_message(self):
        """Hook should exit silently for non-send_message tools."""
        exit_code, stdout, stderr = run_hook({
            "tool_name": "mcp__c3po__list_agents",
            "tool_input": {},
        })

        assert exit_code == 0
        assert stdout.strip() == ""

    def test_passthrough_without_file_path(self):
        """Hook should exit silently for send_message without file_path."""
        exit_code, stdout, stderr = run_hook({
            "tool_name": "mcp__c3po__send_message",
            "tool_input": {"to": "agent/b", "message": "hello"},
        })

        assert exit_code == 0
        assert stdout.strip() == ""

    def test_upload_and_rewrite(self, mock_coordinator, tmp_path):
        """Hook should upload file and rewrite message."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("hello world")

        exit_code, stdout, stderr = run_hook(
            {
                "tool_name": "mcp__c3po__send_message",
                "tool_input": {
                    "to": "agent/b",
                    "message": "Check this out",
                    "file_path": str(test_file),
                },
            },
            env={"C3PO_COORDINATOR_URL": mock_coordinator},
        )

        assert exit_code == 0
        output = json.loads(stdout)
        updated = output["hookSpecificOutput"]["updatedInput"]

        # file_path should be removed
        assert "file_path" not in updated

        # Message should be prepended with blob reference
        assert updated["message"].startswith("[blob:blob-abc123def456:test.txt]")
        assert "Check this out" in updated["message"]

        # Other fields preserved
        assert updated["to"] == "agent/b"

    def test_file_not_found(self):
        """Hook should remove file_path and allow when file doesn't exist."""
        exit_code, stdout, stderr = run_hook({
            "tool_name": "mcp__c3po__send_message",
            "tool_input": {
                "to": "agent/b",
                "message": "hello",
                "file_path": "/nonexistent/file.txt",
            },
        })

        assert exit_code == 0
        output = json.loads(stdout)
        updated = output["hookSpecificOutput"]["updatedInput"]
        assert "file_path" not in updated
        assert "WARNING" in stderr

    def test_file_too_large(self, tmp_path):
        """Hook should remove file_path when file exceeds 5MB."""
        big_file = tmp_path / "big.bin"
        big_file.write_bytes(b"x" * (5 * 1024 * 1024 + 1))

        exit_code, stdout, stderr = run_hook({
            "tool_name": "mcp__c3po__send_message",
            "tool_input": {
                "to": "agent/b",
                "message": "hello",
                "file_path": str(big_file),
            },
        })

        assert exit_code == 0
        output = json.loads(stdout)
        updated = output["hookSpecificOutput"]["updatedInput"]
        assert "file_path" not in updated
        assert "too large" in stderr

    def test_empty_file(self, tmp_path):
        """Hook should remove file_path when file is empty."""
        empty_file = tmp_path / "empty.txt"
        empty_file.write_text("")

        exit_code, stdout, stderr = run_hook({
            "tool_name": "mcp__c3po__send_message",
            "tool_input": {
                "to": "agent/b",
                "message": "hello",
                "file_path": str(empty_file),
            },
        })

        assert exit_code == 0
        output = json.loads(stdout)
        updated = output["hookSpecificOutput"]["updatedInput"]
        assert "file_path" not in updated
        assert "empty" in stderr

    def test_upload_failure_falls_through(self, tmp_path):
        """Hook should allow send_message when upload fails."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("hello")

        exit_code, stdout, stderr = run_hook(
            {
                "tool_name": "mcp__c3po__send_message",
                "tool_input": {
                    "to": "agent/b",
                    "message": "hello",
                    "file_path": str(test_file),
                },
            },
            env={"C3PO_COORDINATOR_URL": "http://127.0.0.1:9999"},  # Non-existent
        )

        assert exit_code == 0
        output = json.loads(stdout)
        updated = output["hookSpecificOutput"]["updatedInput"]
        assert "file_path" not in updated
        # Message should NOT have blob reference (upload failed)
        assert "[blob:" not in updated["message"]
        assert "WARNING" in stderr
