"""Tests for C3PO coordinator server."""

import pytest

import fakeredis

from coordinator.server import _ping_impl, _list_agents_impl, _register_agent_impl
from coordinator.agents import AgentManager


@pytest.fixture
def redis_client():
    """Create a fresh fakeredis client for each test."""
    return fakeredis.FakeRedis()


@pytest.fixture
def agent_manager(redis_client):
    """Create AgentManager with fakeredis."""
    return AgentManager(redis_client)


class TestPing:
    """Tests for the ping tool."""

    def test_ping_returns_pong(self):
        """Ping should return pong=True."""
        result = _ping_impl()
        assert result["pong"] is True

    def test_ping_has_timestamp(self):
        """Ping should include a timestamp."""
        result = _ping_impl()
        assert "timestamp" in result
        assert isinstance(result["timestamp"], str)
        # Should be ISO format
        assert "T" in result["timestamp"]


class TestListAgents:
    """Tests for the list_agents tool."""

    def test_list_agents_returns_empty_list(self, agent_manager):
        """list_agents should return empty list when no agents registered."""
        result = _list_agents_impl(agent_manager)
        assert result == []
        assert isinstance(result, list)

    def test_list_agents_returns_registered(self, agent_manager):
        """list_agents should return agents after registration."""
        agent_manager.register_agent("test-agent")
        result = _list_agents_impl(agent_manager)

        assert len(result) == 1
        assert result[0]["id"] == "test-agent"


class TestRegisterAgent:
    """Tests for the register_agent tool."""

    def test_register_agent_basic(self, agent_manager):
        """register_agent should create agent entry."""
        result = _register_agent_impl(agent_manager, "my-agent")

        assert result["id"] == "my-agent"
        assert "registered_at" in result

    def test_register_agent_with_capabilities(self, agent_manager):
        """register_agent should store capabilities."""
        result = _register_agent_impl(
            agent_manager, "my-agent", capabilities=["search", "code"]
        )

        assert result["capabilities"] == ["search", "code"]
