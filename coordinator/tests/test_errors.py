"""Tests for C3PO error handling."""

import pytest
import fakeredis
from datetime import datetime, timezone, timedelta
import json

from coordinator.server import (
    _send_message_impl,
    _reply_impl,
    _validate_agent_id,
    _validate_message,
    MAX_MESSAGE_LENGTH,
)
from coordinator.agents import AgentManager
from coordinator.messaging import MessageManager
from coordinator.errors import (
    ErrorCodes,
    agent_not_found,
    invalid_request,
    rate_limited,
    redis_unavailable,
    RedisConnectionError,
)
from fastmcp.exceptions import ToolError


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


class TestErrorStructure:
    """Tests for structured error responses."""

    def test_agent_not_found_error(self):
        """Error includes code, message, and suggestion."""
        err = agent_not_found("test-agent", ["agent-a", "agent-b"])
        result = err.to_dict()

        assert result["code"] == ErrorCodes.AGENT_NOT_FOUND
        assert "test-agent" in result["error"]
        assert "suggestion" in result
        assert "agent-a" in result["suggestion"]

    def test_agent_not_found_empty_list(self):
        """Error handles empty agent list."""
        err = agent_not_found("test-agent", [])
        result = err.to_dict()

        assert "No agents are currently registered" in result["suggestion"]

    def test_invalid_request_error(self):
        """Invalid request includes field info."""
        err = invalid_request("message", "cannot be empty")
        result = err.to_dict()

        assert result["code"] == ErrorCodes.INVALID_REQUEST
        assert "message" in result["error"]
        assert "cannot be empty" in result["error"]

    def test_rate_limited_error(self):
        """Rate limit error includes limit info."""
        err = rate_limited("test-agent", 10, 60)
        result = err.to_dict()

        assert result["code"] == ErrorCodes.RATE_LIMITED
        assert "test-agent" in result["error"]
        assert "10" in result["suggestion"]


class TestValidation:
    """Tests for input validation."""

    def test_valid_agent_id(self):
        """Valid agent IDs pass validation."""
        # Should not raise
        _validate_agent_id("test-agent")
        _validate_agent_id("Agent123")
        _validate_agent_id("my_agent.v2")
        _validate_agent_id("a")  # Single char

    def test_invalid_agent_id_empty(self):
        """Empty agent ID raises error."""
        with pytest.raises(ToolError) as exc_info:
            _validate_agent_id("")
        assert "cannot be empty" in str(exc_info.value)

    def test_invalid_agent_id_special_start(self):
        """Agent ID starting with special char raises error."""
        with pytest.raises(ToolError) as exc_info:
            _validate_agent_id("-invalid")
        assert "alphanumeric" in str(exc_info.value)

    def test_invalid_agent_id_too_long(self):
        """Agent ID over 64 chars raises error."""
        long_id = "a" * 65
        with pytest.raises(ToolError) as exc_info:
            _validate_agent_id(long_id)
        assert "1-64" in str(exc_info.value)

    def test_valid_message(self):
        """Valid messages pass validation."""
        _validate_message("Hello, world!")
        _validate_message("  spaces  ")

    def test_invalid_message_empty(self):
        """Empty message raises error."""
        with pytest.raises(ToolError) as exc_info:
            _validate_message("")
        assert "cannot be empty" in str(exc_info.value)

    def test_invalid_message_whitespace_only(self):
        """Whitespace-only message raises error."""
        with pytest.raises(ToolError) as exc_info:
            _validate_message("   ")
        assert "cannot be empty" in str(exc_info.value)

    def test_invalid_message_too_long(self):
        """Message over limit raises error."""
        long_msg = "a" * (MAX_MESSAGE_LENGTH + 1)
        with pytest.raises(ToolError) as exc_info:
            _validate_message(long_msg)
        assert "maximum length" in str(exc_info.value)


class TestRateLimiting:
    """Tests for rate limiting."""

    def test_rate_limit_allows_under_limit(self, message_manager):
        """Requests under limit are allowed."""
        is_allowed, count = message_manager.check_rate_limit("test-agent")
        assert is_allowed
        assert count == 0

    def test_rate_limit_blocks_over_limit(self, message_manager):
        """Requests over limit are blocked."""
        # Record enough requests to hit the limit
        for _ in range(message_manager.RATE_LIMIT_REQUESTS):
            message_manager.record_request("test-agent")

        is_allowed, count = message_manager.check_rate_limit("test-agent")
        assert not is_allowed
        assert count == message_manager.RATE_LIMIT_REQUESTS

    def test_rate_limit_per_agent(self, message_manager):
        """Rate limits are per-agent."""
        # Max out agent-a
        for _ in range(message_manager.RATE_LIMIT_REQUESTS):
            message_manager.record_request("agent-a")

        # agent-b should still be allowed
        is_allowed, _ = message_manager.check_rate_limit("agent-b")
        assert is_allowed

    def test_send_message_rate_limited(
        self, message_manager, agent_manager
    ):
        """send_message rejects when rate limited."""
        agent_manager.register_agent("sender")
        agent_manager.register_agent("target")

        # Max out the rate limit
        for _ in range(message_manager.RATE_LIMIT_REQUESTS):
            message_manager.record_request("sender")

        with pytest.raises(ToolError) as exc_info:
            _send_message_impl(
                message_manager,
                agent_manager,
                "sender",
                "target",
                "Hello"
            )
        assert "Rate limit exceeded" in str(exc_info.value)


