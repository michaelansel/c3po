"""Tests for C3PO coordinator server."""

import pytest

import fakeredis

from coordinator.server import _ping_impl, _list_agents_impl, _register_agent_impl, _get_messages_impl, _wait_for_message_impl
from coordinator.agents import AgentManager
from coordinator.messaging import MessageManager
from fastmcp.exceptions import ToolError


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


@pytest.fixture
def message_manager(redis_client):
    """Create MessageManager with fakeredis."""
    return MessageManager(redis_client)


class TestGetMessagesImpl:
    """Tests for _get_messages_impl server function."""

    def test_returns_messages(self, message_manager):
        """Should return messages from get_messages."""
        message_manager.send_request("a", "b", "hello")
        result = _get_messages_impl(message_manager, "b")
        assert len(result) == 1
        assert result[0]["type"] == "request"

    def test_invalid_type_raises(self, message_manager):
        """Should raise ToolError for invalid type parameter."""
        with pytest.raises(ToolError):
            _get_messages_impl(message_manager, "b", message_type="invalid")

    def test_none_type_returns_both(self, message_manager):
        """Should return both requests and responses when type is None."""
        req = message_manager.send_request("a", "b", "Q")
        message_manager.respond_to_request(req["id"], "b", "A")

        # Agent a gets responses, agent b gets requests
        msgs_a = _get_messages_impl(message_manager, "a", message_type=None)
        assert len(msgs_a) == 1
        assert msgs_a[0]["type"] == "response"


class TestWaitForMessageImpl:
    """Tests for _wait_for_message_impl server function."""

    def test_returns_timeout_dict(self, message_manager):
        """Should return timeout dict when no messages arrive."""
        result = _wait_for_message_impl(message_manager, "agent-a", timeout=1)
        assert result["status"] == "timeout"
        assert "No messages received" in result["message"]

    def test_returns_received_dict(self, message_manager):
        """Should return received dict with messages."""
        message_manager.send_request("a", "b", "hello")
        result = _wait_for_message_impl(message_manager, "b", timeout=5)
        assert result["status"] == "received"
        assert len(result["messages"]) == 1

    def test_invalid_type_raises(self, message_manager):
        """Should raise ToolError for invalid type parameter."""
        with pytest.raises(ToolError):
            _wait_for_message_impl(message_manager, "b", timeout=1, message_type="invalid")
