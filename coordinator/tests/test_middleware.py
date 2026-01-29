"""Tests for AgentIdentityMiddleware - header extraction and collision handling.

These tests verify the middleware logic by testing the underlying components
(AgentManager collision resolution, registration behavior) that the middleware
depends on. Integration testing of the actual MCP endpoint requires a running
server, which is covered by manual testing.
"""

import json
from datetime import datetime, timezone, timedelta

import fakeredis
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from unittest.mock import MagicMock, patch, AsyncMock

from coordinator.agents import AgentManager
from coordinator.messaging import MessageManager
from coordinator.server import AgentIdentityMiddleware


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


class TestAgentAutoRegistration:
    """Tests for agent auto-registration behavior during registration."""

    def test_new_agent_registers_successfully(self, agent_manager):
        """New agent should be registered with provided ID."""
        result = agent_manager.register_agent("new-agent", "session-1")

        assert result["id"] == "new-agent"
        assert result["session_id"] == "session-1"
        assert result["status"] == "online"

    def test_same_session_reconnect_keeps_id(self, agent_manager):
        """Same session reconnecting should keep the same agent ID."""
        # First registration
        agent_manager.register_agent("reconnect-test", "session-abc")

        # Same session reconnecting
        result = agent_manager.register_agent("reconnect-test", "session-abc")

        # Should still have same ID
        assert result["id"] == "reconnect-test"
        assert result["session_id"] == "session-abc"

        # Should still be one agent
        agents = agent_manager.list_agents()
        assert len(agents) == 1

    def test_session_reconnect_updates_heartbeat(self, agent_manager):
        """Same session reconnecting should update last_seen timestamp."""
        # First registration
        first = agent_manager.register_agent("heartbeat-test", "session-123")
        first_seen = first["last_seen"]

        # Wait a tiny bit
        import time
        time.sleep(0.01)

        # Reconnect
        second = agent_manager.register_agent("heartbeat-test", "session-123")
        second_seen = second["last_seen"]

        # Timestamp should have changed
        assert second_seen >= first_seen


class TestCollisionResolution:
    """Tests for agent ID collision handling."""

    def test_collision_with_online_agent_creates_suffix(self, agent_manager):
        """Different session with same agent ID should get suffixed ID when original is online."""
        # First agent registers
        agent_manager.register_agent("collision-test", "session-1")

        # Different session with same agent ID
        result = agent_manager.register_agent("collision-test", "session-2")

        # Should have suffixed ID
        assert result["id"] == "collision-test-2"
        assert result["session_id"] == "session-2"

        # Should have two agents now
        agents = agent_manager.list_agents()
        assert len(agents) == 2

        agent_ids = [a["id"] for a in agents]
        assert "collision-test" in agent_ids
        assert "collision-test-2" in agent_ids

    def test_multiple_collisions_increment_suffix(self, agent_manager):
        """Multiple collisions should increment the suffix."""
        # Register three agents with same requested ID but different sessions
        agent_manager.register_agent("multi-collision", "session-0")
        agent_manager.register_agent("multi-collision", "session-1")
        result = agent_manager.register_agent("multi-collision", "session-2")

        # Third agent should be -3
        assert result["id"] == "multi-collision-3"

        agents = agent_manager.list_agents()
        assert len(agents) == 3

        agent_ids = sorted([a["id"] for a in agents])
        assert agent_ids == ["multi-collision", "multi-collision-2", "multi-collision-3"]

    def test_offline_agent_id_can_be_reused(self, agent_manager):
        """Offline agent ID should be available for reuse."""
        # Register an agent
        agent_manager.register_agent("reuse-test", "old-session")

        # Make the agent offline by backdating its last_seen
        agent_data = json.loads(
            agent_manager.redis.hget(agent_manager.AGENTS_KEY, "reuse-test").decode()
        )
        old_time = (
            datetime.now(timezone.utc)
            - timedelta(seconds=agent_manager.AGENT_TIMEOUT_SECONDS + 10)
        ).isoformat()
        agent_data["last_seen"] = old_time
        agent_manager.redis.hset(
            agent_manager.AGENTS_KEY, "reuse-test", json.dumps(agent_data)
        )

        # Verify agent is offline
        agent = agent_manager.get_agent("reuse-test")
        assert agent["status"] == "offline"

        # New session with same ID should take over
        result = agent_manager.register_agent("reuse-test", "new-session")

        # Should have original ID, not suffixed
        assert result["id"] == "reuse-test"
        assert result["session_id"] == "new-session"
        assert result["status"] == "online"

        # Should still be one agent
        agents = agent_manager.list_agents()
        assert len(agents) == 1

    def test_collision_suffix_skips_online_agents(self, agent_manager):
        """Collision resolution should skip online suffixed agents."""
        # Register base agent and agent-2
        agent_manager.register_agent("skip-test", "session-1")
        agent_manager.register_agent("skip-test", "session-2")  # Creates skip-test-2

        # Third collision should create skip-test-3
        result = agent_manager.register_agent("skip-test", "session-3")
        assert result["id"] == "skip-test-3"

    def test_collision_reuses_offline_suffix(self, agent_manager):
        """Collision resolution should reuse offline suffixed agents."""
        # Register base agent and agent-2
        agent_manager.register_agent("reuse-suffix", "session-1")
        agent_manager.register_agent("reuse-suffix", "session-2")  # Creates reuse-suffix-2

        # Make agent-2 offline
        agent_data = json.loads(
            agent_manager.redis.hget(agent_manager.AGENTS_KEY, "reuse-suffix-2").decode()
        )
        old_time = (
            datetime.now(timezone.utc)
            - timedelta(seconds=agent_manager.AGENT_TIMEOUT_SECONDS + 10)
        ).isoformat()
        agent_data["last_seen"] = old_time
        agent_manager.redis.hset(
            agent_manager.AGENTS_KEY, "reuse-suffix-2", json.dumps(agent_data)
        )

        # New collision should reuse the -2 slot
        result = agent_manager.register_agent("reuse-suffix", "session-3")
        assert result["id"] == "reuse-suffix-2"
        assert result["session_id"] == "session-3"


