"""Tests for agent registration and management."""

import json
import time
from datetime import datetime, timedelta, timezone

import fakeredis
import pytest

from coordinator.agents import AgentManager


@pytest.fixture
def redis_client():
    """Create a fresh fakeredis client for each test."""
    return fakeredis.FakeRedis()


@pytest.fixture
def agent_manager(redis_client):
    """Create AgentManager with fakeredis."""
    return AgentManager(redis_client)


class TestRegisterAgent:
    """Tests for agent registration."""

    def test_register_new_agent(self, agent_manager):
        """Should create new agent with correct data."""
        result = agent_manager.register_agent("agent-a")

        assert result["id"] == "agent-a"
        assert result["capabilities"] == []
        assert "registered_at" in result
        assert "last_seen" in result

    def test_register_agent_with_capabilities(self, agent_manager):
        """Should store capabilities when provided."""
        result = agent_manager.register_agent(
            "agent-a", session_id=None, capabilities=["home-automation", "mqtt"]
        )

        assert result["capabilities"] == ["home-automation", "mqtt"]

    def test_register_duplicate_with_same_session_updates_existing(self, agent_manager):
        """Duplicate registration with same session should update, not create new."""
        first = agent_manager.register_agent("agent-a", session_id="session-1")
        original_registered_at = first["registered_at"]

        # Small delay to ensure timestamp difference
        time.sleep(0.01)

        second = agent_manager.register_agent("agent-a", session_id="session-1")

        # Should keep original registration time
        assert second["registered_at"] == original_registered_at
        # Should update last_seen
        assert second["last_seen"] >= first["last_seen"]
        # Should keep same ID
        assert second["id"] == "agent-a"

    def test_register_updates_capabilities(self, agent_manager):
        """Re-registering with new capabilities should update them."""
        agent_manager.register_agent("agent-a", session_id=None, capabilities=["old"])
        result = agent_manager.register_agent("agent-a", session_id=None, capabilities=["new"])

        assert result["capabilities"] == ["new"]


class TestListAgents:
    """Tests for listing agents."""

    def test_list_empty(self, agent_manager):
        """Should return empty list when no agents registered."""
        result = agent_manager.list_agents()
        assert result == []

    def test_list_registered_agents(self, agent_manager):
        """Should return all registered agents."""
        agent_manager.register_agent("agent-a")
        agent_manager.register_agent("agent-b")

        result = agent_manager.list_agents()

        assert len(result) == 2
        agent_ids = {a["id"] for a in result}
        assert agent_ids == {"agent-a", "agent-b"}

    def test_list_includes_status(self, agent_manager):
        """Agents should have online/offline status."""
        agent_manager.register_agent("agent-a")
        result = agent_manager.list_agents()

        assert len(result) == 1
        assert result[0]["status"] == "online"

    def test_list_shows_offline_after_timeout(self, agent_manager, redis_client):
        """Agent should show offline if last_seen exceeds timeout."""
        # Register agent
        agent_manager.register_agent("agent-a")

        # Manually set last_seen to old time
        old_time = (
            datetime.now(timezone.utc) - timedelta(seconds=100)
        ).isoformat()
        data = json.loads(redis_client.hget(agent_manager.AGENTS_KEY, "agent-a"))
        data["last_seen"] = old_time
        redis_client.hset(agent_manager.AGENTS_KEY, "agent-a", json.dumps(data))

        result = agent_manager.list_agents()
        assert result[0]["status"] == "offline"


class TestGetAgent:
    """Tests for getting a single agent."""

    def test_get_existing_agent(self, agent_manager):
        """Should return agent data when exists."""
        agent_manager.register_agent("agent-a", session_id=None, capabilities=["test"])
        result = agent_manager.get_agent("agent-a")

        assert result is not None
        assert result["id"] == "agent-a"
        assert result["capabilities"] == ["test"]
        assert result["status"] == "online"

    def test_get_nonexistent_agent(self, agent_manager):
        """Should return None for unknown agent."""
        result = agent_manager.get_agent("unknown")
        assert result is None


class TestUpdateHeartbeat:
    """Tests for heartbeat updates."""

    def test_update_heartbeat_success(self, agent_manager):
        """Should update last_seen timestamp."""
        agent_manager.register_agent("agent-a")
        original = agent_manager.get_agent("agent-a")["last_seen"]

        time.sleep(0.01)
        result = agent_manager.update_heartbeat("agent-a")

        assert result is True
        updated = agent_manager.get_agent("agent-a")["last_seen"]
        assert updated >= original

    def test_update_heartbeat_unknown_agent(self, agent_manager):
        """Should return False for unknown agent."""
        result = agent_manager.update_heartbeat("unknown")
        assert result is False


