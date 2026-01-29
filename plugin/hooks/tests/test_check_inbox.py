"""Tests for the check_inbox stop hook."""

import json
import subprocess
import sys
import tempfile
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading
import os

import pytest


HOOK_SCRIPT = os.path.join(os.path.dirname(__file__), "..", "check_inbox.py")


class MockCoordinatorHandler(BaseHTTPRequestHandler):
    """Mock HTTP handler for testing coordinator responses."""

    # Class-level response configuration
    pending_response = {"count": 0, "requests": []}
    health_response = {"status": "ok", "agents_online": 0}
    response_delay = 0

    def log_message(self, format, *args):
        """Suppress request logging."""
        pass

    def do_GET(self):
        """Handle GET requests."""
        import time

        if self.response_delay:
            time.sleep(self.response_delay)

        if self.path == "/api/pending":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(self.pending_response).encode())
        elif self.path == "/api/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(self.health_response).encode())
        else:
            self.send_response(404)
            self.end_headers()


@pytest.fixture
def mock_coordinator():
    """Start a mock coordinator server."""
    server = HTTPServer(("127.0.0.1", 0), MockCoordinatorHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever)
    thread.daemon = True
    thread.start()

    yield f"http://127.0.0.1:{port}"

    server.shutdown()


@pytest.fixture
def agent_id_file(tmp_path):
    """Write an agent ID file that the hook subprocess will find.

    The hook reads from $TMPDIR/c3po-agent-id-{ppid}, where ppid is the
    hook process's parent PID. Since we launch the hook via subprocess.run,
    the hook's ppid is our PID. So we write the file using our PID.
    """
    tmpdir = str(tmp_path)
    pid = os.getpid()
    path = os.path.join(tmpdir, f"c3po-agent-id-{pid}")
    with open(path, "w") as f:
        f.write("test-machine/test-project")

    yield tmpdir

    try:
        os.unlink(path)
    except OSError:
        pass


def run_hook(stdin_data: dict, env: dict = None) -> tuple[int, str, str]:
    """Run the hook script and return (exit_code, stdout, stderr)."""
    full_env = os.environ.copy()
    if env:
        full_env.update(env)

    result = subprocess.run(
        [sys.executable, HOOK_SCRIPT],
        input=json.dumps(stdin_data),
        capture_output=True,
        text=True,
        env=full_env,
        timeout=10,
    )
    return result.returncode, result.stdout, result.stderr


