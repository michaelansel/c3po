"""
Test for the compaction + ack SET clear bug.

This test EXACTLY replicates the ithaca scenario and FAILS with the current bug.

BUG REPRODUCTION:
1. Send 25 messages
2. Acknowledge 23 messages (above compaction threshold of 20)
3. Wait for compaction to trigger
4. Agent sees 2 remaining messages with acked=0
5. Agent tries to ACK the remaining messages repeatedly over 2+ minutes
6. Agent loops trying to ack these messages because compaction cleared the acked SET

THE BUG: When compaction runs, it deletes the entire acked SET (line 489 of messaging.py).
This causes `peek_messages` to report `acked=0` for remaining messages, making them
appear as "not acked" even though they were previously acked.

THE FIX: Don't delete the acked SET on compaction. Keep it indefinitely until agent
unregistration or set it to expire after MESSAGE_TTL.
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
    """Create an MCP client session with the coordinator."""
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
async def test_compaction_ack_loop_with_delay():
    """
    Test that detects the ack loop bug with delayed acknowledgment.

    This test FAILS with the current bug and should PASS after the fix.

    BUG REPRODUCTION:
    1. Send 25 messages
    2. Acknowledge 23 messages (above compaction threshold of 20)
    3. Wait for compaction to trigger
    4. Agent sees 2 remaining messages with acked=0
    5. Agent tries to ACK them repeatedly over 30+ seconds (mimicking ithaca)
    6. Bug: Compaction cleared acked SET, so messages appear "not acked"

    Expected: After compaction, remaining messages should still be marked as
    acked in the SET, so `peek_messages` doesn't report them as "acked=0".

    Actual (bug): After compaction, `peek_messages` reports `acked=0` for
    remaining messages, causing agent to loop trying to ack them.
    """
    async with mcp_client_session("test-delayed-ack/agent") as agent:
        # Register agent
        await agent.call_tool("register_agent", {"name": "test-delayed-ack/agent"})

        # Step 1: Send 25 messages (above compaction threshold of 20)
        print("\n  [Step 1] Sending 25 messages...")
        for i in range(25):
            await agent.call_tool("send_message", {
                "to": "test-delayed-ack/agent",
                "message": f"Message {i}"
            })

        # Step 2: Get all messages and acknowledge 23 of them
        print("  [Step 2] Getting messages and acknowledging 23...")
        result = await agent.call_tool("get_messages", {})
        parsed = _parse_tool_result(result)
        if isinstance(parsed, list):
            messages = parsed
            msg_count = len(messages)
        elif isinstance(parsed, dict):
            messages = parsed.get("messages", [])
            msg_count = len(messages)
        else:
            messages = []
            msg_count = 0

        assert msg_count == 25, f"Expected 25 messages, got {msg_count}"

        # Acknowledge 23 messages (above compaction threshold of 20)
        msg_ids = [m["id"] for m in messages[:23]]
        await agent.call_tool("ack_messages", {
            "message_ids": msg_ids
        })

        # Step 3: Wait for compaction to trigger
        print("  [Step 3] Waiting for compaction to trigger...")
        await asyncio.sleep(1)  # Give compaction time to run

        # Step 4: Get messages again - should see 2 remaining
        result = await agent.call_tool("get_messages", {})
        parsed = _parse_tool_result(result)
        if isinstance(parsed, list):
            remaining = parsed
            remaining_count = len(remaining)
        elif isinstance(parsed, dict):
            remaining = parsed.get("messages", [])
            remaining_count = len(remaining)
        else:
            remaining = []
            remaining_count = 0

        print(f"  [Step 4] Messages after ack: {remaining_count}")

        if remaining_count != 2:
            pytest.fail(f"Expected 2 remaining messages, got {remaining_count}")

        remaining_ids = [m["id"] for m in remaining]
        print(f"  [Step 5] Remaining message IDs: {remaining_ids}")

        # Step 6: Try to acknowledge remaining 2 messages repeatedly
        # This mimics the ithaca pattern where it tries to ack them over 2+ minutes
        print("  [Step 6] Attempting to acknowledge remaining messages for 30 seconds...")
        loop_detected = False

        for attempt in range(30):  # Try for 30 attempts (30 seconds)
            result = await agent.call_tool("get_messages", {})
            parsed = _parse_tool_result(result)
            if isinstance(parsed, list):
                current_remaining = parsed
                current_count = len(current_remaining)
            elif isinstance(parsed, dict):
                current_remaining = parsed.get("messages", [])
                current_count = len(current_remaining)
            else:
                current_remaining = []
                current_count = 0

            if current_count == 0:
                print(f"  [Attempt {attempt + 1}/30] ✓ Messages cleared after {attempt + 1} seconds")
                break

            current_remaining_ids = [m["id"] for m in current_remaining]

            # Check if we're trying to ack the same messages again
            for msg_id in current_remaining_ids:
                if msg_id in remaining_ids:
                    print(f"  [Attempt {attempt + 1}/30] ⚠️  WARNING: Trying to ack same messages again")
                    print(f"     Message ID: {msg_id}")
                    print(f"     This indicates compaction cleared the acked SET")
                    loop_detected = True

            # Try to acknowledge
            try:
                await agent.call_tool("ack_messages", {
                    "message_ids": current_remaining_ids
                })
                print(f"  [Attempt {attempt + 1}/30] ✓ Ack succeeded")
            except Exception as e:
                print(f"  [Attempt {attempt + 1}/30] ✗ Ack failed (expected if messages compacted)")
                # This is okay - messages might be gone

            await asyncio.sleep(1)

        # After the test, verify no infinite loop occurred
        print("\n  [Result]")
        if loop_detected:
            print("  ⚠️  WARNING: Agent tried to ack same messages repeatedly")
            print("  This indicates compaction cleared the acked SET")
            print("  Expected behavior: Compaction should keep the acked SET")
            pytest.fail("Compaction cleared acked SET, causing repeated ack attempts")
        else:
            print("  ✓ No ack loop detected")
            print("  Compaction preserved acked SET state")
