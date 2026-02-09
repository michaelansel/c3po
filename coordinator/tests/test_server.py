"""Tests for C3PO coordinator server."""

import asyncio
import hmac
import hashlib
import json
from unittest.mock import Mock, patch

import pytest

import fakeredis

from coordinator.server import _ping_impl, _list_agents_impl, _register_agent_impl, _set_description_impl, _get_messages_impl, _wait_for_message_impl, _upload_blob_impl, _fetch_blob_impl, _register_webhook_impl, _unregister_webhook_impl, _fire_webhook, INLINE_BLOB_THRESHOLD
from coordinator.agents import AgentManager
from coordinator.blobs import BlobManager
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


class TestSetDescription:
    """Tests for the set_description tool."""

    def test_set_description_via_impl(self, agent_manager):
        """set_description should update agent description."""
        agent_manager.register_agent("my-agent")
        result = _set_description_impl(agent_manager, "my-agent", "Does cool stuff")

        assert result["description"] == "Does cool stuff"
        # Verify it persists in list
        agents = _list_agents_impl(agent_manager)
        assert agents[0]["description"] == "Does cool stuff"

    def test_set_description_unknown_agent(self, agent_manager):
        """set_description should raise ToolError for unknown agent."""
        with pytest.raises(ToolError):
            _set_description_impl(agent_manager, "nonexistent", "desc")


@pytest.fixture
def message_manager(redis_client):
    """Create MessageManager with fakeredis."""
    return MessageManager(redis_client)


class TestGetMessagesImpl:
    """Tests for _get_messages_impl server function."""

    def test_returns_messages(self, message_manager):
        """Should return messages from get_messages."""
        message_manager.send_message("a", "b", "hello")
        result = _get_messages_impl(message_manager, "b")
        assert len(result) == 1
        assert result[0]["type"] == "message"

    def test_invalid_type_raises(self, message_manager):
        """Should raise ToolError for invalid type parameter."""
        with pytest.raises(ToolError):
            _get_messages_impl(message_manager, "b", message_type="invalid")

    def test_none_type_returns_both(self, message_manager):
        """Should return both messages and replies when type is None."""
        req = message_manager.send_message("a", "b", "Q")
        message_manager.reply(req["id"], "b", "A")

        # Agent a gets replies, agent b gets messages
        msgs_a = _get_messages_impl(message_manager, "a", message_type=None)
        assert len(msgs_a) == 1
        assert msgs_a[0]["type"] == "reply"

    def test_legacy_type_request_accepted(self, message_manager):
        """Should accept legacy type='request' and return messages."""
        message_manager.send_message("a", "b", "hello")
        result = _get_messages_impl(message_manager, "b", message_type="request")
        assert len(result) == 1
        assert result[0]["type"] == "message"

    def test_legacy_type_response_accepted(self, message_manager):
        """Should accept legacy type='response' and return replies."""
        req = message_manager.send_message("a", "b", "Q")
        message_manager.reply(req["id"], "b", "A")
        result = _get_messages_impl(message_manager, "a", message_type="response")
        assert len(result) == 1
        assert result[0]["type"] == "reply"


class TestWaitForMessageImpl:
    """Tests for _wait_for_message_impl server function."""

    def test_returns_timeout_dict(self, message_manager):
        """Should return timeout dict when no messages arrive."""
        result = _wait_for_message_impl(message_manager, "agent-a", timeout=1)
        assert result["status"] == "timeout"
        assert "No messages received" in result["message"]

    def test_returns_received_dict(self, message_manager):
        """Should return received dict with messages."""
        message_manager.send_message("a", "b", "hello")
        result = _wait_for_message_impl(message_manager, "b", timeout=5)
        assert result["status"] == "received"
        assert len(result["messages"]) == 1

    def test_invalid_type_raises(self, message_manager):
        """Should raise ToolError for invalid type parameter."""
        with pytest.raises(ToolError):
            _wait_for_message_impl(message_manager, "b", timeout=1, message_type="invalid")

    def test_timeout_clamped_to_max(self, message_manager):
        """Timeout > 3600 should be clamped, not error. Pre-queue a message so it returns immediately."""
        message_manager.send_message("sender", "agent-a", "hello")
        result = _wait_for_message_impl(message_manager, "agent-a", timeout=9999)
        assert result["status"] == "received"
        assert len(result["messages"]) == 1

    def test_timeout_minimum_floor(self, message_manager):
        """Timeout <= 0 should be floored to 1, not error."""
        result = _wait_for_message_impl(message_manager, "agent-a", timeout=0)
        assert result["status"] == "timeout"

        result = _wait_for_message_impl(message_manager, "agent-a", timeout=-5)
        assert result["status"] == "timeout"

    def test_shutdown_event_returns_retry(self, message_manager):
        """Should return retry dict when shutdown_event is set."""
        import threading
        shutdown = threading.Event()
        shutdown.set()
        result = _wait_for_message_impl(
            message_manager, "agent-a", timeout=60,
            shutdown_event=shutdown,
        )
        assert result["status"] == "retry"
        assert result["retry_after"] == 15
        assert "restarting" in result["message"].lower()

    def test_shutdown_event_not_set_times_out(self, message_manager):
        """Should time out normally when shutdown_event exists but is not set."""
        import threading
        shutdown = threading.Event()
        result = _wait_for_message_impl(
            message_manager, "agent-a", timeout=1,
            shutdown_event=shutdown,
        )
        assert result["status"] == "timeout"


