"""Tests for the scripts/c3po-watch background watcher."""

import importlib.machinery
import importlib.util
import json
import os
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer

import pytest


WATCH_SCRIPT = os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "c3po-watch")

AGENT_ID = "test-machine/test-project"


class MockCoordinatorHandler(BaseHTTPRequestHandler):
    """Serves queued (status_code, body, headers) responses for /agent/api/wait."""

    responses = []
    received_requests = []
    hang = threading.Event()  # set = release requests queued as "HANG"

    def log_message(self, format, *args):
        pass

    def do_GET(self):
        MockCoordinatorHandler.received_requests.append({
            "path": self.path,
            "headers": dict(self.headers),
        })
        if MockCoordinatorHandler.responses:
            code, body, headers = MockCoordinatorHandler.responses.pop(0)
            if code == "HANG":  # a long poll nobody answers
                MockCoordinatorHandler.hang.wait(30)
                return
            if code == "DELAY":  # body is the delay; then answer the next response
                time.sleep(body)
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
    MockCoordinatorHandler.hang = threading.Event()

    server = ThreadingHTTPServer(("127.0.0.1", 0), MockCoordinatorHandler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever)
    thread.daemon = True
    thread.start()

    yield f"http://127.0.0.1:{server.server_address[1]}"

    MockCoordinatorHandler.hang.set()
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
    # Keep agent-ID resolution independent of the session running the tests
    for name in ("CLAUDE_CODE_SESSION_ID", "CLAUDE_PROJECT_NAME", "CLAUDE_PID",
                 "GT_RIG", "GT_ROLE", "GT_CREW"):
        env.pop(name, None)
    env["C3PO_MACHINE_NAME"] = "test-machine"
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


class TestAgentIdResolution:
    def test_explicit_id_is_sanitized(self, mock_coordinator, tmp_path):
        result = run_watch(watcher_env(tmp_path), "--print-id", "--agent-id", "Mac mini/Ansel Family")

        assert result.returncode == 0
        assert result.stdout.strip() == "Mac-mini/Ansel-Family"

    def test_sanitized_id_is_sent_to_coordinator(self, mock_coordinator, tmp_path):
        MockCoordinatorHandler.responses = [
            (200, {"count": 1, "status": "received", "messages": [
                {"id": "msg-1", "from_agent": "other/agent", "message": "hi"},
            ]}, {}),
        ]
        result = run_watch(watcher_env(tmp_path, mock_coordinator), "--agent-id", "test-machine/Ansel Family")

        assert result.returncode == 0
        assert MockCoordinatorHandler.received_requests[0]["headers"]["X-Machine-Name"] == "test-machine/Ansel-Family"

    def test_default_reads_session_agent_id_file(self, tmp_path):
        env = watcher_env(tmp_path)
        env["CLAUDE_CODE_SESSION_ID"] = "sess-123"
        with open(os.path.join(str(tmp_path), "c3po-agent-id-sess-123"), "w") as f:
            f.write("test-machine/proj-2")  # coordinator-assigned collision suffix

        result = run_watch(env, "--print-id")

        assert result.returncode == 0
        assert result.stdout.strip() == "test-machine/proj-2"

    def test_default_derives_from_cwd_without_session_file(self, tmp_path):
        env = watcher_env(tmp_path)
        env["CLAUDE_CODE_SESSION_ID"] = "sess-missing"
        project_dir = tmp_path / "Ansel Family"
        project_dir.mkdir()

        result = subprocess.run(
            [sys.executable, os.path.abspath(WATCH_SCRIPT), "--print-id"],
            capture_output=True, text=True, env=env, timeout=15, cwd=str(project_dir),
        )

        assert result.returncode == 0
        assert result.stdout.strip() == "test-machine/Ansel-Family"

    def test_default_honours_project_name_override(self, tmp_path):
        env = watcher_env(tmp_path)
        env["CLAUDE_PROJECT_NAME"] = "my project"

        result = run_watch(env, "--print-id")

        assert result.stdout.strip() == "test-machine/my-project"

    def test_stop_without_agent_id_uses_resolved_id(self, tmp_path):
        env = watcher_env(tmp_path)
        env["CLAUDE_CODE_SESSION_ID"] = "sess-123"
        with open(os.path.join(str(tmp_path), "c3po-agent-id-sess-123"), "w") as f:
            f.write(AGENT_ID)
        sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            with open(pid_file(tmp_path), "w") as f:
                f.write(str(sleeper.pid))

            result = run_watch(env, "--stop")

            assert result.returncode == 0
            assert f"Stopped watcher pid {sleeper.pid}" in result.stdout
            assert sleeper.wait(timeout=5) != 0
        finally:
            if sleeper.poll() is None:
                sleeper.kill()


