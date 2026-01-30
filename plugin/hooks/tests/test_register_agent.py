"""Tests for the register_agent SessionStart hook."""

import json
import subprocess
import sys
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading
import os

import pytest


HOOK_SCRIPT = os.path.join(os.path.dirname(__file__), "..", "register_agent.py")

TEST_SESSION_ID = "test-session-uuid-1234"


class MockCoordinatorHandler(BaseHTTPRequestHandler):
    """Mock HTTP handler for testing coordinator responses."""

    # Class-level response configuration
    health_response = {"status": "ok", "agents_online": 0}
    register_response = {"id": "test-agent", "status": "online", "capabilities": []}
    response_code = 200
    response_delay = 0

    def log_message(self, format, *args):
        """Suppress request logging."""
        pass

    def do_GET(self):
        """Handle GET requests."""
        import time

        if self.response_delay:
            time.sleep(self.response_delay)

        if self.path == "/api/health":
            self.send_response(self.response_code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            if self.response_code == 200:
                self.wfile.write(json.dumps(self.health_response).encode())
            else:
                self.wfile.write(b'{"error": "test error"}')
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        """Handle POST requests (REST API)."""
        import time

        if self.response_delay:
            time.sleep(self.response_delay)

        if self.path == "/api/register":
            self.send_response(self.response_code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            if self.response_code == 200:
                self.wfile.write(json.dumps(self.register_response).encode())
            else:
                self.wfile.write(b'{"error": "test error"}')
        else:
            self.send_response(404)
            self.end_headers()


@pytest.fixture
def mock_coordinator():
    """Start a mock coordinator server."""
    # Reset to defaults
    MockCoordinatorHandler.health_response = {"status": "ok", "agents_online": 0}
    MockCoordinatorHandler.register_response = {"id": "test-agent", "status": "online", "capabilities": []}
    MockCoordinatorHandler.response_code = 200
    MockCoordinatorHandler.response_delay = 0

    server = HTTPServer(("127.0.0.1", 0), MockCoordinatorHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever)
    thread.daemon = True
    thread.start()

    yield f"http://127.0.0.1:{port}"

    server.shutdown()


def run_hook(env: dict = None, stdin_data: dict = None) -> tuple[int, str, str]:
    """Run the hook script and return (exit_code, stdout, stderr)."""
    full_env = os.environ.copy()
    if env:
        full_env.update(env)

    stdin_input = json.dumps(stdin_data) if stdin_data else json.dumps({"session_id": TEST_SESSION_ID})

    result = subprocess.run(
        [sys.executable, HOOK_SCRIPT],
        input=stdin_input,
        capture_output=True,
        text=True,
        env=full_env,
        timeout=10,
    )
    return result.returncode, result.stdout, result.stderr


class TestRegisterAgentHook:
    """Tests for the register_agent SessionStart hook."""

    def test_outputs_connection_status_when_coordinator_available(
        self, mock_coordinator
    ):
        """Hook should output connection status when coordinator is reachable."""
        MockCoordinatorHandler.health_response = {"status": "ok", "agents_online": 3}

        exit_code, stdout, stderr = run_hook(
            env={
                "C3PO_COORDINATOR_URL": mock_coordinator,
                "C3PO_MACHINE_NAME": "test-agent",
            },
        )

        assert exit_code == 0
        assert "[c3po] Connected to coordinator" in stdout
        assert "Your agent ID: test-agent" in stdout
        assert "3 agent(s) currently online" in stdout

    def test_outputs_local_mode_when_coordinator_unavailable(self):
        """Hook should indicate local mode when coordinator is not reachable."""
        exit_code, stdout, stderr = run_hook(
            env={
                "C3PO_COORDINATOR_URL": "http://127.0.0.1:9999",  # Non-existent
                "C3PO_MACHINE_NAME": "test-agent",
            },
        )

        assert exit_code == 0
        assert "[c3po]" in stdout
        assert "not available" in stdout or "Running in local mode" in stdout

    def test_outputs_local_mode_on_http_error(self, mock_coordinator):
        """Hook should indicate local mode on HTTP error responses."""
        MockCoordinatorHandler.response_code = 500

        exit_code, stdout, stderr = run_hook(
            env={
                "C3PO_COORDINATOR_URL": mock_coordinator,
                "C3PO_MACHINE_NAME": "test-agent",
            },
        )

        assert exit_code == 0
        assert "Running in local mode" in stdout

    def test_always_exits_successfully(self, mock_coordinator):
        """Hook should always exit with code 0 to not block session start."""
        # Test with valid coordinator
        MockCoordinatorHandler.health_response = {"status": "ok", "agents_online": 1}
        exit_code, _, _ = run_hook(
            env={
                "C3PO_COORDINATOR_URL": mock_coordinator,
                "C3PO_MACHINE_NAME": "test-agent",
            },
        )
        assert exit_code == 0

        # Test with unavailable coordinator
        exit_code, _, _ = run_hook(
            env={
                "C3PO_COORDINATOR_URL": "http://127.0.0.1:9999",
                "C3PO_MACHINE_NAME": "test-agent",
            },
        )
        assert exit_code == 0

    def test_uses_default_coordinator_url(self):
        """Hook should use default coordinator URL when not specified."""
        # Just verify the script doesn't crash with defaults
        result = subprocess.run(
            [sys.executable, HOOK_SCRIPT],
            input=json.dumps({"session_id": TEST_SESSION_ID}),
            capture_output=True,
            text=True,
            timeout=10,
        )

        # Should exit cleanly (local mode since localhost:8420 probably isn't running)
        assert result.returncode == 0
        assert "[c3po]" in result.stdout

    def test_uses_cwd_as_default_agent_id(self, mock_coordinator, tmp_path, monkeypatch):
        """Hook should use current directory name as default agent ID.

        The coordinator constructs agent_id as machine/project from headers.
        We simulate the coordinator returning an ID that includes the project name.
        """
        # Change to a temp directory with known name
        test_dir = tmp_path / "my-test-project"
        test_dir.mkdir()
        monkeypatch.chdir(test_dir)

        MockCoordinatorHandler.health_response = {"status": "ok", "agents_online": 0}
        # Coordinator would return machine/project as the agent ID
        MockCoordinatorHandler.register_response = {
            "id": "test-machine/my-test-project", "status": "online", "capabilities": []
        }

        exit_code, stdout, stderr = run_hook(
            env={
                "C3PO_COORDINATOR_URL": mock_coordinator,
                # Don't set C3PO_AGENT_ID - should use cwd name
            },
        )

        assert exit_code == 0
        assert "my-test-project" in stdout

    def test_shows_zero_agents_online(self, mock_coordinator):
        """Hook should correctly display zero agents online."""
        MockCoordinatorHandler.health_response = {"status": "ok", "agents_online": 0}

        exit_code, stdout, stderr = run_hook(
            env={
                "C3PO_COORDINATOR_URL": mock_coordinator,
                "C3PO_MACHINE_NAME": "test-agent",
            },
        )

        assert exit_code == 0
        assert "0 agent(s) currently online" in stdout

    def test_handles_missing_agents_online_field(self, mock_coordinator):
        """Hook should handle missing agents_online field gracefully."""
        MockCoordinatorHandler.health_response = {"status": "ok"}

        exit_code, stdout, stderr = run_hook(
            env={
                "C3PO_COORDINATOR_URL": mock_coordinator,
                "C3PO_MACHINE_NAME": "test-agent",
            },
        )

        assert exit_code == 0
        # Should default to 0
        assert "0 agent(s) currently online" in stdout

    def test_saves_agent_id_file_with_session_id(self, mock_coordinator, tmp_path):
        """Hook should save agent ID file keyed by session_id."""
        session_id = "unique-session-abc"
        MockCoordinatorHandler.register_response = {
            "id": "machine/project", "status": "online", "capabilities": []
        }

        run_hook(
            env={
                "C3PO_COORDINATOR_URL": mock_coordinator,
                "C3PO_MACHINE_NAME": "test-agent",
                "TMPDIR": str(tmp_path),
            },
            stdin_data={"session_id": session_id},
        )

        # Verify file was created with session_id in name
        expected_file = tmp_path / f"c3po-agent-id-{session_id}"
        assert expected_file.exists()
        assert expected_file.read_text() == "machine/project"

    def test_sends_session_id_to_coordinator(self, mock_coordinator):
        """Hook should send the session_id from stdin as X-Session-ID header."""
        session_id = "header-test-session"
        received_headers = {}

        # Capture headers from the mock
        original_do_post = MockCoordinatorHandler.do_POST

        def capturing_do_post(self):
            received_headers["X-Session-ID"] = self.headers.get("X-Session-ID")
            original_do_post(self)

        MockCoordinatorHandler.do_POST = capturing_do_post
        try:
            run_hook(
                env={
                    "C3PO_COORDINATOR_URL": mock_coordinator,
                    "C3PO_MACHINE_NAME": "test-agent",
                },
                stdin_data={"session_id": session_id},
            )

            assert received_headers.get("X-Session-ID") == session_id
        finally:
            MockCoordinatorHandler.do_POST = original_do_post
