"""
End-to-end integration tests for C3PO coordinator.

These tests simulate the full request/response flow between agents
using the actual MCP protocol via the streamablehttp client.

Note: These tests require the coordinator to be running.
Run with: pytest tests/test_e2e_integration.py -v

To run against a live coordinator:
    export C3PO_TEST_LIVE=1
    ./scripts/test-local.sh start
    pytest tests/test_e2e_integration.py -v

To run only latency tests:
    C3PO_TEST_LIVE=1 pytest tests/test_e2e_integration.py -v -k latency
"""

import asyncio
import json
import os
import time
import pytest
import pytest_asyncio
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
    # The coordinator reads X-Machine-Name and X-Project-Name, not X-Agent-ID.
    # Agent IDs are formatted as "machine/project".
    parts = agent_id.split("/", 1)
    headers = {"X-Machine-Name": parts[0]}
    if len(parts) > 1:
        headers["X-Project-Name"] = parts[1]

    async with streamablehttp_client(url, headers=headers) as (read_stream, write_stream, _):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            yield session


# ---------------------------------------------------------------------------
# Helpers for parsing MCP tool results
# ---------------------------------------------------------------------------

def _parse_tool_result(result) -> dict | list | str:
    """Extract the JSON payload from an MCP CallToolResult.

    FastMCP wraps tool returns in TextContent blocks. This grabs the
    first text block and parses it as JSON, falling back to raw text.
    """
    for block in result.content:
        if hasattr(block, "text"):
            try:
                return json.loads(block.text)
            except json.JSONDecodeError:
                return block.text
    return str(result.content)


# ---------------------------------------------------------------------------
# Functional tests
# ---------------------------------------------------------------------------

class TestE2EIntegration:
    """End-to-end integration tests."""

    @pytest.mark.asyncio
    async def test_ping_tool(self):
        """Test the ping tool returns expected response."""
        async with mcp_client_session("e2e/ping") as session:
            result = await session.call_tool("ping", {})
            assert "pong" in str(result)

    @pytest.mark.asyncio
    async def test_agent_registration_via_list(self):
        """Test that connecting registers the agent."""
        async with mcp_client_session("e2e/list") as session:
            result = await session.call_tool("list_agents", {})
            assert result is not None

    @pytest.mark.asyncio
    async def test_send_and_receive_message(self):
        """Test sending a message from one agent to another."""
        # Register agent-b first
        async with mcp_client_session("e2e/agent-b") as session_b:
            await session_b.call_tool("ping", {})

        # Send message from agent-a
        async with mcp_client_session("e2e/agent-a") as session_a:
            send_result = await session_a.call_tool("send_message", {
                "to": "e2e/agent-b",
                "message": "Hello from E2E test!",
                "context": "Integration test"
            })
            assert send_result is not None

    @pytest.mark.asyncio
    async def test_full_message_reply_cycle(self):
        """Test complete message/reply cycle between two agents."""
        # Step 1: Register both agents
        async with mcp_client_session("e2e/sender") as sender:
            await sender.call_tool("ping", {})

        async with mcp_client_session("e2e/receiver") as receiver:
            await receiver.call_tool("ping", {})

        # Step 2: Sender sends message
        async with mcp_client_session("e2e/sender") as sender:
            send_result = await sender.call_tool("send_message", {
                "to": "e2e/receiver",
                "message": "E2E test message"
            })
            assert send_result is not None

        # Step 3: Receiver gets messages
        async with mcp_client_session("e2e/receiver") as receiver:
            pending = await receiver.call_tool("get_messages", {})
            assert pending is not None

    @pytest.mark.asyncio
    async def test_wait_for_message_timeout(self):
        """Test that wait_for_message times out correctly."""
        async with mcp_client_session("e2e/timeout") as session:
            result = await session.call_tool("wait_for_message", {
                "timeout": 2
            })
            # Should return timeout status
            assert result is not None