class TestWaitForMessageCancelledError:
    """Tests for CancelledError handling in the async wait_for_message wrapper."""

    @pytest.mark.asyncio
    async def test_cancelled_error_returns_retry(self):
        """When uvicorn cancels the executor future, wait_for_message should return retry dict."""
        from unittest.mock import MagicMock, patch

        # Import the async tool function (decorated with @mcp.tool())
        from coordinator.server import wait_for_message

        # Access the underlying coroutine function from the FunctionTool wrapper
        fn = wait_for_message.fn

        # Mock context with agent identity
        mock_ctx = MagicMock()
        mock_ctx.session.state = {"agent_id": "test/agent"}

        # Patch _resolve_agent_id and _enforce_agent_pattern to skip auth checks,
        # then patch run_in_executor to raise CancelledError
        with patch("coordinator.server._resolve_agent_id", return_value="test/agent"), \
             patch("coordinator.server._enforce_agent_pattern"):
            # Create a future that raises CancelledError
            fut = asyncio.get_running_loop().create_future()
            fut.cancel()
            with patch.object(asyncio.get_running_loop(), "run_in_executor", return_value=fut):
                result = await fn(mock_ctx, timeout=60)

        assert result["status"] == "retry"
        assert result["retry_after"] == 15
        assert "restarting" in result["message"].lower()


@pytest.fixture
def blob_manager(redis_client):
    """Create BlobManager with fakeredis."""
    return BlobManager(redis_client)


class TestUploadBlobImpl:
    """Tests for _upload_blob_impl server function."""

    def test_upload_returns_metadata(self, blob_manager):
        """Should store blob and return metadata."""
        result = _upload_blob_impl(blob_manager, b"hello", "test.txt", "text/plain", "agent/a")
        assert result["blob_id"].startswith("blob-")
        assert result["filename"] == "test.txt"
        assert result["size"] == 5

    def test_upload_too_large_raises(self, blob_manager):
        """Should raise ToolError for oversized blob."""
        with pytest.raises(ToolError, match="exceeds maximum"):
            _upload_blob_impl(blob_manager, b"x" * (5 * 1024 * 1024 + 1), "big.bin")


class TestFetchBlobImpl:
    """Tests for _fetch_blob_impl server function."""

    def test_fetch_small_text_blob_inline(self, blob_manager):
        """Small text blobs should return content inline with utf-8 encoding."""
        meta = blob_manager.store_blob(b"hello world", "test.txt", "text/plain")
        result = _fetch_blob_impl(blob_manager, meta["blob_id"])
        assert result["content"] == "hello world"
        assert result["encoding"] == "utf-8"

    def test_fetch_small_binary_blob_inline(self, blob_manager):
        """Small binary blobs should return base64-encoded content inline."""
        import base64
        data = bytes(range(256))
        meta = blob_manager.store_blob(data, "data.bin")
        result = _fetch_blob_impl(blob_manager, meta["blob_id"])
        assert result["encoding"] == "base64"
        assert base64.b64decode(result["content"]) == data

    def test_fetch_large_blob_metadata_only(self, blob_manager):
        """Large blobs should return metadata and download_url, not content."""
        data = b"x" * (INLINE_BLOB_THRESHOLD + 1)
        meta = blob_manager.store_blob(data, "large.bin")
        result = _fetch_blob_impl(blob_manager, meta["blob_id"], coordinator_url="https://example.com")
        assert "content" not in result
        assert "download_url" in result
        assert result["download_url"].startswith("https://example.com/agent/api/blob/")
        assert "note" in result

    def test_fetch_not_found_raises(self, blob_manager):
        """Should raise ToolError for non-existent blob."""
        with pytest.raises(ToolError, match="not found"):
            _fetch_blob_impl(blob_manager, "blob-doesnotexist")

    def test_large_blob_note_mentions_alternatives(self, blob_manager):
        """Large blob note should mention alternatives for clients without shell access."""
        data = b"x" * (INLINE_BLOB_THRESHOLD + 1)
        meta = blob_manager.store_blob(data, "large.bin")
        result = _fetch_blob_impl(blob_manager, meta["blob_id"])
        assert "smaller pieces" in result["note"] or "split" in result["note"]


