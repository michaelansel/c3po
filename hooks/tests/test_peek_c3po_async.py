"""Tests for the peek_c3po_async PostToolUse hook."""

import json
import subprocess
import sys
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading
import os

import pytest


HOOK_SCRIPT = os.path.join(os.path.dirname(__file__), "..", "peek_c3po_async.py")

TEST_SESSION_ID = "test-session-peek-1234"


class MockCoordinatorHandler(BaseHTTPRequestHandler):
    """Mock HTTP handler for testing coordinator responses."""

    pending_response = {"count": 0, "messages": []}
    response_code = 200
    response_delay = 0

    def log_message(self, format, *args):
        pass

    def do_GET(self):
        import time

        if self.response_delay:
            time.sleep(self.response_delay)

        if self.path == "/agent/api/pending":
            self.send_response(self.response_code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            if self.response_code == 200:
                self.wfile.write(json.dumps(self.pending_response).encode())
            else:
                self.wfile.write(b'{"error": "test error"}')
        else:
            self.send_response(404)
            self.end_headers()


@pytest.fixture
def mock_coordinator():
    """Start a mock coordinator server."""
    MockCoordinatorHandler.pending_response = {"count": 0, "messages": []}
    MockCoordinatorHandler.response_code = 200
    MockCoordinatorHandler.response_delay = 0

    server = HTTPServer(("127.0.0.1", 0), MockCoordinatorHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever)
    thread.daemon = True
    thread.start()

    yield f"http://127.0.0.1:{port}"

    server.shutdown()


@pytest.fixture
def agent_id_file(tmp_path):
    """Write an agent ID file keyed by session_id that the hook will find."""
    tmpdir = str(tmp_path)
    path = os.path.join(tmpdir, f"c3po-agent-id-{TEST_SESSION_ID}")
    with open(path, "w") as f:
        f.write("test-machine/test-project")

    yield tmpdir

    try:
        os.unlink(path)
    except OSError:
        pass


def run_hook(stdin_data: dict, env: dict = None) -> tuple:
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


class TestPeekAsyncHook:
    """Tests for the peek_c3po_async PostToolUse hook."""

    def test_exits_silently_when_no_pending_messages(self, mock_coordinator, agent_id_file):
        """Hook should exit with no output when inbox is empty."""
        MockCoordinatorHandler.pending_response = {"count": 0, "messages": []}

        exit_code, stdout, stderr = run_hook(
            {"tool_name": "Read"},
            env={
                "C3PO_COORDINATOR_URL": mock_coordinator,
                "TMPDIR": agent_id_file,
            },
        )

        assert exit_code == 0
        assert stdout.strip() == ""

    def test_outputs_system_message_when_messages_pending(self, mock_coordinator, agent_id_file):
        """Hook should output systemMessage JSON when messages are pending."""
        MockCoordinatorHandler.pending_response = {
            "count": 1,
            "messages": [
                {
                    "id": "sender::test::msg1",
                    "from_agent": "sender-agent",
                    "message": "Please help with this task",
                },
            ],
        }

        exit_code, stdout, stderr = run_hook(
            {"tool_name": "Read"},
            env={
                "C3PO_COORDINATOR_URL": mock_coordinator,
                "TMPDIR": agent_id_file,
            },
        )

        assert exit_code == 0
        output = json.loads(stdout)
        assert "systemMessage" in output
        assert "sender-agent" in output["systemMessage"]
        assert "Please help with this task" in output["systemMessage"]
        assert "1 total" in output["systemMessage"]

    def test_prioritizes_urgent_messages(self, mock_coordinator, agent_id_file):
        """Hook should show urgent messages first with marker."""
        MockCoordinatorHandler.pending_response = {
            "count": 2,
            "messages": [
                {
                    "id": "a::test::msg1",
                    "from_agent": "normal-agent",
                    "message": "Regular message",
                },
                {
                    "id": "b::test::msg2",
                    "from_agent": "urgent-agent",
                    "message": "URGENT: need immediate help",
                },
            ],
        }

        exit_code, stdout, stderr = run_hook(
            {"tool_name": "Read"},
            env={
                "C3PO_COORDINATOR_URL": mock_coordinator,
                "TMPDIR": agent_id_file,
            },
        )

        assert exit_code == 0
        output = json.loads(stdout)
        msg = output["systemMessage"]
        # Urgent message should appear before normal
        urgent_pos = msg.find("urgent-agent")
        normal_pos = msg.find("normal-agent")
        assert urgent_pos < normal_pos

    def test_exits_silently_when_coordinator_unavailable(self, agent_id_file):
        """Hook should exit silently when coordinator is not reachable."""
        exit_code, stdout, stderr = run_hook(
            {"tool_name": "Read"},
            env={
                "C3PO_COORDINATOR_URL": "http://127.0.0.1:9999",
                "TMPDIR": agent_id_file,
            },
        )

        assert exit_code == 0
        assert stdout.strip() == ""

    def test_exits_silently_when_no_session_id(self, mock_coordinator):
        """Hook should exit silently when session_id is missing."""
        exit_code, stdout, stderr = run_hook(
            {"tool_name": "Read"},
            env={
                "C3PO_COORDINATOR_URL": mock_coordinator,
            },
        )

        # Override: send stdin without session_id
        full_env = os.environ.copy()
        full_env["C3PO_COORDINATOR_URL"] = mock_coordinator

        result = subprocess.run(
            [sys.executable, HOOK_SCRIPT],
            input=json.dumps({"tool_name": "Read"}),
            capture_output=True,
            text=True,
            env=full_env,
            timeout=10,
        )

        assert result.returncode == 0
        assert result.stdout.strip() == ""

    def test_exits_silently_when_no_agent_id_file(self, mock_coordinator, tmp_path):
        """Hook should exit silently when agent ID file doesn't exist."""
        exit_code, stdout, stderr = run_hook(
            {"tool_name": "Read"},
            env={
                "C3PO_COORDINATOR_URL": mock_coordinator,
                "TMPDIR": str(tmp_path),  # No agent ID file here
            },
        )

        assert exit_code == 0
        assert stdout.strip() == ""

    def test_exits_silently_on_invalid_json_input(self, mock_coordinator):
        """Hook should exit silently on malformed stdin."""
        full_env = os.environ.copy()
        full_env["C3PO_COORDINATOR_URL"] = mock_coordinator

        result = subprocess.run(
            [sys.executable, HOOK_SCRIPT],
            input="not valid json",
            capture_output=True,
            text=True,
            env=full_env,
            timeout=10,
        )

        assert result.returncode == 0
        assert result.stdout.strip() == ""

    def test_rate_limits_same_messages(self, mock_coordinator, agent_id_file, tmp_path):
        """Hook should not reinject the same messages within the rate limit window."""
        MockCoordinatorHandler.pending_response = {
            "count": 1,
            "messages": [
                {
                    "id": "sender::test::msg1",
                    "from_agent": "sender",
                    "message": "Hello",
                },
            ],
        }

        env = {
            "C3PO_COORDINATOR_URL": mock_coordinator,
            "TMPDIR": agent_id_file,
        }

        # First call should produce output
        exit_code, stdout, stderr = run_hook({"tool_name": "Read"}, env=env)
        assert exit_code == 0
        assert "systemMessage" in stdout

        # Second call with same messages should be rate-limited (silent)
        exit_code, stdout, stderr = run_hook({"tool_name": "Read"}, env=env)
        assert exit_code == 0
        assert stdout.strip() == ""

    def test_injects_for_new_messages_despite_rate_limit(self, mock_coordinator, agent_id_file):
        """Hook should inject when new message IDs appear, even within rate limit window."""
        env = {
            "C3PO_COORDINATOR_URL": mock_coordinator,
            "TMPDIR": agent_id_file,
        }

        # First call with message 1
        MockCoordinatorHandler.pending_response = {
            "count": 1,
            "messages": [
                {"id": "sender::test::msg1", "from_agent": "sender", "message": "First"},
            ],
        }
        exit_code, stdout, _ = run_hook({"tool_name": "Read"}, env=env)
        assert "systemMessage" in stdout

        # Second call with a new message
        MockCoordinatorHandler.pending_response = {
            "count": 2,
            "messages": [
                {"id": "sender::test::msg1", "from_agent": "sender", "message": "First"},
                {"id": "sender::test::msg2", "from_agent": "sender", "message": "Second"},
            ],
        }
        exit_code, stdout, _ = run_hook({"tool_name": "Read"}, env=env)
        assert "systemMessage" in stdout
        assert "2 total" in stdout

    def test_truncates_long_messages(self, mock_coordinator, agent_id_file):
        """Hook should truncate long message previews."""
        long_message = "x" * 200
        MockCoordinatorHandler.pending_response = {
            "count": 1,
            "messages": [
                {"id": "s::t::m1", "from_agent": "sender", "message": long_message},
            ],
        }

        exit_code, stdout, _ = run_hook(
            {"tool_name": "Read"},
            env={
                "C3PO_COORDINATOR_URL": mock_coordinator,
                "TMPDIR": agent_id_file,
            },
        )

        assert exit_code == 0
        output = json.loads(stdout)
        assert "..." in output["systemMessage"]
        assert long_message not in output["systemMessage"]

    def test_shows_remaining_count_for_many_messages(self, mock_coordinator, agent_id_file):
        """Hook should show '... and N more' when there are many messages."""
        MockCoordinatorHandler.pending_response = {
            "count": 6,
            "messages": [
                {"id": f"s::t::m{i}", "from_agent": f"agent-{i}", "message": f"Msg {i}"}
                for i in range(6)
            ],
        }

        exit_code, stdout, _ = run_hook(
            {"tool_name": "Read"},
            env={
                "C3PO_COORDINATOR_URL": mock_coordinator,
                "TMPDIR": agent_id_file,
            },
        )

        assert exit_code == 0
        output = json.loads(stdout)
        assert "and" in output["systemMessage"]
        assert "more" in output["systemMessage"]

    def test_always_exits_zero(self, mock_coordinator, agent_id_file):
        """Hook should always exit with code 0 regardless of errors."""
        # HTTP 500 from coordinator
        MockCoordinatorHandler.response_code = 500

        exit_code, stdout, stderr = run_hook(
            {"tool_name": "Read"},
            env={
                "C3PO_COORDINATOR_URL": mock_coordinator,
                "TMPDIR": agent_id_file,
            },
        )

        assert exit_code == 0
