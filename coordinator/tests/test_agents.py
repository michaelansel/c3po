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

        # Manually set last_seen to old time (beyond timeout)
        old_time = (
            datetime.now(timezone.utc) - timedelta(seconds=agent_manager.AGENT_TIMEOUT_SECONDS + 10)
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

    def test_no_session_id_updates_existing_online_agent(self, agent_manager):
        """Without session_id, re-registration to online agent updates heartbeat.

        This handles MCP calls that can't include dynamic session IDs.
        The assumption is that if an agent is online and a call comes in
        with no session_id, it's from the same Claude Code instance.
        """
        first = agent_manager.register_agent("agent-a", session_id="session-123")
        second = agent_manager.register_agent("agent-a")  # No session_id (MCP call)

        # Should keep the same ID (assumed to be same session)
        assert second["id"] == "agent-a"
        # Original session_id preserved
        assert second["session_id"] == "session-123"

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

        # Make agent-a-2 offline (beyond timeout)
        old_time = (
            datetime.now(timezone.utc) - timedelta(seconds=agent_manager.AGENT_TIMEOUT_SECONDS + 10)
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

        # Make agent offline (beyond timeout)
        old_time = (
            datetime.now(timezone.utc) - timedelta(seconds=agent_manager.AGENT_TIMEOUT_SECONDS + 10)
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


class TestBulkRemove:
    """Tests for remove_agents_by_pattern."""

    def test_removes_matching_agents(self, agent_manager):
        """Should remove agents matching the pattern."""
        agent_manager.register_agent("stress/sender-0")
        agent_manager.register_agent("stress/sender-1")
        agent_manager.register_agent("other/agent")

        removed = agent_manager.remove_agents_by_pattern("stress/*")

        assert sorted(removed) == ["stress/sender-0", "stress/sender-1"]
        assert agent_manager.get_agent("stress/sender-0") is None
        assert agent_manager.get_agent("stress/sender-1") is None
        assert agent_manager.get_agent("other/agent") is not None

    def test_returns_empty_when_no_matches(self, agent_manager):
        """Should return empty list when no agents match."""
        agent_manager.register_agent("other/agent")

        removed = agent_manager.remove_agents_by_pattern("stress/*")

        assert removed == []
        assert agent_manager.get_agent("other/agent") is not None

    def test_cleans_up_redis_keys(self, agent_manager, redis_client):
        """Should delete associated Redis keys when cleanup_keys=True."""
        agent_manager.register_agent("stress/test")

        # Simulate Redis keys that would exist for this agent
        redis_client.rpush("c3po:inbox:stress/test", "msg1")
        redis_client.rpush("c3po:notify:stress/test", "notify1")
        redis_client.rpush("c3po:responses:stress/test", "resp1")
        redis_client.sadd("c3po:acked:stress/test", "acked1")

        agent_manager.remove_agents_by_pattern("stress/*")

        assert redis_client.llen("c3po:inbox:stress/test") == 0
        assert redis_client.llen("c3po:notify:stress/test") == 0
        assert redis_client.llen("c3po:responses:stress/test") == 0
        assert redis_client.scard("c3po:acked:stress/test") == 0

    def test_preserves_redis_keys_when_cleanup_disabled(self, agent_manager, redis_client):
        """Should preserve Redis keys when cleanup_keys=False."""
        agent_manager.register_agent("stress/test")
        redis_client.rpush("c3po:inbox:stress/test", "msg1")

        agent_manager.remove_agents_by_pattern("stress/*", cleanup_keys=False)

        assert agent_manager.get_agent("stress/test") is None
        assert redis_client.llen("c3po:inbox:stress/test") == 1

    def test_sub_pattern_matching(self, agent_manager):
        """Should support sub-patterns like stress/sender-*."""
        agent_manager.register_agent("stress/sender-0")
        agent_manager.register_agent("stress/sender-1")
        agent_manager.register_agent("stress/listener-0")

        removed = agent_manager.remove_agents_by_pattern("stress/sender-*")

        assert sorted(removed) == ["stress/sender-0", "stress/sender-1"]
        assert agent_manager.get_agent("stress/listener-0") is not None


class TestSetDescription:
    """Tests for agent description."""

    def test_set_description(self, agent_manager):
        """Should set description on registered agent."""
        agent_manager.register_agent("agent-a")
        result = agent_manager.set_description("agent-a", "Home automation controller")

        assert result["id"] == "agent-a"
        assert result["description"] == "Home automation controller"

    def test_set_description_updates(self, agent_manager):
        """Setting description twice should use the second value."""
        agent_manager.register_agent("agent-a")
        agent_manager.set_description("agent-a", "First description")
        result = agent_manager.set_description("agent-a", "Second description")

        assert result["description"] == "Second description"

    def test_set_description_unknown_agent(self, agent_manager):
        """Should raise KeyError for unknown agent."""
        with pytest.raises(KeyError, match="not found"):
            agent_manager.set_description("unknown-agent", "some description")

    def test_description_default_empty(self, agent_manager):
        """New agent should have empty description."""
        result = agent_manager.register_agent("agent-a")
        assert result["description"] == ""

    def test_description_in_list_agents(self, agent_manager):
        """Description should appear in list_agents output."""
        agent_manager.register_agent("agent-a")
        agent_manager.set_description("agent-a", "My description")

        agents = agent_manager.list_agents()
        assert len(agents) == 1
        assert agents[0]["description"] == "My description"


class TestAnonymousSessions:
    """Tests for anonymous session handling (Claude.ai/Desktop)."""

    def test_anonymous_chat_with_uuid_suffix_registers(self, agent_manager):
        """anonymous/chat-UUID pattern should register successfully."""
        agent_id = "anonymous/chat-a1b2c3d4"
        result = agent_manager.register_agent(agent_id, session_id="test-session")

        assert result["id"] == agent_id
        assert result["status"] == "online"

    def test_anonymous_chat_with_different_uuids_are_separate(self, agent_manager):
        """Different UUID suffixes create separate agent registrations."""
        agent1 = "anonymous/chat-uuid1"
        agent2 = "anonymous/chat-uuid2"

        result1 = agent_manager.register_agent(agent1, session_id="session-1")
        result2 = agent_manager.register_agent(agent2, session_id="session-2")

        assert result1["id"] == agent1
        assert result2["id"] == agent2

        agents = agent_manager.list_agents()
        assert len(agents) == 2
        assert {a["id"] for a in agents} == {agent1, agent2}

    def test_anonymous_chat_same_uuid_same_session_updates(self, agent_manager):
        """Same UUID and session should update existing agent, not create new."""
        agent_id = "anonymous/chat-myuuid"
        session_id = "test-session"

        result1 = agent_manager.register_agent(agent_id, session_id=session_id)
        original_registered_at = result1["registered_at"]

        time.sleep(0.01)

        result2 = agent_manager.register_agent(agent_id, session_id=session_id)

        # Should update existing, not create collision
        assert result2["id"] == agent_id
        assert result2["registered_at"] == original_registered_at
        assert result2["last_seen"] > result1["last_seen"]

    def test_anonymous_chat_same_uuid_different_session_creates_collision(self, agent_manager):
        """Same UUID but different session triggers collision resolution."""
        agent_id = "anonymous/chat-shared-uuid"

        result1 = agent_manager.register_agent(agent_id, session_id="session-1")
        assert result1["id"] == agent_id

        result2 = agent_manager.register_agent(agent_id, session_id="session-2")
        # Should get -2 suffix
        assert result2["id"] == f"{agent_id}-2"

    def test_anonymous_chat_accepts_any_suffix(self, agent_manager):
        """Should accept any suffix, not just UUID format."""
        test_cases = [
            "anonymous/chat-123abc",
            "anonymous/chat-my-project",
            "anonymous/chat-test",
            "anonymous/chat-a1b2c3d4-e5f6-7890-abcd-ef1234567890",
        ]

        for agent_id in test_cases:
            result = agent_manager.register_agent(agent_id)
            assert result["id"] == agent_id

        agents = agent_manager.list_agents()
        assert len(agents) == len(test_cases)