class TestMessageExpiration:
    """Tests for message TTL/expiration."""

    def test_message_not_expired(self, message_manager):
        """Recent messages are not expired."""
        now = datetime.now(timezone.utc).isoformat()
        message = {"timestamp": now, "message": "test"}

        assert not message_manager._is_message_expired(message)

    def test_message_expired(self, message_manager):
        """Old messages are expired."""
        # Create timestamp older than TTL
        old_time = datetime.now(timezone.utc) - timedelta(
            seconds=message_manager.MESSAGE_TTL_SECONDS + 1
        )
        message = {"timestamp": old_time.isoformat(), "message": "test"}

        assert message_manager._is_message_expired(message)

    def test_expired_messages_filtered(self, message_manager, redis_client):
        """Expired messages are filtered from pending requests."""
        # Directly inject an expired message
        old_time = datetime.now(timezone.utc) - timedelta(
            seconds=message_manager.MESSAGE_TTL_SECONDS + 1
        )
        old_message = {
            "id": "old-msg",
            "from_agent": "sender",
            "to_agent": "receiver",
            "message": "old message",
            "timestamp": old_time.isoformat(),
            "status": "pending",
        }

        # Also add a fresh message
        now = datetime.now(timezone.utc).isoformat()
        new_message = {
            "id": "new-msg",
            "from_agent": "sender",
            "to_agent": "receiver",
            "message": "new message",
            "timestamp": now,
            "status": "pending",
        }

        inbox_key = f"{message_manager.INBOX_PREFIX}receiver"
        redis_client.rpush(inbox_key, json.dumps(old_message))
        redis_client.rpush(inbox_key, json.dumps(new_message))

        # Peek should only show the fresh message
        pending = message_manager.peek_pending_messages("receiver")
        assert len(pending) == 1
        assert pending[0]["id"] == "new-msg"

        # Get should also only return the fresh message
        pending = message_manager.get_pending_messages("receiver")
        assert len(pending) == 1
        assert pending[0]["id"] == "new-msg"


class TestReplyValidation:
    """Tests for reply validation."""

    def test_empty_response_rejected(self, message_manager):
        """Empty response raises error."""
        with pytest.raises(ToolError) as exc_info:
            _reply_impl(
                message_manager,
                "from-agent",
                "sender::receiver::12345678",
                ""
            )
        assert "cannot be empty" in str(exc_info.value)

    def test_invalid_message_id_rejected(self, message_manager):
        """Invalid message_id format raises error."""
        with pytest.raises(ToolError) as exc_info:
            _reply_impl(
                message_manager,
                "from-agent",
                "invalid-format",
                "response"
            )
        assert "invalid format" in str(exc_info.value)

    def test_valid_reply_accepted(self, message_manager):
        """Valid reply is accepted."""
        result = _reply_impl(
            message_manager,
            "receiver",
            "sender::receiver::12345678",
            "This is my response"
        )
        assert result["response"] == "This is my response"


class TestRedisErrorHandling:
    """Tests for Redis connection error messages."""

    def test_redis_unavailable_parses_host_port(self):
        """Error parses host:port from Redis URL."""
        err = redis_unavailable("redis://localhost:6379")
        result = err.to_dict()

        assert result["code"] == ErrorCodes.REDIS_UNAVAILABLE
        assert "localhost:6379" in result["error"]
        assert "Ensure Redis is running" in result["suggestion"]

    def test_redis_unavailable_default_port(self):
        """Error uses default port when not specified."""
        err = redis_unavailable("redis://myhost")
        result = err.to_dict()

        assert "myhost:6379" in result["error"]

    def test_redis_unavailable_includes_original_error(self):
        """Error includes original error message."""
        err = redis_unavailable(
            "redis://localhost:6379",
            "Connection refused"
        )
        result = err.to_dict()

        assert "Connection refused" in result["error"]
        assert "localhost:6379" in result["error"]

    def test_redis_connection_error_exception(self):
        """RedisConnectionError has actionable message."""
        original = ConnectionError("Connection refused")
        exc = RedisConnectionError("redis://localhost:6379", original)

        error_str = str(exc)
        assert "localhost:6379" in error_str
        assert "Ensure Redis is running" in error_str
        assert "Connection refused" in error_str

    def test_redis_unavailable_non_standard_url(self):
        """Error handles non-standard URL format gracefully."""
        err = redis_unavailable("unix:///var/run/redis.sock")
        result = err.to_dict()

        # Should fall back to showing the full URL
        assert "unix:///var/run/redis.sock" in result["error"]
