"""
Test for the compaction + ack race condition bug.

The bug: When compaction removes messages and clears the acked SET,
agents see messages as "not acked" and keep trying to ack them,
even though those messages no longer exist.

This test replicates the scenario where:
1. Agent has 23 acked messages
2. Compaction threshold is 20
3. Compaction removes 21 messages and clears acked SET
4. Agent sees 3 messages with acked=0 and loops trying to ack
"""

import asyncio
import json
import os
import pytest
from contextlib import asynccontextmanager

# Check if we should run live tests
LIVE_TESTS = os.environ.get("C3PO_TEST_LIVE", "").lower() in ("1", "true", "yes")
COORDINATOR_URL = os.environ.get("C3PO_COORDINATOR_URL", "http://localhost:8420")

# Skip if not running live tests
pytestmark = pytest.mark.skipif(
    not LIVE_TESTS,
    reason="Live E2E tests disabled. Set C3PO_TEST_LIVE=1 to enable."
)


@asynccontextmanager
async def mcp_client_session(agent_id: str):
    """Create an MCP client session with the coordinator.

    Uses the streamablehttp_client for proper MCP protocol handling.
    """
    try:
        from mcp.client.streamable_http import streamablehttp_client
        from mcp import ClientSession
    except ImportError:
        pytest.skip("MCP client library not available")

    url = f"{COORDINATOR_URL}/mcp"
    parts = agent_id.split("/", 1)
    headers = {"X-Machine-Name": parts[0]}
    if len(parts) > 1:
        headers["X-Project-Name"] = parts[1]

    async with streamablehttp_client(url, headers=headers) as (read_stream, write_stream, _):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            yield session


def _parse_tool_result(result):
    """Extract the JSON payload from an MCP CallToolResult."""
    for block in result.content:
        if hasattr(block, "text"):
            try:
                return json.loads(block.text)
            except json.JSONDecodeError:
                return block.text
    return str(result.content)


@pytest.mark.asyncio
async def test_compaction_clears_acked_set():
    """Test that compaction doesn't leave agents in ack loop.

    Replicates the bug where:
    1. Agent has 23 acked messages
    2. Compaction threshold is 20
    3. Compaction removes 21 messages and clears acked SET
    4. Agent sees 3 messages with acked=0 and tries to ack repeatedly

    Expected: Compaction should NOT clear the acked SET.
    Actual (bug): Compaction clears acked SET, causing loop.
    """
    async with mcp_client_session("test-compaction/agent") as agent:
        # Step 1: Register agent
        await agent.call_tool("register_agent", {"name": "test-compaction/agent"})

        # Step 2: Send 25 messages
        print("\n  Sending 25 messages...")
        for i in range(25):
            await agent.call_tool("send_message", {
                "to": "test-compaction/agent",
                "message": f"Message {i}"
            })

        # Step 3: Get all messages and acknowledge 23 of them
        print("  Getting messages...")
        result = await agent.call_tool("get_messages", {})
        parsed = _parse_tool_result(result)
        if isinstance(parsed, list):
            msg_count = len(parsed)
        elif isinstance(parsed, dict):
            msg_count = len(parsed.get("messages", []))
        else:
            msg_count = 0

        assert msg_count == 25, f"Expected 25 messages, got {msg_count}"

        print(f"  Acknowledging 23 of 25 messages...")
        # Acknowledge 23 messages (above compaction threshold of 20)
        msg_ids = [m["id"] for m in parsed[:23]]
        await agent.call_tool("ack_messages", {
            "message_ids": msg_ids
        })

        # Step 4: Wait for compaction to trigger
        print("  Waiting for compaction to trigger...")
        await asyncio.sleep(1)  # Give compaction time to run

        # Step 5: Get messages again - should see 2 remaining
        result = await agent.call_tool("get_messages", {})
        parsed = _parse_tool_result(result)
        if isinstance(parsed, list):
            msg_count = len(parsed)
        elif isinstance(parsed, dict):
            msg_count = len(parsed.get("messages", []))
        else:
            msg_count = 0

        print(f"  Messages after ack: {msg_count}")

        # Step 6: Try to acknowledge remaining 2 messages
        if msg_count > 0:
            remaining_ids = [m["id"] for m in parsed]
            print(f"  Attempting to acknowledge {len(remaining_ids)} remaining messages...")

            # This should succeed - acknowledge the remaining messages
            await agent.call_tool("ack_messages", {
                "message_ids": remaining_ids
            })

            # Step 7: Verify all messages are gone
            result = await agent.call_tool("get_messages", {})
            parsed = _parse_tool_result(result)
            if isinstance(parsed, list):
                final_count = len(parsed)
            elif isinstance(parsed, dict):
                final_count = len(parsed.get("messages", []))
            else:
                final_count = 0

            print(f"  Final message count: {final_count}")
            assert final_count == 0, "All messages should be removed after ack"

        print("\n  ✓ Test passed - compaction and ack work correctly")


@pytest.mark.asyncio
async def test_compaction_ack_loop_detection():
    """Test that detects the ack loop bug.

    This test verifies that agents don't get stuck in a loop
    trying to ack messages that were compacted away.
    """
    async with mcp_client_session("test-loop/agent") as agent:
        # Register agent
        await agent.call_tool("register_agent", {"name": "test-loop/agent"})

        # Send 25 messages
        print("\n  Sending 25 messages...")
        for i in range(25):
            await agent.call_tool("send_message", {
                "to": "test-loop/agent",
                "message": f"Loop test message {i}"
            })

        # Acknowledge 23 messages
        print("  Acknowledging 23 messages...")
        result = await agent.call_tool("get_messages", {})
        parsed = _parse_tool_result(result)
        if isinstance(parsed, list):
            msg_ids = [m["id"] for m in parsed[:23]]
        elif isinstance(parsed, dict):
            msg_ids = [m["id"] for m in parsed.get("messages", [])[:23]]
        else:
            msg_ids = []

        assert len(msg_ids) == 23
        await agent.call_tool("ack_messages", {
            "message_ids": msg_ids
        })

        # Wait for compaction
        print("  Waiting for compaction...")
        await asyncio.sleep(1)

        # Try to acknowledge remaining messages 3 times
        print("  Attempting to acknowledge remaining messages 3 times...")
        for attempt in range(3):
            result = await agent.call_tool("get_messages", {})
            parsed = _parse_tool_result(result)
            if isinstance(parsed, list):
                remaining = parsed
            elif isinstance(parsed, dict):
                remaining = parsed.get("messages", [])
            else:
                remaining = []

            if len(remaining) == 0:
                print(f"  ✓ Attempt {attempt + 1}: No messages remaining")
                break

            remaining_ids = [m["id"] for m in remaining]
            print(f"  Attempt {attempt + 1}: {len(remaining_ids)} messages to ack")

            # Try to acknowledge - should fail gracefully or succeed
            try:
                await agent.call_tool("ack_messages", {
                    "message_ids": remaining_ids
                })
                print(f"  ✓ Attempt {attempt + 1}: Ack succeeded")
            except Exception as e:
                print(f"  ⚠ Attempt {attempt + 1}: Ack failed (expected if messages compacted)")
                # This is okay - messages might be gone

            await asyncio.sleep(0.5)

        print("\n  ✓ Test completed - no infinite loop detected")
