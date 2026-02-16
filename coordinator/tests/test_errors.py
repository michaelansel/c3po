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
    _validate_message_id,
    _wait_for_message_impl,
    _ack_messages_impl,
    MAX_MESSAGE_LENGTH,
    MAX_WAIT_TIMEOUT,
)
from coordinator.agents import AgentManager
from coordinator.messaging import MessageManager
from coordinator.rate_limit import RateLimiter, RATE_LIMITS
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

    def test_valid_message_id(self):
        """Valid message IDs pass validation."""
        # Should not raise
        _validate_message_id("homeassistant::meshtastic::abc12345")

    def test_invalid_message_id_empty(self):
        """Empty message_id raises error."""
        with pytest.raises(ToolError) as exc_info:
            _validate_message_id("")
        assert "must be a non-empty" in str(exc_info.value)

    def test_invalid_message_id_missing_parts(self):
        """Message_id with wrong number of parts raises error."""
        with pytest.raises(ToolError) as exc_info:
            _validate_message_id("single-part")
        assert "invalid format" in str(exc_info.value)

        with pytest.raises(ToolError) as exc_info:
            _validate_message_id("part1::part2")
        assert "invalid format" in str(exc_info.value)

    def test_invalid_message_id_empty_from_agent(self):
        """Message_id with empty from_agent raises error."""
        with pytest.raises(ToolError) as exc_info:
            _validate_message_id("::to-agent::abc12345")
        assert "from_agent" in str(exc_info.value)

    def test_invalid_message_id_empty_to_agent(self):
        """Message_id with empty to_agent raises error."""
        with pytest.raises(ToolError) as exc_info:
            _validate_message_id("from-agent::abc12345")
        assert "to_agent" in str(exc_info.value)

    def test_invalid_message_id_too_long_from_agent(self):
        """Message_id with too long from_agent raises error."""
        long_agent = "a" * 65
        with pytest.raises(ToolError) as exc_info:
            _validate_message_id(f"{long_agent}::to-agent::abc12345")
        assert "64 characters" in str(exc_info.value)

    def test_invalid_message_id_too_long_to_agent(self):
        """Message_id with too long to_agent raises error."""
        long_agent = "b" * 65
        with pytest.raises(ToolError) as exc_info:
            _validate_message_id(f"from-agent::{long_agent}::abc12345")
        assert "64 characters" in str(exc_info.value)

    def test_invalid_message_id_non_hex_uuid(self):
        """Message_id with non-hex UUID raises error."""
        with pytest.raises(ToolError) as exc_info:
            _validate_message_id("from-agent::to-agent::invalid")
        assert "UUID" in str(exc_info.value)

    def test_invalid_message_id_wrong_uuid_length(self):
        """Message_id with wrong UUID length raises error."""
        with pytest.raises(ToolError) as exc_info:
            _validate_message_id("from-agent::to-agent::short")
        assert "UUID" in str(exc_info.value)

    def test_invalid_message_id_too_long_uuid(self):
        """Message_id with too long UUID raises error."""
        with pytest.raises(ToolError) as exc_info:
            _validate_message_id("from-agent::to-agent::verylonguuid12345678")
        assert "UUID" in str(exc_info.value)

    def test_wait_for_message_timeout_clamped_to_min(self, message_manager):
        """wait_for_message clamps timeout less than 1 second to 1."""
        result = _wait_for_message_impl(
            message_manager,
            "test-agent",
            timeout=0
        )
        assert result["status"] == "timeout"

    def test_wait_for_message_timeout_too_large(self, message_manager):
        """wait_for_message rejects timeout greater than MAX_WAIT_TIMEOUT."""
        with pytest.raises(ToolError) as exc_info:
            _wait_for_message_impl(
                message_manager,
                "test-agent",
                timeout=MAX_WAIT_TIMEOUT + 1
            )
        assert str(MAX_WAIT_TIMEOUT) in str(exc_info.value)

    def test_ack_messages_single_invalid_id(self, message_manager):
        """ack_messages rejects single invalid ID."""
        with pytest.raises(ToolError) as exc_info:
            _ack_messages_impl(
                message_manager,
                "test-agent",
                ["invalid-format"]
            )
        assert "invalid ID(s)" in str(exc_info.value)

    def test_ack_messages_multiple_invalid_ids(self, message_manager):
        """ack_messages rejects multiple invalid IDs."""
        with pytest.raises(ToolError) as exc_info:
            _ack_messages_impl(
                message_manager,
                "test-agent",
                ["valid::valid::abc12345", "invalid1", "invalid2"]
            )
        assert "contains 2 invalid ID(s)" in str(exc_info.value)

    def test_ack_messages_mixed_valid_invalid(self, message_manager):
        """ack_messages rejects list with mixed valid/invalid IDs."""
        with pytest.raises(ToolError) as exc_info:
            _ack_messages_impl(
                message_manager,
                "test-agent",
                ["valid::valid::abc12345", "another::valid::def67890", "bad-format"]
            )
        assert "contains 1 invalid ID(s)" in str(exc_info.value)

    def test_ack_messages_empty_list(self, message_manager):
        """ack_messages with empty list returns early."""
        result = _ack_messages_impl(
            message_manager,
            "test-agent",
            []
        )
        assert result["acked"] == 0
        assert result["compacted"] is False


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
        self, redis_client, message_manager, agent_manager
    ):
        """send_message rejects when rate limited."""
        import coordinator.server as server_module

        agent_manager.register_agent("sender")
        agent_manager.register_agent("target")

        # Use a RateLimiter backed by the same fakeredis
        test_rate_limiter = RateLimiter(redis_client)
        original = server_module.rate_limiter
        server_module.rate_limiter = test_rate_limiter

        try:
            # Max out the central rate limit for send_message
            limit = RATE_LIMITS["send_message"][0]
            for _ in range(limit):
                test_rate_limiter.check_and_record("send_message", "sender")

            with pytest.raises(ToolError) as exc_info:
                _send_message_impl(
                    message_manager,
                    agent_manager,
                    "sender",
                    "target",
                    "Hello"
                )
            assert "Rate limit exceeded" in str(exc_info.value)
        finally:
            server_module.rate_limiter = original


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
        # reply() returns data with "message" field, not "response"
        assert result["message"] == "This is my response"
        assert result["reply_to"] == "sender::receiver::12345678"
        assert result["from_agent"] == "receiver"
        assert result["to_agent"] == "sender"


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