class TestCheckInboxHook:
    """Tests for the check_inbox stop hook."""

    def test_allows_stop_when_no_pending_requests(self, mock_coordinator, agent_id_file):
        """Hook should allow stop when there are no pending requests."""
        MockCoordinatorHandler.pending_response = {"count": 0, "requests": []}

        exit_code, stdout, stderr = run_hook(
            {"stop_hook_active": False},
            env={
                "C3PO_COORDINATOR_URL": mock_coordinator,
                "TMPDIR": agent_id_file,
            },
        )

        assert exit_code == 0
        # No JSON output means allow stop
        assert stdout.strip() == "" or not stdout.strip().startswith("{")

    def test_blocks_stop_when_pending_requests_exist(self, mock_coordinator, agent_id_file):
        """Hook should block stop and provide reason when requests are pending."""
        MockCoordinatorHandler.pending_response = {
            "count": 2,
            "requests": [
                {
                    "id": "sender::test-agent::abc123",
                    "from_agent": "sender",
                    "message": "Please help with this task",
                },
                {
                    "id": "other::test-agent::def456",
                    "from_agent": "other",
                    "message": "Another request",
                },
            ],
        }

        exit_code, stdout, stderr = run_hook(
            {"stop_hook_active": False},
            env={
                "C3PO_COORDINATOR_URL": mock_coordinator,
                "TMPDIR": agent_id_file,
            },
        )

        assert exit_code == 0
        output = json.loads(stdout)
        assert output["decision"] == "block"
        assert "2 pending coordination request" in output["reason"]
        assert "sender" in output["reason"]
        assert "get_pending_requests" in output["reason"]
        assert "respond_to_request" in output["reason"]

    def test_respects_stop_hook_active_flag(self, mock_coordinator, agent_id_file):
        """Hook should allow stop when stop_hook_active is True to prevent loops."""
        MockCoordinatorHandler.pending_response = {
            "count": 1,
            "requests": [{"from_agent": "sender", "message": "Test"}],
        }

        exit_code, stdout, stderr = run_hook(
            {"stop_hook_active": True},
            env={
                "C3PO_COORDINATOR_URL": mock_coordinator,
                "TMPDIR": agent_id_file,
            },
        )

        assert exit_code == 0
        # Should not block even though there are pending requests
        assert stdout.strip() == "" or "block" not in stdout

    def test_fails_open_when_coordinator_unavailable(self, agent_id_file):
        """Hook should allow stop when coordinator is not reachable."""
        exit_code, stdout, stderr = run_hook(
            {"stop_hook_active": False},
            env={
                "C3PO_COORDINATOR_URL": "http://127.0.0.1:9999",  # Non-existent
                "TMPDIR": agent_id_file,
            },
        )

        assert exit_code == 0
        # No blocking output
        assert stdout.strip() == "" or "block" not in stdout

    def test_fails_open_on_invalid_json_input(self, mock_coordinator, agent_id_file):
        """Hook should allow stop when stdin is invalid JSON."""
        full_env = os.environ.copy()
        full_env["C3PO_COORDINATOR_URL"] = mock_coordinator
        full_env["TMPDIR"] = agent_id_file

        result = subprocess.run(
            [sys.executable, HOOK_SCRIPT],
            input="not valid json",
            capture_output=True,
            text=True,
            env=full_env,
            timeout=10,
        )

        assert result.returncode == 0

    def test_truncates_long_messages_in_summary(self, mock_coordinator, agent_id_file):
        """Hook should truncate long messages when showing summary."""
        long_message = "x" * 200
        MockCoordinatorHandler.pending_response = {
            "count": 1,
            "requests": [{"from_agent": "sender", "message": long_message}],
        }

        exit_code, stdout, stderr = run_hook(
            {"stop_hook_active": False},
            env={
                "C3PO_COORDINATOR_URL": mock_coordinator,
                "TMPDIR": agent_id_file,
            },
        )

        assert exit_code == 0
        output = json.loads(stdout)
        assert output["decision"] == "block"
        # Message should be truncated with ...
        assert "..." in output["reason"]
        # Should not contain the full 200-char message
        assert long_message not in output["reason"]

    def test_shows_and_more_for_many_requests(self, mock_coordinator, agent_id_file):
        """Hook should show '... and N more' for many pending requests."""
        MockCoordinatorHandler.pending_response = {
            "count": 5,
            "requests": [
                {"from_agent": f"agent-{i}", "message": f"Message {i}"}
                for i in range(5)
            ],
        }

        exit_code, stdout, stderr = run_hook(
            {"stop_hook_active": False},
            env={
                "C3PO_COORDINATOR_URL": mock_coordinator,
                "TMPDIR": agent_id_file,
            },
        )

        assert exit_code == 0
        output = json.loads(stdout)
        assert output["decision"] == "block"
        assert "5 pending" in output["reason"]
        assert "and 2 more" in output["reason"]

    def test_uses_default_coordinator_url(self):
        """Hook should use default coordinator URL when not specified."""
        # Just verify the script doesn't crash with defaults
        result = subprocess.run(
            [sys.executable, HOOK_SCRIPT],
            input=json.dumps({"stop_hook_active": False}),
            capture_output=True,
            text=True,
            timeout=10,
        )

        # Should exit cleanly (fail open since localhost:8420 probably isn't running)
        assert result.returncode == 0

    def test_skips_with_warning_when_no_agent_id_file(self, mock_coordinator, tmp_path):
        """Hook should warn and skip when agent ID file is missing."""
        MockCoordinatorHandler.pending_response = {
            "count": 1,
            "requests": [{"from_agent": "sender", "message": "Test"}],
        }

        exit_code, stdout, stderr = run_hook(
            {"stop_hook_active": False},
            env={
                "C3PO_COORDINATOR_URL": mock_coordinator,
                "TMPDIR": str(tmp_path),  # No agent ID file written here
            },
        )

        assert exit_code == 0
        # Should not block (no agent ID to check)
        assert stdout.strip() == "" or "block" not in stdout
        # Should warn on stderr
        assert "no agent ID file found" in stderr
