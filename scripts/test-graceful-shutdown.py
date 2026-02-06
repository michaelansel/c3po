#!/usr/bin/env python3
"""
Test graceful shutdown of the C3PO coordinator.

Mode 1 (local): Starts a local coordinator subprocess, connects via MCP,
sends SIGTERM, and verifies the client receives a retry response.

Mode 2 (remote): Connects to a remote coordinator, waits for an external
restart, and reports the result.

Usage:
    # Local test (self-contained, starts its own coordinator + redis):
    python3 scripts/test-graceful-shutdown.py

    # Remote test (connect to existing coordinator, restart manually):
    python3 scripts/test-graceful-shutdown.py --remote https://mcp.qerk.be

    # Remote test using credentials from c3po-credentials.json:
    python3 scripts/test-graceful-shutdown.py --remote
"""

import argparse
import asyncio
import json
import os
import signal
import subprocess
import sys
import time


CREDS_PATH = os.environ.get(
    "C3PO_CREDENTIALS", os.path.expanduser("~/.claude/c3po-credentials.json")
)


def wait_for_health(url: str, timeout: float = 15) -> bool:
    """Poll the health endpoint until it responds or timeout."""
    import urllib.request
    import urllib.error
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            resp = urllib.request.urlopen(f"{url}/api/health", timeout=2)
            if resp.status == 200:
                return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.3)
    return False


async def run_mcp_test(coordinator_url: str, headers: dict, sigterm_pid: int | None = None):
    """Connect to coordinator, call wait_for_message, optionally send SIGTERM, check result."""
    from mcp.client.streamable_http import streamablehttp_client
    from mcp import ClientSession

    mcp_url = f"{coordinator_url}/mcp"
    if "/agent/" in coordinator_url or headers.get("Authorization"):
        # For remote with auth, use /agent/mcp
        mcp_url = f"{coordinator_url.rstrip('/')}/agent/mcp" if "/agent/" not in coordinator_url else coordinator_url

    # For local (no auth), just use /mcp
    if not headers.get("Authorization"):
        mcp_url = f"{coordinator_url}/mcp"

    print(f"  Connecting to {mcp_url}...")
    async with streamablehttp_client(mcp_url, headers=headers) as (read_stream, write_stream, _):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            print(f"  Connected. Calling wait_for_message(timeout=30)...")

            if sigterm_pid:
                # Schedule SIGTERM after a short delay
                async def send_sigterm():
                    await asyncio.sleep(2)
                    print(f"  Sending SIGTERM to PID {sigterm_pid}...")
                    os.kill(sigterm_pid, signal.SIGTERM)
                sigterm_task = asyncio.create_task(send_sigterm())

            t0 = time.monotonic()
            try:
                result = await asyncio.wait_for(
                    session.call_tool("wait_for_message", {"timeout": 30}),
                    timeout=25,
                )
            except asyncio.TimeoutError:
                elapsed = time.monotonic() - t0
                print(f"\n  Client-side timeout after {elapsed:.1f}s (no response received)")
                return {"status": "client_timeout", "elapsed": elapsed}
            except Exception as e:
                elapsed = time.monotonic() - t0
                print(f"\n  Exception after {elapsed:.1f}s: {type(e).__name__}: {e}")
                return {"status": "error", "error": str(e), "elapsed": elapsed}

            elapsed = time.monotonic() - t0

            # Parse result
            data = None
            for block in result.content:
                if hasattr(block, "text"):
                    try:
                        data = json.loads(block.text)
                    except json.JSONDecodeError:
                        data = block.text
                    break
            if data is None:
                data = str(result.content)

            print(f"\n  Response after {elapsed:.1f}s:")
            print(f"  {json.dumps(data, indent=2) if isinstance(data, dict) else data}")
            return data