class TestRemoveAgent:
    """Tests for agent removal."""

    def test_remove_existing_agent(self, agent_manager):
        """Should remove agent from registry."""
        agent_manager.register_agent("agent-a")
        result = agent_manager.remove_agent("agent-a")

        assert result is True
        assert agent_manager.get_agent("agent-a") is None

    def test_remove_nonexistent_agent(self, agent_manager):
        """Should return False for unknown agent."""
        result = agent_manager.remove_agent("unknown")
        assert result is False


class TestCollisionHandling:
    """Tests for agent ID collision handling."""

    def test_collision_without_session_id(self, agent_manager):
        """Without session_id, re-registration is treated as collision if online."""
        first = agent_manager.register_agent("agent-a")
        second = agent_manager.register_agent("agent-a")

        # Without session_id, we can't tell if it's the same session,
        # so treat as collision if agent is online
        assert second["id"] == "agent-a-2"

    def test_same_session_reconnect_keeps_id(self, agent_manager):
        """Same session reconnecting should keep the same agent ID."""
        first = agent_manager.register_agent("agent-a", session_id="session-123")
        second = agent_manager.register_agent("agent-a", session_id="session-123")

        assert second["id"] == "agent-a"
        assert second["session_id"] == "session-123"

    def test_collision_with_different_session_gets_suffix(self, agent_manager):
        """Different session with same agent ID gets suffixed ID."""
        first = agent_manager.register_agent("agent-a", session_id="session-1")
        second = agent_manager.register_agent("agent-a", session_id="session-2")

        assert first["id"] == "agent-a"
        assert second["id"] == "agent-a-2"
        assert second["session_id"] == "session-2"

    def test_multiple_collisions_increment_suffix(self, agent_manager):
        """Multiple collisions should increment suffix."""
        agent_manager.register_agent("agent-a", session_id="session-1")
        agent_manager.register_agent("agent-a", session_id="session-2")
        third = agent_manager.register_agent("agent-a", session_id="session-3")

        assert third["id"] == "agent-a-3"

    def test_collision_reuses_offline_slot(self, agent_manager, redis_client):
        """Collision should reuse an offline agent's slot."""
        # Register first agent
        agent_manager.register_agent("agent-a", session_id="session-1")

        # Register second agent (gets -2)
        agent_manager.register_agent("agent-a", session_id="session-2")

        # Make agent-a-2 offline
        old_time = (
            datetime.now(timezone.utc) - timedelta(seconds=100)
        ).isoformat()
        data = json.loads(redis_client.hget(agent_manager.AGENTS_KEY, "agent-a-2"))
        data["last_seen"] = old_time
        redis_client.hset(agent_manager.AGENTS_KEY, "agent-a-2", json.dumps(data))

        # New session should get agent-a-2 (reusing offline slot)
        third = agent_manager.register_agent("agent-a", session_id="session-3")
        assert third["id"] == "agent-a-2"

    def test_collision_skips_online_slots(self, agent_manager):
        """Collision should skip online agent slots."""
        agent_manager.register_agent("agent-a", session_id="session-1")
        agent_manager.register_agent("agent-a", session_id="session-2")  # Gets -2
        # Session-3 should get -3 since both agent-a and agent-a-2 are online
        third = agent_manager.register_agent("agent-a", session_id="session-3")

        assert third["id"] == "agent-a-3"

    def test_no_collision_with_offline_agent(self, agent_manager, redis_client):
        """New session can reuse ID of offline agent."""
        agent_manager.register_agent("agent-a", session_id="session-1")

        # Make agent offline
        old_time = (
            datetime.now(timezone.utc) - timedelta(seconds=100)
        ).isoformat()
        data = json.loads(redis_client.hget(agent_manager.AGENTS_KEY, "agent-a"))
        data["last_seen"] = old_time
        redis_client.hset(agent_manager.AGENTS_KEY, "agent-a", json.dumps(data))

        # New session should get agent-a (not -2)
        second = agent_manager.register_agent("agent-a", session_id="session-2")
        assert second["id"] == "agent-a"

    def test_session_id_stored(self, agent_manager):
        """Session ID should be stored in agent data."""
        agent_manager.register_agent("agent-a", session_id="session-123")
        agent = agent_manager.get_agent("agent-a")

        assert agent["session_id"] == "session-123"

    def test_session_id_none_allowed(self, agent_manager):
        """Registration without session_id should work."""
        result = agent_manager.register_agent("agent-a", session_id=None)

        assert result["id"] == "agent-a"
        assert result["session_id"] is None
