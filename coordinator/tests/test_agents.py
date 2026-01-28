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
            "agent-a", capabilities=["home-automation", "mqtt"]
        )

        assert result["capabilities"] == ["home-automation", "mqtt"]

    def test_register_duplicate_updates_existing(self, agent_manager):
        """Duplicate registration should update, not create new."""
        first = agent_manager.register_agent("agent-a")
        original_registered_at = first["registered_at"]

        # Small delay to ensure timestamp difference
        time.sleep(0.01)

        second = agent_manager.register_agent("agent-a")

        # Should keep original registration time
        assert second["registered_at"] == original_registered_at
        # Should update last_seen
        assert second["last_seen"] >= first["last_seen"]

    def test_register_updates_capabilities(self, agent_manager):
        """Re-registering with new capabilities should update them."""
        agent_manager.register_agent("agent-a", capabilities=["old"])
        result = agent_manager.register_agent("agent-a", capabilities=["new"])

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
        agent_manager.register_agent("agent-a", capabilities=["test"])
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
