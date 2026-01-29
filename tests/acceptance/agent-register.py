#!/usr/bin/env python3
"""
Minimal agent that registers with the coordinator and stays online.
Used for acceptance testing without requiring full Claude Code installation.

Uses the MCP client library for proper protocol handling.
"""

import asyncio
import json
import os
import sys
import urllib.request
import urllib.error

COORDINATOR_URL = os.environ.get("C3PO_COORDINATOR_URL", "http://localhost:8420")
AGENT_ID = os.environ.get("C3PO_AGENT_ID", "test-agent")


def log(msg):
    print(f"[{AGENT_ID}] {msg}", flush=True)


def wait_for_health(max_attempts=30):
    """Wait for coordinator to be ready via REST API."""
    for i in range(max_attempts):
        try:
            req = urllib.request.Request(f"{COORDINATOR_URL}/api/health")
            with urllib.request.urlopen(req, timeout=5) as resp:
                health = json.loads(resp.read())
                if health.get("status") == "ok":
                    log("Coordinator is ready")
                    return True
        except Exception:
            pass
        import time
        time.sleep(1)
    return False


async def run_agent():
    """Run the agent using the MCP client library."""
    try:
        from mcp.client.streamable_http import streamablehttp_client
        from mcp import ClientSession
    except ImportError as e:
        log(f"MCP client library not available: {e}")
        log("Falling back to REST-only mode (agent registration via health checks)")
        # Can't register without MCP library, but we can still check health
        while True:
            await asyncio.sleep(30)
        return

    url = f"{COORDINATOR_URL}/mcp"
    headers = {"X-Agent-ID": AGENT_ID, "X-Project-Name": "acceptance-test"}

    try:
        async with streamablehttp_client(url, headers=headers) as (read_stream, write_stream, _):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                log("Connected to coordinator")

                # Ping to register
                result = await session.call_tool("ping", {})
                log(f"Registered successfully: {result}")

                # Keep alive by pinging periodically
                log("Staying online (Ctrl+C to stop)...")
                while True:
                    await asyncio.sleep(30)
                    try:
                        result = await session.call_tool("ping", {})
                        log("Heartbeat sent")
                    except Exception as e:
                        log(f"Heartbeat failed: {e}")

    except Exception as e:
        log(f"MCP connection failed: {e}")
        sys.exit(1)


def main():
    log(f"Starting agent, connecting to {COORDINATOR_URL}")

    # Wait for coordinator to be ready
    if not wait_for_health():
        log("Coordinator not ready after 30 seconds")
        sys.exit(1)

    # Run the async agent
    asyncio.run(run_agent())


if __name__ == "__main__":
    main()