class MockContext:
    """Mock FastMCP Context for testing _resolve_agent_id."""
    def __init__(self, state=None):
        self.state = state or {}

    def get_state(self, key):
        return self.state.get(key)


class TestResolveAgentId:
    """Tests for _resolve_agent_id function."""

    def test_explicit_agent_id_accepted(self, agent_manager):
        """Should accept explicit agent_id parameter."""
        from coordinator.server import _resolve_agent_id
        ctx = MockContext({"agent_id": "placeholder", "session_id": "test-session"})
        result = _resolve_agent_id(ctx, explicit_agent_id="macbook/myproject")
        assert result == "macbook/myproject"

    def test_bare_anonymous_chat_rejected(self, agent_manager):
        """Should reject bare 'anonymous/chat' with onboarding error."""
        from coordinator.server import _resolve_agent_id
        ctx = MockContext({"agent_id": "anonymous", "session_id": "test-session"})

        with pytest.raises(ToolError) as exc_info:
            _resolve_agent_id(ctx, explicit_agent_id="anonymous/chat")

        error_msg = str(exc_info.value)
        assert "shared anonymous agent ID" in error_msg
        assert "uuidgen" in error_msg
        assert "agent_id=" in error_msg

    def test_anonymous_chat_with_uuid_accepted(self, agent_manager):
        """Should accept anonymous/chat-UUID pattern."""
        from coordinator.server import _resolve_agent_id
        ctx = MockContext({"agent_id": "anonymous", "session_id": "test-session"})
        result = _resolve_agent_id(ctx, explicit_agent_id="anonymous/chat-a1b2c3d4")
        assert result == "anonymous/chat-a1b2c3d4"

    def test_anonymous_chat_with_custom_suffix_accepted(self, agent_manager):
        """Should accept anonymous/chat with any suffix."""
        from coordinator.server import _resolve_agent_id
        ctx = MockContext({"agent_id": "anonymous", "session_id": "test-session"})

        test_cases = [
            "anonymous/chat-123abc",
            "anonymous/chat-my-project",
            "anonymous/chat-test",
        ]

        for agent_id in test_cases:
            result = _resolve_agent_id(ctx, explicit_agent_id=agent_id)
            assert result == agent_id

    def test_anonymous_placeholder_without_explicit_id_rejected(self, agent_manager):
        """Should reject anonymous placeholder when no explicit agent_id provided."""
        from coordinator.server import _resolve_agent_id
        ctx = MockContext({"agent_id": "anonymous", "session_id": "test-session"})

        with pytest.raises(ToolError) as exc_info:
            _resolve_agent_id(ctx, explicit_agent_id=None)

        error_msg = str(exc_info.value)
        assert "shared anonymous agent ID" in error_msg or "unique ID" in error_msg

    def test_middleware_fallback_with_slash(self, agent_manager):
        """Should accept middleware ID if it contains a slash."""
        from coordinator.server import _resolve_agent_id
        ctx = MockContext({"agent_id": "macbook/myproject", "session_id": "test-session"})
        result = _resolve_agent_id(ctx, explicit_agent_id=None)
        assert result == "macbook/myproject"


class TestRegisterWebhook:
    """Tests for webhook registration."""

    def test_register_webhook(self, agent_manager):
        """Should register webhook for an agent."""
        agent_manager.register_agent("agent-a")
        result = _register_webhook_impl(
            agent_manager,
            "agent-a",
            "https://example.com/webhook",
            "secret-at-least-16ch"
        )

        assert result["id"] == "agent-a"
        assert result["webhook_url"] == "https://example.com/webhook"
        # Secret should be stripped from returned data
        assert "webhook_secret" not in result

    def test_register_webhook_nonexistent_agent(self, agent_manager):
        """Should raise ToolError for nonexistent agent."""
        with pytest.raises(ToolError):
            _register_webhook_impl(
                agent_manager,
                "nonexistent",
                "https://example.com",
                "secret"
            )


