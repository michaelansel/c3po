"""Tests for REST API endpoints (/api/health, /api/pending)."""

import fakeredis
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from coordinator.agents import AgentManager
from coordinator.messaging import MessageManager


@pytest.fixture
def redis_client():
    """Create a fresh fakeredis client for each test."""
    return fakeredis.FakeRedis()


@pytest.fixture
def agent_manager(redis_client):
    """Create AgentManager with fakeredis."""
    return AgentManager(redis_client)


@pytest.fixture
def message_manager(redis_client):
    """Create MessageManager with fakeredis."""
    return MessageManager(redis_client)


@pytest.fixture
def mcp_app(redis_client, agent_manager, message_manager, monkeypatch):
    """Create the MCP app with test Redis client."""
    # Monkeypatch the module-level clients before importing server
    import coordinator.server as server_module

    monkeypatch.setattr(server_module, "redis_client", redis_client)
    monkeypatch.setattr(server_module, "agent_manager", agent_manager)
    monkeypatch.setattr(server_module, "message_manager", message_manager)

    return server_module.mcp.http_app()


@pytest_asyncio.fixture
async def client(mcp_app):
    """Create async test client."""
    transport = ASGITransport(app=mcp_app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


class TestHealthEndpoint:
    """Tests for /api/health endpoint."""

    @pytest.mark.asyncio
    async def test_health_returns_ok(self, client):
        """Health endpoint should return status ok."""
        response = await client.get("/api/health")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"

    @pytest.mark.asyncio
    async def test_health_returns_agents_online_count(self, client, agent_manager):
        """Health endpoint should return count of online agents."""
        # Initially no agents
        response = await client.get("/api/health")
        data = response.json()
        assert data["agents_online"] == 0

        # Register some agents
        agent_manager.register_agent("agent-1")
        agent_manager.register_agent("agent-2")

        response = await client.get("/api/health")
        data = response.json()
        assert data["agents_online"] == 2


class TestPendingEndpoint:
    """Tests for /api/pending endpoint."""

    @pytest.mark.asyncio
    async def test_pending_requires_agent_id_header(self, client):
        """Pending endpoint should require X-Agent-ID header."""
        response = await client.get("/api/pending")

        assert response.status_code == 400
        data = response.json()
        assert "Missing X-Agent-ID header" in data["error"]

    @pytest.mark.asyncio
    async def test_pending_returns_empty_for_unknown_agent(self, client):
        """Pending endpoint should return empty for unknown agent."""
        response = await client.get(
            "/api/pending", headers={"X-Agent-ID": "unknown-agent"}
        )

        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 0
        assert data["requests"] == []

    @pytest.mark.asyncio
    async def test_pending_returns_count_without_consuming(
        self, client, message_manager, agent_manager
    ):
        """Pending endpoint should return count without consuming messages."""
        # Register agents
        agent_manager.register_agent("sender")
        agent_manager.register_agent("receiver")

        # Send a request
        message_manager.send_request(
            "sender", "receiver", "Test message", context="Test context"
        )

        # Check pending - should show 1
        response = await client.get(
            "/api/pending", headers={"X-Agent-ID": "receiver"}
        )
        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 1
        assert len(data["requests"]) == 1
        assert data["requests"][0]["message"] == "Test message"

        # Check again - should still show 1 (not consumed)
        response = await client.get(
            "/api/pending", headers={"X-Agent-ID": "receiver"}
        )
        data = response.json()
        assert data["count"] == 1

    @pytest.mark.asyncio
    async def test_pending_returns_multiple_requests(
        self, client, message_manager, agent_manager
    ):
        """Pending endpoint should return all pending requests."""
        agent_manager.register_agent("sender")
        agent_manager.register_agent("receiver")

        # Send multiple requests
        message_manager.send_request("sender", "receiver", "Message 1")
        message_manager.send_request("sender", "receiver", "Message 2")
        message_manager.send_request("sender", "receiver", "Message 3")

        response = await client.get(
            "/api/pending", headers={"X-Agent-ID": "receiver"}
        )

        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 3
        assert len(data["requests"]) == 3

        # Check FIFO order
        assert data["requests"][0]["message"] == "Message 1"
        assert data["requests"][1]["message"] == "Message 2"
        assert data["requests"][2]["message"] == "Message 3"
