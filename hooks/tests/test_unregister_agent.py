"""Tests for the unregister_agent SessionEnd hook."""

import json
import subprocess
import sys
import tempfile
import os
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading

import pytest


HOOK_SCRIPT = os.path.join(os.path.dirname(__file__), "..", "unregister_agent.py")

TEST_SESSION_ID = "test-session-unregister-1234"


class MockCoordinatorHandler(BaseHTTPRequestHandler):
    """Mock HTTP handler for testing coordinator responses."""

    # Track received requests for assertion
    received_requests = []

    def log_message(self, format, *args):
        """Suppress request logging."""
        pass

    def do_POST(self):
        """Handle POST requests."""
        MockCoordinatorHandler.received_requests.append({
            "path": self.path,
            "headers": dict(self.headers),
        })
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status": "ok"}')


@pytest.fixture
def mock_coordinator():
    """Start a mock coordinator server."""
    MockCoordinatorHandler.received_requests = []

    server = HTTPServer(("127.0.0.1", 0), MockCoordinatorHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever)
    thread.daemon = True
    thread.start()

    yield f"http://127.0.0.1:{port}"

    server.shutdown()


def run_hook(
    mock_coordinator_url: str,
    session_id: str = TEST_SESSION_ID,
    agent_id: str = "test-machine/test-project",
    extra_env: dict = None,
) -> tuple[int, str, str]:
    """Run the unregister_agent hook and return (exit_code, stdout, stderr)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Write the agent ID file that the hook reads
        agent_id_file = os.path.join(tmpdir, f"c3po-agent-id-{session_id}")
        with open(agent_id_file, "w") as f:
            f.write(agent_id)

        full_env = os.environ.copy()
        full_env.update({
            "C3PO_COORDINATOR_URL": mock_coordinator_url,
            "TMPDIR": tmpdir,
        })
        if extra_env:
            full_env.update(extra_env)
        # Ensure no credentials are required in tests
        full_env.pop("C3PO_SERVER_SECRET", None)

        result = subprocess.run(
            [sys.executable, HOOK_SCRIPT],
            input=json.dumps({"session_id": session_id}),
            capture_output=True,
            text=True,
            env=full_env,
            timeout=10,
        )
        return result.returncode, result.stdout, result.stderr


class TestUnregisterAgentHook:
    """Tests for the unregister_agent SessionEnd hook."""

    def test_keep_registered_env_var_sends_keep_param(self, mock_coordinator):
        """When C3PO_KEEP_REGISTERED=1, hook should call unregister?keep=true."""
        exit_code, stdout, stderr = run_hook(
            mock_coordinator,
            extra_env={"C3PO_KEEP_REGISTERED": "1"},
        )

        assert exit_code == 0
        # Verify the request went to /agent/api/unregister?keep=true
        requests = MockCoordinatorHandler.received_requests
        assert len(requests) == 1
        assert requests[0]["path"] == "/agent/api/unregister?keep=true"

    def test_no_keep_flag_sends_plain_unregister(self, mock_coordinator):
        """Without C3PO_KEEP_REGISTERED, hook should call plain unregister."""
        exit_code, stdout, stderr = run_hook(mock_coordinator)

        assert exit_code == 0
        requests = MockCoordinatorHandler.received_requests
        assert len(requests) == 1
        assert requests[0]["path"] == "/agent/api/unregister"

    def test_keep_registered_still_deletes_session_file(self, mock_coordinator):
        """Session file should be deleted even when C3PO_KEEP_REGISTERED=1."""
        with tempfile.TemporaryDirectory() as tmpdir:
            session_id = "keep-test-session"
            agent_id_file = os.path.join(tmpdir, f"c3po-agent-id-{session_id}")
            with open(agent_id_file, "w") as f:
                f.write("machine/project")

            full_env = os.environ.copy()
            full_env.update({
                "C3PO_COORDINATOR_URL": mock_coordinator,
                "TMPDIR": tmpdir,
                "C3PO_KEEP_REGISTERED": "1",
            })
            full_env.pop("C3PO_SERVER_SECRET", None)

            subprocess.run(
                [sys.executable, HOOK_SCRIPT],
                input=json.dumps({"session_id": session_id}),
                capture_output=True,
                text=True,
                env=full_env,
                timeout=10,
            )

            # Session file should be gone
            assert not os.path.exists(agent_id_file)

    def test_keep_registered_true_value(self, mock_coordinator):
        """C3PO_KEEP_REGISTERED=true should also send ?keep=true."""
        exit_code, stdout, stderr = run_hook(
            mock_coordinator,
            extra_env={"C3PO_KEEP_REGISTERED": "true"},
        )

        assert exit_code == 0
        requests = MockCoordinatorHandler.received_requests
        assert len(requests) == 1
        assert "?keep=true" in requests[0]["path"]

    def test_always_exits_with_zero(self, mock_coordinator):
        """Hook should always exit 0 to not block session exit."""
        exit_code, stdout, stderr = run_hook(mock_coordinator)
        assert exit_code == 0