class TestWebhookFiring:
    """Tests for webhook HTTP calls."""

    @patch('coordinator.server.httpx.Client')
    def test_fire_webhook_calls_url_with_signature(self, mock_client):
        """Should POST to webhook URL with HMAC signature."""
        mock_instance = Mock()
        mock_client.return_value.__enter__.return_value = mock_instance

        _fire_webhook("agent-a", "https://example.com/hook", "secret-at-least-16ch")

        # Give background thread time to execute
        import time
        time.sleep(0.1)

        # Verify POST was called
        assert mock_instance.post.called
        call_args = mock_instance.post.call_args

        # Check URL
        assert call_args[0][0] == "https://example.com/hook"

        # Check body
        body = call_args[1]["content"]
        assert body == b'{"agent_id": "agent-a"}'

        # Check signature
        headers = call_args[1]["headers"]
        expected_sig = hmac.new(
            b"secret-at-least-16ch",
            body,
            hashlib.sha256
        ).hexdigest()
        assert headers["X-C3PO-Signature"] == expected_sig

    @patch('coordinator.server.httpx.Client')
    def test_fire_webhook_does_not_raise_on_error(self, mock_client):
        """Webhook errors should be logged but not raised."""
        mock_instance = Mock()
        mock_instance.post.side_effect = Exception("Connection failed")
        mock_client.return_value.__enter__.return_value = mock_instance

        # Should not raise
        _fire_webhook("agent-a", "https://example.com/hook", "secret-at-least-16ch")

        import time
        time.sleep(0.1)  # Let background thread complete


class TestUnregisterWebhook:
    """Tests for webhook unregistration."""

    def test_unregister_webhook(self, agent_manager):
        """Should clear webhook fields for an agent."""
        agent_manager.register_agent("agent-a")
        agent_manager.set_webhook("agent-a", "https://example.com/hook", "secret123")
        result = _unregister_webhook_impl(agent_manager, "agent-a")

        assert result["id"] == "agent-a"
        assert result["webhook_url"] == ""
        # Secret should be stripped from returned data
        assert "webhook_secret" not in result

    def test_unregister_webhook_nonexistent_agent(self, agent_manager):
        """Should raise ToolError for nonexistent agent."""
        with pytest.raises(ToolError):
            _unregister_webhook_impl(agent_manager, "nonexistent")


class TestWebhookValidation:
    """Tests for webhook input validation."""

    def test_rejects_invalid_url(self, agent_manager):
        """Should reject URLs that don't start with http/https."""
        agent_manager.register_agent("agent-a")
        with pytest.raises(ToolError, match="HTTP"):
            _register_webhook_impl(agent_manager, "agent-a", "ftp://bad", "secret-at-least-16ch")

    def test_rejects_short_secret(self, agent_manager):
        """Should reject secrets shorter than 16 chars."""
        agent_manager.register_agent("agent-a")
        with pytest.raises(ToolError, match="16 characters"):
            _register_webhook_impl(agent_manager, "agent-a", "https://example.com", "short")

    def test_accepts_minimum_secret_length(self, agent_manager):
        """Should accept secrets of exactly 16 chars."""
        agent_manager.register_agent("agent-a")
        result = _register_webhook_impl(
            agent_manager, "agent-a", "https://example.com", "exactly16chars!!"
        )
        assert result["webhook_url"] == "https://example.com"


class TestSecretStripping:
    """Tests that webhook_secret is never exposed in tool responses."""

    def test_list_agents_strips_secret(self, agent_manager):
        """list_agents should not include webhook_secret."""
        agent_manager.register_agent("agent-a")
        agent_manager.set_webhook("agent-a", "https://example.com", "topsecretvalue!!")
        agents = _list_agents_impl(agent_manager)
        assert len(agents) == 1
        assert "webhook_secret" not in agents[0]
        assert agents[0]["webhook_url"] == "https://example.com"

    def test_register_agent_strips_secret(self, agent_manager):
        """register_agent should not include webhook_secret."""
        result = _register_agent_impl(agent_manager, "agent-a")
        assert "webhook_secret" not in result

    def test_set_description_strips_secret(self, agent_manager):
        """set_description should not include webhook_secret."""
        agent_manager.register_agent("agent-a")
        agent_manager.set_webhook("agent-a", "https://example.com", "topsecretvalue!!")
        result = _set_description_impl(agent_manager, "agent-a", "desc")
        assert "webhook_secret" not in result