class TestRESTEndpoints:
    """Test REST API endpoints directly (no MCP session needed)."""

    @pytest.mark.asyncio
    async def test_health_endpoint(self):
        """Test /api/health endpoint."""
        import httpx

        async with httpx.AsyncClient() as client:
            response = await client.get(f"{COORDINATOR_URL}/api/health")
            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "ok"
            assert "agents_online" in data

    @pytest.mark.asyncio
    async def test_pending_endpoint_without_header(self):
        """Test /agent/api/pending without X-Machine-Name header returns error."""
        import httpx

        async with httpx.AsyncClient() as client:
            response = await client.get(f"{COORDINATOR_URL}/agent/api/pending")
            assert response.status_code == 400
            data = response.json()
            assert "error" in data

    @pytest.mark.asyncio
    async def test_pending_endpoint_with_header(self):
        """Test /agent/api/pending with X-Machine-Name header."""
        import httpx

        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{COORDINATOR_URL}/agent/api/pending",
                headers={"X-Machine-Name": "e2e/rest-test"}
            )
            assert response.status_code == 200
            data = response.json()
            assert "count" in data
            assert "messages" in data


# ---------------------------------------------------------------------------
# Latency / performance tests
# ---------------------------------------------------------------------------

class TestLatency:
    """Latency and throughput tests against a live coordinator.

    Run with:
        C3PO_TEST_LIVE=1 pytest tests/test_e2e_integration.py -v -k latency
    """

    @pytest.mark.asyncio
    async def test_latency_ping_round_trip(self):
        """Ping round-trip should be well under 500ms."""
        async with mcp_client_session("latency/ping") as session:
            latencies = []
            for _ in range(10):
                t0 = time.perf_counter()
                await session.call_tool("ping", {})
                latencies.append((time.perf_counter() - t0) * 1000)

            avg = sum(latencies) / len(latencies)
            p95 = sorted(latencies)[int(len(latencies) * 0.95)]
            print(f"\n  Ping: avg={avg:.1f}ms  p95={p95:.1f}ms  "
                  f"min={min(latencies):.1f}ms  max={max(latencies):.1f}ms")
            assert avg < 500, f"Ping avg {avg:.1f}ms exceeds 500ms"

    @pytest.mark.asyncio
    async def test_latency_send_message(self):
        """send_message round-trip through MCP."""
        async with mcp_client_session("latency/sender") as session:
            await session.call_tool("register_agent", {
                "name": "latency/sender",
            })

            latencies = []
            for i in range(10):
                t0 = time.perf_counter()
                await session.call_tool("send_message", {
                    "to": "latency/sink",
                    "message": f"perf-{i}",
                })
                latencies.append((time.perf_counter() - t0) * 1000)

            avg = sum(latencies) / len(latencies)
            p95 = sorted(latencies)[int(len(latencies) * 0.95)]
            print(f"\n  send_message: avg={avg:.1f}ms  p95={p95:.1f}ms  "
                  f"min={min(latencies):.1f}ms  max={max(latencies):.1f}ms")
            assert avg < 1000, f"send_message avg {avg:.1f}ms exceeds 1s"

    @pytest.mark.asyncio
    async def test_latency_send_then_receive(self):
        """Full send → wait_for_message → ack cycle between two sessions.

        Both sessions must stay open concurrently: the receiver holds a
        blocking wait while the sender fires a message.
        """
        latencies = []

        for i in range(5):
            async with mcp_client_session("latency/receiver") as receiver:
                await receiver.call_tool("register_agent", {
                    "name": "latency/receiver",
                })

                # Start wait in a background task (session stays open)
                wait_task = asyncio.create_task(
                    receiver.call_tool("wait_for_message", {"timeout": 10})
                )
                await asyncio.sleep(0.3)

                # Send from a second concurrent session
                async with mcp_client_session(f"latency/sender-{i}") as sender:
                    await sender.call_tool("register_agent", {
                        "name": f"latency/sender-{i}",
                    })
                    t0 = time.perf_counter()
                    await sender.call_tool("send_message", {
                        "to": "latency/receiver",
                        "message": f"latency-{i}",
                    })

                # Collect the wait result (receiver session still open)
                result = await asyncio.wait_for(wait_task, timeout=10)
                latency = (time.perf_counter() - t0) * 1000
                latencies.append(latency)

                # Ack so next round is clean
                parsed = _parse_tool_result(result)
                if isinstance(parsed, dict) and parsed.get("status") == "received":
                    msg_ids = [m["id"] for m in parsed.get("messages", [])
                               if "id" in m]
                    if msg_ids:
                        await receiver.call_tool("ack_messages", {
                            "message_ids": msg_ids,
                        })

        avg = sum(latencies) / len(latencies)
        p95 = sorted(latencies)[int(len(latencies) * 0.95)]
        print(f"\n  send→receive: avg={avg:.1f}ms  p95={p95:.1f}ms  "
              f"min={min(latencies):.1f}ms  max={max(latencies):.1f}ms")
        assert avg < 2000, f"send→receive avg {avg:.1f}ms exceeds 2s"

    @pytest.mark.asyncio
    async def test_latency_get_messages_under_load(self):
        """get_messages latency with a full inbox."""
        async with mcp_client_session("latency/loaded") as session:
            await session.call_tool("register_agent", {
                "name": "latency/loaded",
            })

            # Fill the inbox from a separate session
            async with mcp_client_session("latency/filler") as filler:
                await filler.call_tool("register_agent", {
                    "name": "latency/filler",
                })
                for i in range(50):
                    await filler.call_tool("send_message", {
                        "to": "latency/loaded",
                        "message": f"load-{i}",
                    })

            # Time get_messages calls
            latencies = []
            for _ in range(10):
                t0 = time.perf_counter()
                await session.call_tool("get_messages", {})
                latencies.append((time.perf_counter() - t0) * 1000)

            avg = sum(latencies) / len(latencies)
            p95 = sorted(latencies)[int(len(latencies) * 0.95)]
            print(f"\n  get_messages (50 msgs): avg={avg:.1f}ms  p95={p95:.1f}ms  "
                  f"min={min(latencies):.1f}ms  max={max(latencies):.1f}ms")
            assert avg < 1000, f"get_messages avg {avg:.1f}ms exceeds 1s"

    @pytest.mark.asyncio
    async def test_latency_concurrent_senders(self):
        """Multiple concurrent senders to the same target."""
        async with mcp_client_session("latency/target") as target_session:
            await target_session.call_tool("register_agent", {
                "name": "latency/target",
            })

            async def send_batch(sender_id, count):
                async with mcp_client_session(f"latency/sender-{sender_id}") as s:
                    await s.call_tool("register_agent", {
                        "name": f"latency/sender-{sender_id}",
                    })
                    latencies = []
                    for i in range(count):
                        t0 = time.perf_counter()
                        await s.call_tool("send_message", {
                            "to": "latency/target",
                            "message": f"from-{sender_id}-msg-{i}",
                        })
                        latencies.append((time.perf_counter() - t0) * 1000)
                    return latencies

            # 5 concurrent senders, 5 messages each
            t0 = time.perf_counter()
            tasks = [asyncio.create_task(send_batch(i, 5)) for i in range(5)]
            all_latencies = []
            for task in tasks:
                all_latencies.extend(await task)
            wall_time = (time.perf_counter() - t0) * 1000

            avg = sum(all_latencies) / len(all_latencies)
            p95 = sorted(all_latencies)[int(len(all_latencies) * 0.95)]
            print(f"\n  5 concurrent senders × 5 msgs:")
            print(f"    per-msg: avg={avg:.1f}ms  p95={p95:.1f}ms")
            print(f"    wall time: {wall_time:.0f}ms  "
                  f"throughput: {len(all_latencies) / (wall_time / 1000):.1f} msgs/s")

            # Verify all messages arrived
            result = await target_session.call_tool("get_messages", {})
            parsed = _parse_tool_result(result)
            if isinstance(parsed, list):
                msg_count = len(parsed)
            elif isinstance(parsed, dict):
                msg_count = len(parsed.get("messages", []))
            else:
                msg_count = 0
            assert msg_count >= 25, (
                f"Expected >=25 messages, got {msg_count}"
            )


