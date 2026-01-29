#!/usr/bin/env python3
"""
Test agent communication through the C3PO coordinator.

Uses the MCP client library for proper protocol handling.
This test creates two agents and tests the full request/response cycle.
"""

import asyncio
import json
import os
import sys

COORDINATOR_URL = os.environ.get("C3PO_COORDINATOR_URL", "http://localhost:18420")


def log(msg):
    print(f"\033[0;32m[comm-test]\033[0m {msg}", flush=True)


def error(msg):
    print(f"\033[0;31m[comm-test]\033[0m {msg}", file=sys.stderr, flush=True)


async def run_test():
    """Run the communication test using MCP client library."""
    try:
        from mcp.client.streamable_http import streamablehttp_client
        from mcp import ClientSession
    except ImportError as e:
        error(f"MCP client library not available: {e}")
        sys.exit(1)

    url = f"{COORDINATOR_URL}/mcp"

    # Use unique agent IDs for this test to avoid collision with persistent agents
    # Headers use base ID; middleware constructs full ID as base/project
    alice_base = "test-alice"
    bob_base = "test-bob"
    project = "acceptance-test"
    alice_id = f"{alice_base}/{project}"
    bob_id = f"{bob_base}/{project}"

    # Generate unique session IDs to ensure consistent agent identity across tool calls
    import uuid
    alice_session = str(uuid.uuid4())
    bob_session = str(uuid.uuid4())

    alice_headers = {"X-Agent-ID": alice_base, "X-Project-Name": project, "X-Session-ID": alice_session}
    bob_headers = {"X-Agent-ID": bob_base, "X-Project-Name": project, "X-Session-ID": bob_session}

    # We need to keep sessions open for the full test, so we'll nest them
    log(f"Creating sessions for {alice_id} and {bob_id}...")

    async with streamablehttp_client(url, headers=alice_headers) as (read_a, write_a, _):
        async with ClientSession(read_a, write_a) as session_alice:
            await session_alice.initialize()
            log(f"✓ {alice_id} connected")

            async with streamablehttp_client(url, headers=bob_headers) as (read_b, write_b, _):
                async with ClientSession(read_b, write_b) as session_bob:
                    await session_bob.initialize()
                    # Ping to register bob (agent registration happens on first tool call)
                    await session_bob.call_tool("ping", {})
                    log(f"✓ {bob_id} connected and registered")

                    # Test 1: List agents - both should see each other
                    log("Test 1: Listing agents...")
                    result = await session_alice.call_tool("list_agents", {})
                    result_text = str(result)
                    print(result_text)

                    if bob_id not in result_text:
                        error(f"{alice_id} cannot see {bob_id}")
                        return 1
                    log(f"✓ {alice_id} can see {bob_id}")

                    # Test 2: Alice sends request to Bob
                    log(f"Test 2: {alice_id} sending request to {bob_id}...")
                    result = await session_alice.call_tool("send_request", {
                        "target": bob_id,
                        "message": "What is 2+2?"
                    })
                    result_text = str(result)
                    print(result_text)

                    # Parse the request_id from the result
                    import re
                    match = re.search(r'"id":\s*"([^"]+)"', result_text)
                    if not match:
                        error("Failed to get request ID")
                        return 1
                    request_id = match.group(1)
                    log(f"✓ Request sent with ID: {request_id}")

                    # Test 3: Bob receives the request
                    log(f"Test 3: {bob_id} checking pending requests...")
                    result = await session_bob.call_tool("get_pending_requests", {})
                    result_text = str(result)
                    print(result_text)

                    if "What is 2+2" not in result_text:
                        error(f"{bob_id} did not receive the request")
                        return 1
                    log(f"✓ {bob_id} received the request")

                    # Test 4: Bob responds
                    log(f"Test 4: {bob_id} responding...")
                    result = await session_bob.call_tool("respond_to_request", {
                        "request_id": request_id,
                        "response": "The answer is 4"
                    })
                    result_text = str(result)
                    print(result_text)

                    if "success" not in result_text.lower():
                        error(f"{bob_id} failed to respond")
                        return 1
                    log(f"✓ {bob_id} sent response")

                    # Test 5: Alice receives the response
                    log(f"Test 5: {alice_id} waiting for response...")
                    result = await session_alice.call_tool("wait_for_response", {
                        "request_id": request_id,
                        "timeout": 10
                    })
                    result_text = str(result)
                    print(result_text)

                    if "The answer is 4" not in result_text:
                        error(f"{alice_id} did not receive the response")
                        return 1
                    log(f"✓ {alice_id} received response: 'The answer is 4'")

    log("=== Communication test passed! ===")
    return 0


def main():
    exit_code = asyncio.run(run_test())
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