def load_watch_module():
    loader = importlib.machinery.SourceFileLoader("c3po_watch", WATCH_SCRIPT)
    spec = importlib.util.spec_from_loader("c3po_watch", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def wait_until(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# A stand-in for the Claude session: it starts a shell (like run_in_background
# does) that runs the watcher, prints the shell's pid, then idles until killed.
FAKE_SESSION = r"""
import os, subprocess, sys, time
env = dict(os.environ)
if sys.argv[1] == "set-claude-pid":
    env["CLAUDE_PID"] = str(os.getpid())
out = open(sys.argv[2], "w")
cmd = '"$0" "$1" --agent-id "$2"; exit $?'  # not a lone command, so sh can't exec it
shell = subprocess.Popen(["sh", "-c", cmd, sys.argv[3], sys.argv[4], sys.argv[5]],
                         stdout=out, stderr=subprocess.STDOUT, env=env)
print(shell.pid, flush=True)
time.sleep(60)
"""


class TestOrphanDetection:
    def start_session(self, tmp_path, env, mode):
        out_path = str(tmp_path / "watch.out")
        session = subprocess.Popen(
            [sys.executable, "-c", FAKE_SESSION, mode, out_path,
             sys.executable, os.path.abspath(WATCH_SCRIPT), AGENT_ID],
            stdout=subprocess.PIPE, text=True, env=env,
        )
        shell_pid = int(session.stdout.readline())
        return session, shell_pid, out_path

    @pytest.mark.parametrize("mode", ["set-claude-pid", "no-claude-pid"])
    def test_exits_mid_poll_when_session_is_killed(self, mock_coordinator, tmp_path, mode):
        MockCoordinatorHandler.responses = [("HANG", None, None)]
        env = watcher_env(tmp_path, mock_coordinator)
        session, shell_pid, out_path = self.start_session(tmp_path, env, mode)
        try:
            assert wait_until(lambda: MockCoordinatorHandler.received_requests)
            with open(pid_file(tmp_path)) as f:
                watcher_pid = int(f.read())

            session.kill()  # the daemon reaping the session; the shell lives on
            session.wait(timeout=5)

            assert wait_until(lambda: not os.path.exists(pid_file(tmp_path)), timeout=5)
            assert wait_until(lambda: not pid_alive(watcher_pid), timeout=5)
            with open(out_path) as f:
                output = f.read()
            assert "C3PO_WATCH_ORPHANED" in output
            assert "C3PO_MESSAGES" not in output
            assert wait_until(lambda: not pid_alive(shell_pid), timeout=5)
        finally:
            if session.poll() is None:
                session.kill()
            for pid in (shell_pid,):
                if pid_alive(pid):
                    os.kill(pid, signal.SIGKILL)

    def test_live_session_still_gets_messages(self, mock_coordinator, tmp_path):
        # Several owner checks (0.1s apiece at the test sleep scale) run during the poll
        MockCoordinatorHandler.responses = [
            ("DELAY", 1.0, None),
            (200, {"count": 1, "status": "received", "messages": [
                {"id": "msg-1", "from_agent": "other/agent", "message": "hi"},
            ]}, {}),
        ]
        env = watcher_env(tmp_path, mock_coordinator)
        session, shell_pid, out_path = self.start_session(tmp_path, env, "set-claude-pid")
        try:
            assert wait_until(lambda: not pid_alive(shell_pid) or
                              "C3PO_MESSAGES" in open(out_path).read(), timeout=10)
            with open(out_path) as f:
                output = f.read()
            assert "C3PO_MESSAGES 1 pending" in output
            assert "C3PO_WATCH_ORPHANED" not in output
            assert "watching session pid %d" % session.pid in output
        finally:
            session.kill()
            session.wait(timeout=5)


class TestProcessHelpers:
    @pytest.fixture(params=["native", "ps"])
    def watch(self, request, monkeypatch):
        module = load_watch_module()
        if request.param == "ps":
            if os.path.exists("/proc/self/stat"):
                pytest.skip("ps is only the fallback where /proc is missing (macOS)")
            monkeypatch.setattr(module, "_has_proc", lambda: False)
        return module

    def test_parent_pid_of_self_and_dead(self, watch):
        assert watch.parent_pid_of(os.getpid()) == os.getppid()
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        assert watch.parent_pid_of(dead.pid) is None

    def test_command_of_self(self, watch):
        assert any("pytest" in a for a in watch.command_of(os.getpid()))

    def test_looks_like_claude(self, watch):
        assert watch.looks_like_claude(["claude", "bg-spare", "--bg-spare", "/tmp/x.sock"])
        assert watch.looks_like_claude(["/Users/me/.local/bin/claude", "daemon", "run"])
        assert watch.looks_like_claude(["node", "/usr/local/bin/claude", "--print"])
        assert watch.looks_like_claude(["node", "/usr/lib/node_modules/@anthropic-ai/claude-code/cli.js"])
        assert not watch.looks_like_claude(["/bin/zsh", "-c", "claude --version"])
        assert not watch.looks_like_claude([])

    def test_owner_chain_prefers_claude_pid(self, watch, monkeypatch):
        monkeypatch.setattr(watch, "ancestors", lambda: [30, 20, 10])
        monkeypatch.setattr(watch, "command_of", lambda pid: ["claude"] if pid == 10 else ["sh"])
        monkeypatch.setenv("CLAUDE_PID", "20")
        assert watch.owner_chain() == [30, 20]

    def test_owner_chain_falls_back_to_claude_ancestor_then_grandparent(self, watch, monkeypatch):
        monkeypatch.setattr(watch, "ancestors", lambda: [30, 20, 10])
        monkeypatch.setenv("CLAUDE_PID", "999")  # not an ancestor: ignored
        monkeypatch.setattr(watch, "command_of", lambda pid: ["claude"] if pid == 10 else ["sh"])
        assert watch.owner_chain() == [30, 20, 10]
        monkeypatch.setattr(watch, "command_of", lambda pid: ["sh"])
        assert watch.owner_chain() == [30, 20]

    def test_owner_gone_checks_every_link(self, watch, monkeypatch):
        ppid = os.getppid()
        links = {ppid: 20}
        monkeypatch.setattr(watch, "parent_pid_of", lambda pid: links.get(pid))
        assert not watch.owner_gone([ppid, 20])
        links[ppid] = 1  # reparented to init: the session died
        assert watch.owner_gone([ppid, 20])
        assert watch.owner_gone([ppid + 1])  # our own parent changed
        assert not watch.owner_gone([])
