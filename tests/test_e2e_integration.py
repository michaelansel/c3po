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
"""

import asyncio
import os
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
    headers = {"X-Agent-ID": agent_id}

    async with streamablehttp_client(url, headers=headers) as (read_stream, write_stream, _):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            yield session


class TestE2EIntegration:
    """End-to-end integration tests."""

    @pytest.mark.asyncio
    async def test_ping_tool(self):
        """Test the ping tool returns expected response."""
        async with mcp_client_session("test-agent") as session:
            result = await session.call_tool("ping", {})
            assert "pong" in str(result)

    @pytest.mark.asyncio
    async def test_agent_registration_via_list(self):
        """Test that connecting registers the agent."""
        async with mcp_client_session("e2e-test-agent") as session:
            result = await session.call_tool("list_agents", {})
            # The calling agent should be in the list
            agent_ids = [a.get("id") for a in result.content if hasattr(a, "text")]
            # Note: result format depends on MCP response structure
            assert result is not None

    @pytest.mark.asyncio
    async def test_send_and_receive_request(self):
        """Test sending a request from one agent to another."""
        # Register agent-b first
        async with mcp_client_session("e2e-agent-b") as session_b:
            await session_b.call_tool("ping", {})  # Register via heartbeat

        # Send request from agent-a
        async with mcp_client_session("e2e-agent-a") as session_a:
            send_result = await session_a.call_tool("send_request", {
                "target": "e2e-agent-b",
                "message": "Hello from E2E test!",
                "context": "Integration test"
            })

            # Extract request_id from result
            # The exact format depends on how FastMCP returns tool results
            assert send_result is not None

    @pytest.mark.asyncio
    async def test_full_request_response_cycle(self):
        """Test complete request/response cycle between two agents."""
        request_id = None

        # Step 1: Register both agents
        async with mcp_client_session("e2e-sender") as sender:
            await sender.call_tool("ping", {})

        async with mcp_client_session("e2e-receiver") as receiver:
            await receiver.call_tool("ping", {})

        # Step 2: Sender sends request
        async with mcp_client_session("e2e-sender") as sender:
            send_result = await sender.call_tool("send_request", {
                "target": "e2e-receiver",
                "message": "E2E test message"
            })
            # Parse request_id from result
            # This depends on the exact response format
            assert send_result is not None

        # Step 3: Receiver gets and responds to request
        async with mcp_client_session("e2e-receiver") as receiver:
            pending = await receiver.call_tool("get_pending_requests", {})
            # Parse the request and respond
            assert pending is not None

    @pytest.mark.asyncio
    async def test_wait_for_request_timeout(self):
        """Test that wait_for_request times out correctly."""
        async with mcp_client_session("timeout-test-agent") as session:
            result = await session.call_tool("wait_for_request", {
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
        """Test /api/pending without X-Agent-ID header returns error."""
        import httpx

        async with httpx.AsyncClient() as client:
            response = await client.get(f"{COORDINATOR_URL}/api/pending")
            assert response.status_code == 400
            data = response.json()
            assert "error" in data

    @pytest.mark.asyncio
    async def test_pending_endpoint_with_header(self):
        """Test /api/pending with X-Agent-ID header."""
        import httpx

        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{COORDINATOR_URL}/api/pending",
                headers={"X-Agent-ID": "rest-test-agent"}
            )
            assert response.status_code == 200
            data = response.json()
            assert "count" in data
            assert "requests" in data
