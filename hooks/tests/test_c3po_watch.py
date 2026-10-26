"""Tests for the scripts/c3po-watch background watcher."""

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest


WATCH_SCRIPT = os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "c3po-watch")

AGENT_ID = "test-machine/test-project"


class MockCoordinatorHandler(BaseHTTPRequestHandler):
    """Serves queued (status_code, body, headers) responses for /agent/api/wait."""

    responses = []
    received_requests = []

    def log_message(self, format, *args):
        pass

    def do_GET(self):
        MockCoordinatorHandler.received_requests.append({
            "path": self.path,
            "headers": dict(self.headers),
        })
        if MockCoordinatorHandler.responses:
            code, body, headers = MockCoordinatorHandler.responses.pop(0)
        else:
            code, body, headers = 200, {"count": 0, "status": "timeout"}, {}
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        for name, value in headers.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())


@pytest.fixture
def mock_coordinator():
    MockCoordinatorHandler.responses = []
    MockCoordinatorHandler.received_requests = []

    server = HTTPServer(("127.0.0.1", 0), MockCoordinatorHandler)
    thread = threading.Thread(target=server.serve_forever)
    thread.daemon = True
    thread.start()

    yield f"http://127.0.0.1:{server.server_address[1]}"

    server.shutdown()


def watcher_env(tmp_path, coordinator_url=None) -> dict:
    env = os.environ.copy()
    env.update({
        "HOME": str(tmp_path),  # no credentials file
        "TMPDIR": str(tmp_path),
        "C3PO_WATCH_SLEEP_SCALE": "0.01",
    })
    env.pop("XDG_RUNTIME_DIR", None)
    env.pop("C3PO_COORDINATOR_URL", None)
    if coordinator_url:
        env["C3PO_COORDINATOR_URL"] = coordinator_url
    return env


def run_watch(env, *args) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, WATCH_SCRIPT, *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )


def pid_file(tmp_path) -> str:
    return os.path.join(str(tmp_path), "c3po-watch-test-machine_test-project.pid")


class TestWatchLoop:
    def test_received_exits_zero_with_summary(self, mock_coordinator, tmp_path):
        MockCoordinatorHandler.responses = [
            (200, {"count": 1, "status": "received", "messages": [
                {"id": "msg-1", "from_agent": "other/agent", "message": "Hello\nthere"},
            ]}, {}),
        ]
        result = run_watch(watcher_env(tmp_path, mock_coordinator), "--agent-id", AGENT_ID)

        assert result.returncode == 0
        assert f"C3PO_MESSAGES 1 pending for {AGENT_ID}" in result.stdout
        assert 'id=msg-1 from=other/agent preview="Hello there"' in result.stdout
        req = MockCoordinatorHandler.received_requests[0]
        assert req["path"] == "/agent/api/wait?timeout=240"
        assert req["headers"]["X-Machine-Name"] == AGENT_ID

    def test_reply_shows_reply_id_and_reply_to(self, mock_coordinator, tmp_path):
        MockCoordinatorHandler.responses = [
            (200, {"count": 1, "status": "received", "messages": [
                {"reply_id": "r-1", "reply_to": "msg-1", "from_agent": "other/agent", "message": "ok"},
            ]}, {}),
        ]
        result = run_watch(watcher_env(tmp_path, mock_coordinator), "--agent-id", AGENT_ID)

        assert result.returncode == 0
        assert "id=r-1 from=other/agent reply_to=msg-1" in result.stdout

    def test_retries_timeout_retry_and_server_errors(self, mock_coordinator, tmp_path):
        MockCoordinatorHandler.responses = [
            (200, {"count": 0, "status": "timeout"}, {}),
            (200, {"count": 0, "status": "retry"}, {"Retry-After": "1"}),
            (500, {"error": "boom"}, {}),
            (429, {"error": "rate limited"}, {}),
            (200, {"count": 1, "status": "received", "messages": [
                {"id": "msg-1", "from_agent": "other/agent", "message": "hi"},
            ]}, {}),
        ]
        result = run_watch(watcher_env(tmp_path, mock_coordinator), "--agent-id", AGENT_ID)

        assert result.returncode == 0
        assert len(MockCoordinatorHandler.received_requests) == 5
        assert "C3PO_MESSAGES 1 pending" in result.stdout

    def test_auth_rejection_exits_two(self, mock_coordinator, tmp_path):
        MockCoordinatorHandler.responses = [(401, {"error": "bad token"}, {})]
        result = run_watch(watcher_env(tmp_path, mock_coordinator), "--agent-id", AGENT_ID)

        assert result.returncode == 2
        assert "C3PO_WATCH_ERROR HTTP 401" in result.stdout

    def test_pid_file_removed_on_exit(self, mock_coordinator, tmp_path):
        MockCoordinatorHandler.responses = [
            (200, {"count": 1, "status": "received", "messages": [{"id": "m", "message": "x"}]}, {}),
        ]
        run_watch(watcher_env(tmp_path, mock_coordinator), "--agent-id", AGENT_ID)
        assert not os.path.exists(pid_file(tmp_path))


class TestSetup:
    def test_missing_credentials_exits_three(self, tmp_path):
        result = run_watch(watcher_env(tmp_path), "--agent-id", AGENT_ID)

        assert result.returncode == 3
        assert "C3PO_WATCH_SETUP" in result.stdout

    def test_bare_machine_name_exits_three(self, mock_coordinator, tmp_path):
        result = run_watch(watcher_env(tmp_path, mock_coordinator), "--agent-id", "just-machine")

        assert result.returncode == 3


class TestSingleInstance:
    def test_second_instance_exits_one(self, mock_coordinator, tmp_path):
        with open(pid_file(tmp_path), "w") as f:
            f.write(str(os.getpid()))  # a live process
        result = run_watch(watcher_env(tmp_path, mock_coordinator), "--agent-id", AGENT_ID)

        assert result.returncode == 1
        assert "C3PO_WATCH_RUNNING" in result.stdout
        assert MockCoordinatorHandler.received_requests == []

    def test_stop_kills_running_watcher(self, mock_coordinator, tmp_path):
        env = watcher_env(tmp_path, mock_coordinator)
        watcher = subprocess.Popen(
            [sys.executable, WATCH_SCRIPT, "--agent-id", AGENT_ID, "--poll-timeout", "1"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        try:
            # Wait until the watcher has claimed its pidfile and started polling
            for _ in range(200):
                if MockCoordinatorHandler.received_requests:
                    break
                threading.Event().wait(0.02)
            assert os.path.exists(pid_file(tmp_path))

            result = run_watch(env, "--stop", "--agent-id", AGENT_ID)

            assert result.returncode == 0
            assert f"Stopped watcher pid {watcher.pid}" in result.stdout
            assert watcher.wait(timeout=5) == 128 + 15
            assert not os.path.exists(pid_file(tmp_path))
        finally:
            if watcher.poll() is None:
                watcher.kill()