# ---------------------------------------------------------------------------
# Acknowledgment behavior tests
# ---------------------------------------------------------------------------

class TestAckBehavior:
    """End-to-end tests for ack_messages behavior.

    Run with:
        C3PO_TEST_LIVE=1 pytest tests/test_e2e_integration.py::TestAckBehavior -v
    """

    @pytest.mark.asyncio
    async def test_ack_removes_message_from_queue(self):
        """Verify that acked messages don't appear in subsequent get_messages calls."""
        async with mcp_client_session("ack/sender") as sender:
            async with mcp_client_session("ack/receiver") as receiver:
                # Register agents
                await sender.call_tool("register_agent", {"name": "ack/sender"})
                await receiver.call_tool("register_agent", {"name": "ack/receiver"})

                # Sender sends a message
                await sender.call_tool("send_message", {
                    "to": "ack/receiver",
                    "message": "Test message"
                })

                # Receiver gets the message
                result = await receiver.call_tool("wait_for_message", {"timeout": 5})
                parsed = _parse_tool_result(result)

                assert isinstance(parsed, dict) and parsed.get("status") == "received"
                messages = parsed.get("messages", [])
                assert len(messages) == 1
                msg_id = messages[0]["id"]

                # Receiver acknowledges
                await receiver.call_tool("ack_messages", {
                    "message_ids": [msg_id]
                })

                # Verify message is gone from subsequent get_messages call
                result = await receiver.call_tool("get_messages", {})
                parsed = _parse_tool_result(result)

                if isinstance(parsed, list):
                    msg_count = len(parsed)
                elif isinstance(parsed, dict):
                    msg_count = len(parsed.get("messages", []))
                else:
                    msg_count = 0
                assert msg_count == 0, "Message should be removed after ack"

    @pytest.mark.asyncio
    async def test_partial_ack_leaves_other_messages(self):
        """Verify that acking some messages doesn't remove unacked ones."""
        async with mcp_client_session("ack/sender2") as sender:
            async with mcp_client_session("ack/receiver2") as receiver:
                await sender.call_tool("register_agent", {"name": "ack/sender2"})
                await receiver.call_tool("register_agent", {"name": "ack/receiver2"})

                # Send 3 messages
                for i in range(3):
                    await sender.call_tool("send_message", {
                        "to": "ack/receiver2",
                        "message": f"Message {i}"
                    })

                # Get and acknowledge only the first
                result = await receiver.call_tool("get_messages", {})
                parsed = _parse_tool_result(result)
                if isinstance(parsed, list):
                    msg_count = len(parsed)
                elif isinstance(parsed, dict):
                    msg_count = len(parsed.get("messages", []))
                else:
                    msg_count = 0
                assert msg_count == 3, "Should have 3 messages"

                msg_ids = [m["id"] for m in parsed[:2]]  # Ack first 2
                await receiver.call_tool("ack_messages", {
                    "message_ids": msg_ids
                })

                # Verify only 1 remains (the 3rd was not acked)
                result = await receiver.call_tool("get_messages", {})
                parsed = _parse_tool_result(result)

                if isinstance(parsed, list):
                    msg_count = len(parsed)
                elif isinstance(parsed, dict):
                    msg_count = len(parsed.get("messages", []))
                else:
                    msg_count = 0
                assert msg_count == 1, "Should have 1 unacked message"
                if isinstance(parsed, list):
                    assert parsed[0]["message"] == "Message 2"
                elif isinstance(parsed, dict):
                    assert parsed["messages"][0]["message"] == "Message 2"

    @pytest.mark.asyncio
    async def test_compaction_removes_acked_messages(self):
        """Verify compaction removes all acked messages when threshold is exceeded."""
        async with mcp_client_session("ack/sender3") as sender:
            async with mcp_client_session("ack/receiver3") as receiver:
                await sender.call_tool("register_agent", {"name": "ack/sender3"})
                await receiver.call_tool("register_agent", {"name": "ack/receiver3"})

                # Send 25 messages (above compaction threshold of 20)
                for i in range(25):
                    await sender.call_tool("send_message", {
                        "to": "ack/receiver3",
                        "message": f"Message {i}"
                    })

                # Get all messages
                result = await receiver.call_tool("get_messages", {})
                parsed = _parse_tool_result(result)
                if isinstance(parsed, list):
                    msg_count = len(parsed)
                elif isinstance(parsed, dict):
                    msg_count = len(parsed.get("messages", []))
                else:
                    msg_count = 0
                assert msg_count == 25, "Should have 25 messages"

                # Ack all messages
                msg_ids = [m["id"] for m in parsed]
                await receiver.call_tool("ack_messages", {
                    "message_ids": msg_ids
                })

                # Verify all messages are gone (compaction should have run)
                result = await receiver.call_tool("get_messages", {})
                parsed = _parse_tool_result(result)

                if isinstance(parsed, list):
                    msg_count = len(parsed)
                elif isinstance(parsed, dict):
                    msg_count = len(parsed.get("messages", []))
                else:
                    msg_count = 0
                assert msg_count == 0, "All messages should be removed after compaction"
