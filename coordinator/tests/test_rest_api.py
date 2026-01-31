"""Tests for REST API endpoints (/api/health, /api/pending)."""

import fakeredis
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from coordinator.agents import AgentManager
from coordinator.audit import AuditLogger
from coordinator.auth import ProxyAuthManager
from coordinator.messaging import MessageManager
from coordinator.rate_limit import RateLimiter


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
    # Disable auth for REST API tests (tests exercise endpoint logic, not auth)
    monkeypatch.delenv("C3PO_PROXY_BEARER_TOKEN", raising=False)

    # Monkeypatch the module-level clients before importing server
    import coordinator.server as server_module

    monkeypatch.setattr(server_module, "redis_client", redis_client)
    monkeypatch.setattr(server_module, "agent_manager", agent_manager)
    monkeypatch.setattr(server_module, "message_manager", message_manager)
    monkeypatch.setattr(server_module, "auth_manager", ProxyAuthManager())
    monkeypatch.setattr(server_module, "rate_limiter", RateLimiter(redis_client))
    monkeypatch.setattr(server_module, "audit_logger", AuditLogger(redis_client))

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
            "/api/pending", headers={"X-Agent-ID": "unknown-agent/project"}
        )

        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 0
        assert data["requests"] == []

    @pytest.mark.asyncio
    async def test_pending_rejects_bare_machine_name(self, client):
        """Pending endpoint should reject bare machine name without project."""
        response = await client.get(
            "/api/pending", headers={"X-Agent-ID": "bare-machine"}
        )

        assert response.status_code == 400
        assert "Bare machine name" in response.json()["error"]

    @pytest.mark.asyncio
    async def test_pending_returns_count_without_consuming(
        self, client, message_manager, agent_manager
    ):
        """Pending endpoint should return count without consuming messages."""
        # Register agents
        agent_manager.register_agent("sender/proj")
        agent_manager.register_agent("receiver/proj")

        # Send a request
        message_manager.send_request(
            "sender/proj", "receiver/proj", "Test message", context="Test context"
        )

        # Check pending - should show 1
        response = await client.get(
            "/api/pending", headers={"X-Agent-ID": "receiver/proj"}
        )
        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 1
        assert len(data["requests"]) == 1
        assert data["requests"][0]["message"] == "Test message"

        # Check again - should still show 1 (not consumed)
        response = await client.get(
            "/api/pending", headers={"X-Agent-ID": "receiver/proj"}
        )
        data = response.json()
        assert data["count"] == 1

    @pytest.mark.asyncio
    async def test_pending_returns_multiple_requests(
        self, client, message_manager, agent_manager
    ):
        """Pending endpoint should return all pending requests."""
        agent_manager.register_agent("sender/proj")
        agent_manager.register_agent("receiver/proj")

        # Send multiple requests
        message_manager.send_request("sender/proj", "receiver/proj", "Message 1")
        message_manager.send_request("sender/proj", "receiver/proj", "Message 2")
        message_manager.send_request("sender/proj", "receiver/proj", "Message 3")

        response = await client.get(
            "/api/pending", headers={"X-Agent-ID": "receiver/proj"}
        )

        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 3
        assert len(data["requests"]) == 3

        # Check FIFO order
        assert data["requests"][0]["message"] == "Message 1"
        assert data["requests"][1]["message"] == "Message 2"
        assert data["requests"][2]["message"] == "Message 3"


class TestUnregisterEndpoint:
    """Tests for /api/unregister endpoint."""

    @pytest.mark.asyncio
    async def test_unregister_requires_agent_id_header(self, client):
        """Unregister endpoint should require X-Agent-ID header."""
        response = await client.post("/api/unregister")

        assert response.status_code == 400
        data = response.json()
        assert "Missing X-Agent-ID header" in data["error"]

    @pytest.mark.asyncio
    async def test_unregister_removes_registered_agent(self, client, agent_manager):
        """Unregister endpoint should remove a registered agent."""
        # Register an agent first
        agent_manager.register_agent("machine/to-remove")
        assert agent_manager.get_agent("machine/to-remove") is not None

        # Unregister the agent
        response = await client.post(
            "/api/unregister", headers={"X-Agent-ID": "machine/to-remove"}
        )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert "unregistered" in data["message"]
        assert "machine/to-remove" in data["message"]

        # Verify agent is no longer registered
        assert agent_manager.get_agent("machine/to-remove") is None

    @pytest.mark.asyncio
    async def test_unregister_unknown_agent_returns_ok(self, client, agent_manager):
        """Unregister endpoint should succeed for unknown agent (idempotent)."""
        # Verify agent doesn't exist
        assert agent_manager.get_agent("machine/nonexistent") is None

        # Unregister should still succeed
        response = await client.post(
            "/api/unregister", headers={"X-Agent-ID": "machine/nonexistent"}
        )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert "not registered" in data["message"]

    @pytest.mark.asyncio
    async def test_unregister_does_not_affect_other_agents(self, client, agent_manager):
        """Unregister should only remove the specified agent."""
        # Register multiple agents
        agent_manager.register_agent("machine/agent-1")
        agent_manager.register_agent("machine/agent-2")
        agent_manager.register_agent("machine/agent-3")

        # Unregister one
        response = await client.post(
            "/api/unregister", headers={"X-Agent-ID": "machine/agent-2"}
        )

        assert response.status_code == 200

        # Check only agent-2 was removed
        assert agent_manager.get_agent("machine/agent-1") is not None
        assert agent_manager.get_agent("machine/agent-2") is None
        assert agent_manager.get_agent("machine/agent-3") is not None

    @pytest.mark.asyncio
    async def test_unregister_reflects_in_agent_count(self, client, agent_manager):
        """Unregistered agent should be reflected in health endpoint count."""
        # Register agents
        agent_manager.register_agent("machine/agent-1")
        agent_manager.register_agent("machine/agent-2")

        # Check initial count
        response = await client.get("/api/health")
        assert response.json()["agents_online"] == 2

        # Unregister one
        await client.post("/api/unregister", headers={"X-Agent-ID": "machine/agent-1"})

        # Check updated count
        response = await client.get("/api/health")
        assert response.json()["agents_online"] == 1


class TestInputValidation:
    """Tests for REST endpoint input validation."""

    @pytest.mark.asyncio
    async def test_pending_rejects_invalid_agent_id_format(self, client):
        """Pending endpoint should reject invalid agent ID format."""
        response = await client.get(
            "/api/pending", headers={"X-Agent-ID": "-invalid/proj"}
        )

        assert response.status_code == 400
        data = response.json()
        assert "Invalid" in data["error"]

    @pytest.mark.asyncio
    async def test_pending_accepts_valid_agent_id(self, client):
        """Pending endpoint should accept valid agent ID format."""
        response = await client.get(
            "/api/pending", headers={"X-Agent-ID": "valid-agent/proj_123"}
        )

        assert response.status_code == 200
        data = response.json()
        assert "count" in data

    @pytest.mark.asyncio
    async def test_unregister_rejects_invalid_agent_id_format(self, client):
        """Unregister endpoint should reject invalid agent ID format."""
        response = await client.post(
            "/api/unregister", headers={"X-Agent-ID": " spaces/not-allowed"}
        )

        assert response.status_code == 400
        data = response.json()
        assert "Invalid" in data["error"]

    @pytest.mark.asyncio
    async def test_unregister_accepts_valid_agent_id(self, client):
        """Unregister endpoint should accept valid agent ID format."""
        response = await client.post(
            "/api/unregister", headers={"X-Agent-ID": "valid.agent/proj-1"}
        )

        assert response.status_code == 200