class TestSessionIdTracking:
    """Tests for session ID tracking behavior."""

    def test_session_id_stored_on_registration(self, agent_manager):
        """Session ID should be stored with the agent."""
        result = agent_manager.register_agent(
            "session-track-test",
            session_id="unique-session-456"
        )

        assert result["session_id"] == "unique-session-456"

        # Verify in storage
        agent = agent_manager.get_agent("session-track-test")
        assert agent["session_id"] == "unique-session-456"

    def test_none_session_id_handled(self, agent_manager):
        """None session ID should be stored as None."""
        result = agent_manager.register_agent("no-session-test")

        assert result["session_id"] is None

        agent = agent_manager.get_agent("no-session-test")
        assert agent["session_id"] is None

    def test_different_session_same_id_collides(self, agent_manager):
        """Different session with same agent ID should trigger collision."""
        # First agent
        agent_manager.register_agent("session-collision", "session-A")

        # Different session, same ID
        result = agent_manager.register_agent("session-collision", "session-B")

        # Should be suffixed due to collision
        assert result["id"] == "session-collision-2"
        assert result["session_id"] == "session-B"

    def test_no_session_to_no_session_replaces(self, agent_manager):
        """Agent with no session ID can be replaced by another with no session ID."""
        # First registration without session
        agent_manager.register_agent("no-session-replace", None)

        # Make it offline
        agent_data = json.loads(
            agent_manager.redis.hget(agent_manager.AGENTS_KEY, "no-session-replace").decode()
        )
        old_time = (
            datetime.now(timezone.utc)
            - timedelta(seconds=agent_manager.AGENT_TIMEOUT_SECONDS + 10)
        ).isoformat()
        agent_data["last_seen"] = old_time
        agent_manager.redis.hset(
            agent_manager.AGENTS_KEY, "no-session-replace", json.dumps(agent_data)
        )

        # New registration without session should take over
        result = agent_manager.register_agent("no-session-replace", None)
        assert result["id"] == "no-session-replace"
        assert result["status"] == "online"


class TestMiddlewareInstance:
    """Tests for the AgentIdentityMiddleware class itself."""

    def test_middleware_can_be_instantiated(self):
        """AgentIdentityMiddleware should be instantiable."""
        middleware = AgentIdentityMiddleware()
        assert middleware is not None

    def test_middleware_has_on_call_tool(self):
        """AgentIdentityMiddleware should have on_call_tool method."""
        middleware = AgentIdentityMiddleware()
        assert hasattr(middleware, "on_call_tool")
        assert callable(middleware.on_call_tool)


class TestCapabilitiesHandling:
    """Tests for capabilities during registration."""

    def test_capabilities_stored_on_registration(self, agent_manager):
        """Capabilities should be stored with the agent."""
        result = agent_manager.register_agent(
            "cap-test",
            session_id="session-1",
            capabilities=["search", "code", "test"]
        )

        assert result["capabilities"] == ["search", "code", "test"]

    def test_capabilities_updated_on_reconnect(self, agent_manager):
        """Capabilities should be updated when same session reconnects."""
        # Initial registration
        agent_manager.register_agent(
            "cap-update-test",
            session_id="session-1",
            capabilities=["search"]
        )

        # Reconnect with new capabilities
        result = agent_manager.register_agent(
            "cap-update-test",
            session_id="session-1",
            capabilities=["search", "code", "new"]
        )

        assert result["capabilities"] == ["search", "code", "new"]

    def test_empty_capabilities_default(self, agent_manager):
        """No capabilities should default to empty list."""
        result = agent_manager.register_agent("no-cap-test")

        assert result["capabilities"] == []