def run_local_test():
    """Start a local coordinator, test graceful shutdown."""
    import shutil

    project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    # Check redis is running
    print("[1/5] Checking Redis...")
    docker_cmd = "finch" if shutil.which("finch") else "docker"
    redis_check = subprocess.run(
        [docker_cmd, "ps", "--filter", "name=c3po-test-redis", "--format", "{{.Names}}"],
        capture_output=True, text=True
    )
    if "c3po-test-redis" not in redis_check.stdout:
        print("  Starting Redis container...")
        subprocess.run(
            [docker_cmd, "run", "-d", "--name", "c3po-test-redis", "-p", "6379:6379", "redis:7-alpine"],
            capture_output=True
        )
        time.sleep(1)
    print("  Redis OK")

    # Start coordinator
    print("[2/5] Starting coordinator...")
    env = os.environ.copy()
    env["REDIS_URL"] = "redis://localhost:6379"
    env["PYTHONPATH"] = project_dir
    env["C3PO_PORT"] = "8420"

    coord_proc = subprocess.Popen(
        [sys.executable, os.path.join(project_dir, "coordinator", "server.py")],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    print(f"  Coordinator PID: {coord_proc.pid}")

    if not wait_for_health("http://localhost:8420"):
        out = coord_proc.stdout.read(4096).decode() if coord_proc.stdout else ""
        print(f"  ERROR: Coordinator failed to start.\n{out}")
        coord_proc.kill()
        return 1

    print("  Coordinator healthy")

    # Run the MCP test
    print("[3/5] Connecting MCP client and calling wait_for_message...")
    headers = {"X-Machine-Name": "shutdown-test", "X-Project-Name": "graceful"}

    try:
        result = asyncio.run(
            run_mcp_test("http://localhost:8420", headers, sigterm_pid=coord_proc.pid)
        )
    except Exception as e:
        print(f"  ERROR: {type(e).__name__}: {e}")
        result = {"status": "error", "error": str(e)}

    # Check result
    print("[4/5] Checking result...")
    coord_proc.wait(timeout=5)
    exit_code = coord_proc.returncode
    print(f"  Coordinator exited with code {exit_code}")

    # Read coordinator logs
    if coord_proc.stdout:
        remaining = coord_proc.stdout.read().decode()
        if remaining:
            print(f"  Coordinator output (last 500 chars):\n  {'  '.join(remaining[-500:].splitlines(True))}")

    print("[5/5] Verdict...")
    if isinstance(result, dict) and result.get("status") == "retry":
        print(f"  PASS: Got retry response")
        return 0
    elif isinstance(result, dict) and result.get("status") == "timeout":
        print(f"  FAIL: Got server-side timeout (SIGTERM wasn't detected)")
        return 1
    elif isinstance(result, dict) and result.get("status") == "client_timeout":
        print(f"  FAIL: Client timed out waiting for response ({result.get('elapsed', '?')}s)")
        return 1
    else:
        print(f"  FAIL: Unexpected result: {result}")
        return 1


def run_remote_test(coordinator_url: str | None = None):
    """Connect to a remote coordinator, wait for external restart."""
    if coordinator_url is None:
        # Read from credentials
        with open(CREDS_PATH) as f:
            creds = json.load(f)
        coordinator_url = creds["coordinator_url"]
        token = creds["api_token"]
        agent_pattern = creds.get("agent_pattern", "*/shutdown-test")
        machine = agent_pattern.split("/")[0] if "/" in agent_pattern else "shutdown-test"
    else:
        token = None
        machine = "shutdown-test"

    headers = {"X-Machine-Name": machine, "X-Project-Name": "shutdown-test"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
        mcp_url = f"{coordinator_url}/agent/mcp"
    else:
        mcp_url = f"{coordinator_url}/mcp"

    print(f"Connecting to {coordinator_url}...")
    print(">>> Restart the coordinator now (within 30s) <<<")

    try:
        result = asyncio.run(run_mcp_test(coordinator_url, headers))
    except Exception as e:
        print(f"ERROR: {type(e).__name__}: {e}")
        return 1

    if isinstance(result, dict) and result.get("status") == "retry":
        print(f"\nPASS: Got retry response")
        return 0
    else:
        print(f"\nFAIL: {result}")
        return 1


def main():
    parser = argparse.ArgumentParser(description="Test graceful shutdown of C3PO coordinator")
    parser.add_argument("--remote", nargs="?", const="", default=None,
                        help="Test against remote coordinator (URL or empty for credentials file)")
    args = parser.parse_args()

    if args.remote is not None:
        url = args.remote if args.remote else None
        return run_remote_test(url)
    else:
        return run_local_test()


if __name__ == "__main__":
    sys.exit(main())
