"""Tests for C3PO messaging functionality with single queue architecture."""

import json
import pytest
import fakeredis
import threading
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from coordinator.messaging import MessageManager
from coordinator.agents import AgentManager
from coordinator.rate_limit import RateLimiter
from coordinator.server import (
    _send_message_impl,
    _get_messages_impl,
    _reply_impl,
    _wait_for_message_impl,
    _ack_messages_impl,
    _get_message_impl,
    _get_thread_impl,
)
from fastmcp.exceptions import ToolError


@pytest.fixture
def redis_client():
    """Create a fresh fakeredis client for each test."""
    return fakeredis.FakeRedis()


@pytest.fixture
def message_manager(redis_client):
    """Create MessageManager with fakeredis."""
    return MessageManager(redis_client)


@pytest.fixture
def agent_manager(redis_client):
    """Create AgentManager with fakeredis."""
    return AgentManager(redis_client)


@pytest.fixture(autouse=False)
def patch_rate_limiter(redis_client):
    """Patch server.rate_limiter to use fakeredis so tests don't need real Redis."""
    fake_limiter = RateLimiter(redis_client)
    with patch("coordinator.server.rate_limiter", fake_limiter):
        yield fake_limiter


class TestMessageManager:
    """Tests for the MessageManager class."""

    def test_send_message_creates_properly_formatted_message(self, message_manager):
        """send_message should create a properly formatted message in Redis."""
        result = message_manager.send_message(
            from_agent="agent-a",
            to_agent="agent-b",
            message="Hello, can you help?",
            context="Background info",
        )

        assert "id" in result
        assert result["from_agent"] == "agent-a"
        assert result["to_agent"] == "agent-b"
        assert result["message"] == "Hello, can you help?"
        assert result["context"] == "Background info"
        assert result["status"] == "pending"
        assert "timestamp" in result
        assert "reply_to" not in result  # No reply_to for normal messages
        # ID format should be {from}::{to}::{uuid}
        assert result["id"].startswith("agent-a::agent-b::")

    def test_get_pending_messages_retrieves_and_removes(self, message_manager):
        """get_pending_messages should retrieve and remove messages."""
        message_manager.send_message("a", "b", "message 1")
        message_manager.send_message("c", "b", "message 2")

        # First retrieval should get both
        messages = message_manager.get_pending_messages("b")
        assert len(messages) == 2
        assert messages[0]["message"] == "message 1"
        assert messages[1]["message"] == "message 2"

        # Second retrieval should be empty (consumed)
        messages = message_manager.get_pending_messages("b")
        assert len(messages) == 0

    def test_multiple_messages_queue_fifo(self, message_manager):
        """Messages should queue in FIFO order."""
        message_manager.send_message("a", "b", "first")
        message_manager.send_message("c", "b", "second")
        message_manager.send_message("d", "b", "third")

        messages = message_manager.get_pending_messages("b")
        assert len(messages) == 3
        assert messages[0]["message"] == "first"
        assert messages[1]["message"] == "second"
        assert messages[2]["message"] == "third"

    def test_empty_inbox_returns_empty_list(self, message_manager):
        """Empty inbox should return empty list."""
        messages = message_manager.get_pending_messages("nonexistent")
        assert messages == []

    def test_peek_pending_messages_does_not_consume(self, message_manager):
        """peek_pending_messages should not remove messages."""
        message_manager.send_message("a", "b", "hello")

        # Peek should return the message
        messages = message_manager.peek_pending_messages("b")
        assert len(messages) == 1

        # Peek again should still return the message
        messages = message_manager.peek_pending_messages("b")
        assert len(messages) == 1

        # Now consume it
        messages = message_manager.get_pending_messages("b")
        assert len(messages) == 1

        # Now it should be gone
        messages = message_manager.peek_pending_messages("b")
        assert len(messages) == 0

    def test_backwards_compat_aliases(self, message_manager):
        """Backwards compat aliases should work."""
        # send_request alias
        result = message_manager.send_request("a", "b", "test")
        assert result["from_agent"] == "a"

        # get_pending_requests alias
        msgs = message_manager.get_pending_requests("b")
        assert len(msgs) == 1

        # peek_pending_requests alias
        message_manager.send_message("a", "b", "test2")
        msgs = message_manager.peek_pending_requests("b")
        assert len(msgs) == 1


class TestSendMessageTool:
    """Tests for the send_message tool implementation."""

    @pytest.fixture(autouse=True)
    def _patch_rl(self, patch_rate_limiter):
        """Patch rate_limiter so these tests don't need a real Redis."""

    def test_send_message_to_existing_agent(self, message_manager, agent_manager):
        """send_message should work when target agent exists."""
        # Register the target agent
        agent_manager.register_agent("agent-b")

        result = _send_message_impl(
            message_manager,
            agent_manager,
            from_agent="agent-a",
            to="agent-b",
            message="Help me please",
        )

        assert result["from_agent"] == "agent-a"
        assert result["to_agent"] == "agent-b"
        assert result["message"] == "Help me please"
        assert "type" not in result  # No type field should be assigned

    def test_send_message_to_unknown_agent_returns_error(
        self, message_manager, agent_manager
    ):
        """send_message to unknown agent should return helpful error with deliver_offline hint."""
        # Register a different agent
        agent_manager.register_agent("agent-c")

        with pytest.raises(ToolError) as exc_info:
            _send_message_impl(
                message_manager,
                agent_manager,
                from_agent="agent-a",
                to="agent-b",
                message="Hello?",
            )

        error_msg = str(exc_info.value)
        assert "agent-b" in error_msg
        assert "not found" in error_msg.lower()
        assert "agent-c" in error_msg  # Should list available agents
        assert "deliver_offline" in error_msg  # Should include hint

    def test_send_to_unregistered_with_deliver_offline_flag_succeeds(
        self, message_manager, agent_manager
    ):
        """send_message with deliver_offline=True should succeed for unregistered agents."""
        result = _send_message_impl(
            message_manager,
            agent_manager,
            from_agent="machine/sender",
            to="machine/ghost",
            message="nobody home but queued",
            deliver_offline=True,
        )

        assert result["to_agent"] == "machine/ghost"
        assert result["offline_delivery"] is True
        # Placeholder should have been created in registry
        agent = agent_manager.get_agent("machine/ghost")
        assert agent is not None
        assert agent["status"] == "offline"
        # Message should be in inbox
        msgs = message_manager.get_messages("machine/ghost")
        assert len(msgs) == 1
        assert msgs[0]["message"] == "nobody home but queued"

    def test_send_to_registered_offline_agent_sets_offline_delivery(
        self, message_manager, agent_manager, redis_client
    ):
        """send_message to a registered but offline agent should set offline_delivery=True."""
        import json as _json
        from datetime import datetime, timezone, timedelta
        agent_manager.register_agent("machine/sleeper")
        # Make offline
        old_time = (
            datetime.now(timezone.utc) - timedelta(seconds=agent_manager.AGENT_TIMEOUT_SECONDS + 60)
        ).isoformat()
        data = _json.loads(redis_client.hget(agent_manager.AGENTS_KEY, "machine/sleeper"))
        data["last_seen"] = old_time
        redis_client.hset(agent_manager.AGENTS_KEY, "machine/sleeper", _json.dumps(data))

        result = _send_message_impl(
            message_manager,
            agent_manager,
            from_agent="machine/sender",
            to="machine/sleeper",
            message="wake up",
        )

        assert result["offline_delivery"] is True
        assert "machine/sleeper" in result.get("note", "")

    def test_send_message_creates_single_queue_message(self, message_manager):
        """send_message should create message in single queue without reply_to."""
        result = message_manager.send_message("a", "b", "Hello")
        assert "reply_to" not in result

    def test_send_message_pushes_notification(self, message_manager, redis_client):
        """send_message should push a notification signal to the notify key."""
        message_manager.send_message("agent-a", "agent-b", "hello")

        notify_key = f"{message_manager.NOTIFY_PREFIX}agent-b"
        length = redis_client.llen(notify_key)
        assert length == 1


class TestReplyHandling:
    """Tests for reply functionality."""

    def test_reply_has_reply_to_field(self, message_manager):
        """reply should include a reply_to field pointing to original message."""
        msg = message_manager.send_message("agent-a", "agent-b", "Help?")
        result = message_manager.reply(
            message_id=msg["id"],
            from_agent="agent-b",
            response="Here's your answer",
            status="success",
        )

        assert result["reply_to"] == msg["id"]
        assert result["from_agent"] == "agent-b"
        assert result["to_agent"] == "agent-a"  # Original sender
        assert result["message"] == "Here's your answer"  # Response is in message field (consistent with send_message)
        assert result["status"] == "success"
        assert "timestamp" in result
        assert "id" in result  # Has id field like send_message

    def test_reply_creates_id_field(self, message_manager):
        """reply should create an id field like send_message."""
        msg = message_manager.send_message("a", "b", "Q")
        result = message_manager.reply(msg["id"], "b", "A")

        assert "id" in result
        assert result["id"].startswith("b::a::")  # Format: from::to::uuid
        assert result["reply_to"] == msg["id"]  # Points to original message
        assert result["message"] == "A"  # Response is in message field
        assert result["to_agent"] == "a"  # Goes to original sender

    def test_reply_pushes_notification(self, message_manager, redis_client):
        """reply should push a notification signal to the sender's notify key."""
        msg = message_manager.send_message("agent-a", "agent-b", "Q")

        # Clear the notification that send_message pushed to agent-b
        notify_b = f"{message_manager.NOTIFY_PREFIX}agent-b"
        redis_client.delete(notify_b)

        # Reply
        message_manager.reply(msg["id"], "agent-b", "A")

        # Check notification was pushed to agent-a (the original sender)
        notify_a = f"{message_manager.NOTIFY_PREFIX}agent-a"
        length = redis_client.llen(notify_a)
        assert length == 1

    def test_reply_does_not_notify_responder(self, message_manager, redis_client):
        """reply should notify the original sender, not the responder."""
        msg = message_manager.send_message("agent-a", "agent-b", "Q")

        # Clear all notifications
        redis_client.delete(f"{message_manager.NOTIFY_PREFIX}agent-a")
        redis_client.delete(f"{message_manager.NOTIFY_PREFIX}agent-b")

        message_manager.reply(msg["id"], "agent-b", "A")

        # agent-a should have a notification from reply
        assert redis_client.llen(f"{message_manager.NOTIFY_PREFIX}agent-a") == 1
        # agent-b should NOT have a notification from reply (send_message notification was already cleared)

    def test_reply_rejects_unauthorized_agent(self, message_manager):
        """reply should reject an agent that is not the original recipient."""
        # agent-a sends to agent-b
        msg = message_manager.send_message("agent-a", "agent-b", "Help?")
        message_id = msg["id"]

        # agent-c tries to reply (not the recipient)
        with pytest.raises(ValueError, match="not the recipient"):
            message_manager.reply(
                message_id=message_id,
                from_agent="agent-c",
                response="Intercepted!",
            )

    def test_reply_rejects_crafted_message_id(self, message_manager):
        """reply should reject a crafted message_id targeting another agent's queue."""
        # Craft a fake message_id that would route reply to agent-x's queue
        crafted_id = "agent-x::agent-y::fakeuuid1"

        # agent-z tries to reply using the crafted ID (agent-z != agent-y)
        with pytest.raises(ValueError, match="not the recipient"):
            message_manager.reply(
                message_id=crafted_id,
                from_agent="agent-z",
                response="Injected reply",
            )

    def test_reply_with_error_status(self, message_manager):
        """reply should support error status."""
        msg = message_manager.send_message("agent-a", "agent-b", "Do something")
        message_id = msg["id"]

        result = message_manager.reply(
            message_id=message_id,
            from_agent="agent-b",
            response="Failed to do that",
            status="error",
        )

        assert result["status"] == "error"
        assert result["message"] == "Failed to do that"  # Response is in message field (consistent with send_message)


class TestGetMessages:
    """Tests for the unified get_messages method."""

    def test_returns_both_messages_and_replies(self, message_manager):
        """get_messages should return both messages and replies from single queue."""
        # Create a message to agent-b and a reply to agent-a
        msg = message_manager.send_message("agent-a", "agent-b", "Question?")
        message_manager.reply(msg["id"], "agent-b", "Answer!")

        # Agent-a should see the reply
        msgs_a = message_manager.get_messages("agent-a")
        assert len(msgs_a) == 1
        assert msgs_a[0]["message"] == "Answer!"  # Response is in message field (consistent with send_message)
        assert msgs_a[0]["to_agent"] == "agent-a"
        assert "type" not in msgs_a[0]  # No type field

        # Agent-b should see the message
        msgs_b = message_manager.get_messages("agent-b")
        assert len(msgs_b) == 1
        assert msgs_b[0]["message"] == "Question?"
        assert msgs_b[0]["from_agent"] == "agent-a"
        assert "type" not in msgs_b[0]  # No type field

    def test_non_destructive_without_ack(self, message_manager):
        """get_messages should return same messages until acked."""
        message_manager.send_message("agent-a", "agent-b", "Q")
        msgs = message_manager.get_messages("agent-b")
        assert len(msgs) == 1

        # Second call should return same message (peek semantics)
        msgs = message_manager.get_messages("agent-b")
        assert len(msgs) == 1

        # After ack, should be empty
        message_manager.ack_messages("agent-b", [msgs[0]["id"]])
        msgs = message_manager.get_messages("agent-b")
        assert len(msgs) == 0

    def test_empty_returns_empty_list(self, message_manager):
        """No pending messages should return empty list."""
        msgs = message_manager.get_messages("nonexistent")
        assert msgs == []

    def test_reply_has_reply_to_in_get_messages(self, message_manager):
        """reply should have reply_to field in get_messages results."""
        msg = message_manager.send_message("a", "b", "Q")
        message_manager.reply(msg["id"], "b", "A")

        msgs = message_manager.get_messages("a")
        assert len(msgs) == 1
        assert msgs[0]["reply_to"] == msg["id"]
        assert msgs[0]["from_agent"] == "b"
        assert msgs[0]["to_agent"] == "a"
        assert msgs[0]["message"] == "A"
        assert "type" not in msgs[0]  # No type field
        assert msgs[0]["from_agent"] == "b"
        assert msgs[0]["to_agent"] == "a"
        assert msgs[0]["message"] == "A"
        assert "type" not in msgs[0]  # No type field


class TestWaitForMessage:
    """Tests for the unified wait_for_message method."""

    def test_wakes_on_message(self, message_manager):
        """wait_for_message should return when a message is already queued."""
        message_manager.send_message("agent-a", "agent-b", "Hello!")

        result = message_manager.wait_for_message("agent-b", timeout=5)
        assert result is not None
        assert len(result) >= 1
        # Should be able to find the message without type field
        found = any(m.get("message") == "Hello!" for m in result)
        assert found

    def test_wakes_on_reply(self, message_manager):
        """wait_for_message should return when a reply is already queued."""
        msg = message_manager.send_message("agent-a", "agent-b", "Q")
        message_manager.reply(msg["id"], "agent-b", "A")

        result = message_manager.wait_for_message("agent-a", timeout=5)
        assert result is not None
        assert len(result) >= 1
        # Should be able to find the reply without type field
        found = any(m.get("message") == "A" for m in result)
        assert found

    def test_times_out(self, message_manager):
        """wait_for_message should return None on timeout."""
        start = time.time()
        result = message_manager.wait_for_message("agent-b", timeout=1)
        elapsed = time.time() - start

        assert result is None
        assert elapsed >= 1

    def test_returns_messages_directly(self, message_manager):
        """wait_for_message should return messages directly, not a notification."""
        message_manager.send_message("agent-a", "agent-b", "Direct")

        result = message_manager.wait_for_message("agent-b", timeout=5)
        assert isinstance(result, list)
        found = any(m.get("message") == "Direct" for m in result)
        assert found

    def test_impl_returns_received_dict(self, message_manager):
        """_wait_for_message_impl should wrap results in status dict."""
        message_manager.send_message("agent-a", "agent-b", "Test")

        result = _wait_for_message_impl(message_manager, "agent-b", timeout=5)
        assert result["status"] == "received"
        assert "messages" in result
        assert len(result["messages"]) >= 1

    def test_shutdown_returns_sentinel(self, message_manager):
        """wait_for_message should return 'shutdown' when shutdown_event is set."""
        import threading
        shutdown = threading.Event()
        shutdown.set()
        result = message_manager.wait_for_message(
            "agent-b", timeout=60, shutdown_event=shutdown,
        )
        assert result == "shutdown"

    def test_shutdown_not_set_returns_normally(self, message_manager):
        """wait_for_message with unset shutdown_event should behave normally."""
        import threading
        shutdown = threading.Event()

        message_manager.send_message("agent-a", "agent-b", "Hello!")
        result = message_manager.wait_for_message(
            "agent-b", timeout=5, shutdown_event=shutdown,
        )
        assert result is not None
        assert len(result) >= 1
        found = any(m.get("message") == "Hello!" for m in result)
        assert found


class TestAckMessages:
    """Tests for the ack_messages functionality."""

    def test_ack_removes_from_peek(self, message_manager):
        """Acked messages should not appear in peek_messages."""
        msg1 = message_manager.send_message("a", "b", "msg1")
        msg2 = message_manager.send_message("c", "b", "msg2")

        # Both visible before ack
        msgs = message_manager.get_messages("b")
        assert len(msgs) == 2

        # Ack first message
        message_manager.ack_messages("b", [msg1["id"]])

        # Only second message visible
        msgs = message_manager.get_messages("b")
        assert len(msgs) == 1
        assert msgs[0]["message"] == "msg2"

    def test_ack_reply(self, message_manager):
        """Acked replies should not appear in peek_messages."""
        msg = message_manager.send_message("a", "b", "Q")
        reply = message_manager.reply(msg["id"], "b", "A")

        # Reply visible
        msgs = message_manager.get_messages("a")
        assert len(msgs) == 1
        reply_id = msgs[0]["id"]  # Now just id, not reply_id

        # Ack reply
        message_manager.ack_messages("a", [reply_id])

        # Reply gone
        msgs = message_manager.get_messages("a")
        assert len(msgs) == 0

    def test_partial_ack(self, message_manager):
        """Acking some messages should leave others visible."""
        msg1 = message_manager.send_message("a", "b", "first")
        msg2 = message_manager.send_message("c", "b", "second")
        msg3 = message_manager.send_message("d", "b", "third")

        message_manager.ack_messages("b", [msg1["id"], msg3["id"]])

        msgs = message_manager.get_messages("b")
        assert len(msgs) == 1
        assert msgs[0]["message"] == "second"

    def test_ack_unknown_ids_safe(self, message_manager):
        """Acking unknown IDs should not cause errors."""
        result = message_manager.ack_messages("b", ["nonexistent-id-1", "fake-id-2"])
        assert result["acked"] == 2

        # Nothing to peek
        msgs = message_manager.get_messages("b")
        assert len(msgs) == 0

    def test_ack_empty_list(self, message_manager):
        """Acking empty list should be a no-op."""
        result = message_manager.ack_messages("b", [])
        assert result["acked"] == 0
        assert result["compacted"] is False

    def test_compaction_triggers(self, message_manager):
        """Compaction should trigger when acked set exceeds threshold."""
        # Send enough messages to trigger compaction
        msg_ids = []
        for i in range(25):
            msg = message_manager.send_message("a", "b", f"msg{i}")
            msg_ids.append(msg["id"])

        # Ack all 25 - should trigger compaction (threshold is 20)
        result = message_manager.ack_messages("b", msg_ids)
        assert result["compacted"] is True

        # After compaction, inbox list should be empty
        msgs = message_manager.get_messages("b")
        assert len(msgs) == 0

        # Acked set should also be cleared by compaction
        acked = message_manager._get_acked_ids("b")
        assert len(acked) == 0

    def test_compaction_preserves_unacked(self, message_manager):
        """Compaction should keep unacked messages in the list."""
        msg_ids = []
        for i in range(22):
            msg = message_manager.send_message("a", "b", f"msg{i}")
            msg_ids.append(msg["id"])

        # Ack 21, leaving one unacked
        kept_id = msg_ids[10]
        to_ack = [mid for mid in msg_ids if mid != kept_id]
        result = message_manager.ack_messages("b", to_ack)
        assert result["compacted"] is True

        # Only the unacked message should remain
        msgs = message_manager.get_messages("b")
        assert len(msgs) == 1
        assert msgs[0]["id"] == kept_id

    def test_ack_messages_impl_accepts_empty(self, message_manager):
        """_ack_messages_impl should accept empty message_ids and return early."""
        result = _ack_messages_impl(message_manager, "b", [])
        assert result["acked"] == 0
        assert result["compacted"] is False


class TestWaitForMessage10sLoopFix:
    """Tests that wait_for_message finds messages even without notifications."""

    def test_finds_messages_without_notification(self, message_manager, redis_client):
        """wait_for_message should find messages even if notification is lost."""
        # Send a message
        message_manager.send_message("agent-a", "agent-b", "important")

        # Delete the notification signal (simulating lost notification)
        notify_key = f"{message_manager.NOTIFY_PREFIX}agent-b"
        redis_client.delete(notify_key)

        # wait_for_message should still find it within one BLPOP cycle (~10s max)
        # Using a 15s timeout to give it room
        start = time.time()
        result = message_manager.wait_for_message("agent-b", timeout=15)
        elapsed = time.time() - start

        assert result is not None
        assert len(result) >= 1
        found = any(m.get("message") == "important" for m in result)
        assert found
        # Should find within ~10s (one BLPOP cycle), not at the 15s timeout
        assert elapsed < 12


class TestOfflineAgentQueueing:
    """Tests confirming messages are queued for offline agents until they reconnect."""

    @pytest.fixture(autouse=True)
    def _patch_rl(self, patch_rate_limiter):
        """Patch rate_limiter so these tests don't need a real Redis."""

    def _make_agent_offline(self, redis_client, agent_manager, agent_id):
        """Helper: set an agent's last_seen far enough in the past to appear offline."""
        old_time = (
            datetime.now(timezone.utc)
            - timedelta(seconds=agent_manager.AGENT_TIMEOUT_SECONDS + 60)
        ).isoformat()
        data = json.loads(redis_client.hget(agent_manager.AGENTS_KEY, agent_id))
        data["last_seen"] = old_time
        redis_client.hset(agent_manager.AGENTS_KEY, agent_id, json.dumps(data))

    def test_messages_queued_for_offline_agent(
        self, message_manager, agent_manager, redis_client
    ):
        """send_message should queue messages for offline (but registered) agents."""
        agent_manager.register_agent("machine/receiver")
        self._make_agent_offline(redis_client, agent_manager, "machine/receiver")

        # Confirm the agent is offline
        agent = agent_manager.get_agent("machine/receiver")
        assert agent["status"] == "offline"

        # Send a message — should succeed even though agent is offline
        result = message_manager.send_message(
            "machine/sender", "machine/receiver", "queued for you"
        )
        assert result["to_agent"] == "machine/receiver"

        # Message should be in the inbox
        msgs = message_manager.get_messages("machine/receiver")
        assert len(msgs) == 1
        assert msgs[0]["message"] == "queued for you"

    def test_multiple_messages_queued_while_offline(
        self, message_manager, agent_manager, redis_client
    ):
        """Multiple messages sent while agent is offline should all be queued."""
        agent_manager.register_agent("machine/sleeper")
        self._make_agent_offline(redis_client, agent_manager, "machine/sleeper")

        for i in range(3):
            message_manager.send_message(
                "machine/sender", "machine/sleeper", f"message {i}"
            )

        msgs = message_manager.get_messages("machine/sleeper")
        assert len(msgs) == 3
        assert [m["message"] for m in msgs] == ["message 0", "message 1", "message 2"]

    def test_offline_agent_receives_messages_on_reconnect(
        self, message_manager, agent_manager, redis_client
    ):
        """Queued messages should be retrievable after the agent comes back online."""
        agent_manager.register_agent("machine/returner")
        self._make_agent_offline(redis_client, agent_manager, "machine/returner")

        # Send messages while offline
        message_manager.send_message("machine/sender", "machine/returner", "while you were out")

        # Agent reconnects (re-registers updates last_seen)
        agent_manager.register_agent("machine/returner")
        agent = agent_manager.get_agent("machine/returner")
        assert agent["status"] == "online"

        # Messages should still be waiting
        msgs = message_manager.get_messages("machine/returner")
        assert len(msgs) == 1
        assert msgs[0]["message"] == "while you were out"

    def test_message_inbox_ttl_set_to_7_days(
        self, message_manager, redis_client
    ):
        """Inbox key should have a 7-day TTL set when a message is sent."""
        message_manager.send_message("machine/sender", "machine/receiver", "hello")

        inbox_key = f"{message_manager.INBOX_PREFIX}machine/receiver"
        ttl = redis_client.ttl(inbox_key)

        # TTL should be close to MESSAGE_TTL_SECONDS (604800s = 7 days). Allow some drift.
        assert ttl > 0, "inbox key should have a TTL set"
        assert ttl <= message_manager.MESSAGE_TTL_SECONDS
        assert ttl > message_manager.MESSAGE_TTL_SECONDS - 5  # within 5 seconds of 7 days

    def test_send_message_to_unregistered_agent_raises(
        self, message_manager, agent_manager
    ):
        """send_message should reject messages for agents that were never registered (no flag)."""
        with pytest.raises(ToolError) as exc_info:
            _send_message_impl(
                message_manager,
                agent_manager,
                from_agent="machine/sender",
                to="machine/ghost",
                message="nobody home",
            )
        assert "deliver_offline" in str(exc_info.value)


class TestMessageArchive:
    """Tests for message archive (c3po:msg:{id}) written at send/reply time."""

    def test_send_creates_archive_entry(self, message_manager):
        """send_message should write an archive entry at c3po:msg:{id}."""
        result = message_manager.send_message("machine/a", "machine/b", "hello")
        message_id = result["id"]
        raw = message_manager.redis.get(f"c3po:msg:{message_id}")
        assert raw is not None
        archived = json.loads(raw)
        assert archived["id"] == message_id
        assert archived["from_agent"] == "machine/a"
        assert archived["to_agent"] == "machine/b"
        assert archived["message"] == "hello"

    def test_reply_creates_archive_entry(self, message_manager):
        """reply() should write an archive entry for the reply."""
        msg = message_manager.send_message("machine/a", "machine/b", "hello")
        reply = message_manager.reply(msg["id"], "machine/b", "world")
        reply_id = reply["id"]
        raw = message_manager.redis.get(f"c3po:msg:{reply_id}")
        assert raw is not None
        archived = json.loads(raw)
        assert archived["id"] == reply_id
        assert archived["reply_to"] == msg["id"]
        assert archived["from_agent"] == "machine/b"
        assert archived["to_agent"] == "machine/a"

    def test_archive_has_7d_ttl(self, message_manager):
        """Archive entries should have 7-day TTL."""
        result = message_manager.send_message("machine/a", "machine/b", "hello")
        message_id = result["id"]
        ttl = message_manager.redis.ttl(f"c3po:msg:{message_id}")
        expected = 7 * 24 * 60 * 60  # 604800
        assert ttl > expected - 10  # within 10 seconds of 7 days
        assert ttl <= expected

    def test_get_archived_message_found(self, message_manager):
        """get_archived_message should return the message dict."""
        result = message_manager.send_message("machine/a", "machine/b", "hi")
        archived = message_manager.get_archived_message(result["id"])
        assert archived is not None
        assert archived["id"] == result["id"]
        assert archived["message"] == "hi"

    def test_get_archived_message_missing(self, message_manager):
        """get_archived_message returns None for unknown IDs."""
        result = message_manager.get_archived_message("machine/a::machine/b::00000000")
        assert result is None

    def test_get_thread_linear_chain(self, message_manager):
        """get_thread should return messages in chronological order."""
        # A→B
        msg1 = message_manager.send_message("machine/a", "machine/b", "msg1")
        # B replies to A
        rep1 = message_manager.reply(msg1["id"], "machine/b", "rep1")
        # A replies to B's reply
        rep2 = message_manager.reply(rep1["id"], "machine/a", "rep2")

        thread = message_manager.get_thread(rep2["id"])
        assert len(thread) == 3
        assert thread[0]["id"] == msg1["id"]
        assert thread[1]["id"] == rep1["id"]
        assert thread[2]["id"] == rep2["id"]

    def test_get_thread_single_root(self, message_manager):
        """get_thread on a root message (no reply_to) returns just that message."""
        msg = message_manager.send_message("machine/a", "machine/b", "solo")
        thread = message_manager.get_thread(msg["id"])
        assert len(thread) == 1
        assert thread[0]["id"] == msg["id"]

    def test_get_thread_partial_expired(self, message_manager):
        """get_thread stops at expired archive entries, returns partial thread."""
        msg1 = message_manager.send_message("machine/a", "machine/b", "root")
        rep1 = message_manager.reply(msg1["id"], "machine/b", "reply")

        # Delete the root archive entry to simulate expiry
        message_manager.redis.delete(f"c3po:msg:{msg1['id']}")

        thread = message_manager.get_thread(rep1["id"])
        # Only rep1 should be returned (root expired)
        assert len(thread) == 1
        assert thread[0]["id"] == rep1["id"]

    def test_get_thread_max_depth(self, message_manager):
        """get_thread caps at max_depth messages."""
        # Build a chain of 10 messages
        msg = message_manager.send_message("machine/a", "machine/b", "start")
        last = msg
        for i in range(9):
            # Alternate who replies
            sender = "machine/b" if i % 2 == 0 else "machine/a"
            last = message_manager.reply(last["id"], sender, f"msg{i}")

        thread = message_manager.get_thread(last["id"], max_depth=5)
        assert len(thread) == 5

    def test_get_thread_cycle_protection(self, message_manager):
        """get_thread should not loop infinitely if a cycle exists in reply_to."""
        # Craft a fake cycle by directly writing to Redis
        id_a = "machine/a::machine/b::aaaaaaaa"
        id_b = "machine/b::machine/a::bbbbbbbb"
        msg_a = {"id": id_a, "from_agent": "machine/a", "to_agent": "machine/b",
                 "message": "a", "reply_to": id_b, "timestamp": "2026-01-01T00:00:00+00:00"}
        msg_b = {"id": id_b, "from_agent": "machine/b", "to_agent": "machine/a",
                 "message": "b", "reply_to": id_a, "timestamp": "2026-01-01T00:00:01+00:00"}
        message_manager.redis.set(f"c3po:msg:{id_a}", json.dumps(msg_a))
        message_manager.redis.set(f"c3po:msg:{id_b}", json.dumps(msg_b))

        # Should terminate without infinite loop
        thread = message_manager.get_thread(id_a)
        assert len(thread) <= 2


class TestGetMessageTool:
    """Tests for _get_message_impl authorization and lookup."""

    def test_sender_can_retrieve(self, message_manager):
        """Sender (from_agent) should be able to retrieve their own message."""
        msg = message_manager.send_message("machine/a", "machine/b", "hello")
        result = _get_message_impl(message_manager, "machine/a", msg["id"])
        assert result["id"] == msg["id"]

    def test_recipient_can_retrieve(self, message_manager):
        """Recipient (to_agent) should be able to retrieve a message."""
        msg = message_manager.send_message("machine/a", "machine/b", "hello")
        result = _get_message_impl(message_manager, "machine/b", msg["id"])
        assert result["id"] == msg["id"]

    def test_third_party_rejected(self, message_manager):
        """Third parties should get not-found error (not unauthorized) to avoid leaking existence."""
        msg = message_manager.send_message("machine/a", "machine/b", "hello")
        with pytest.raises(ToolError) as exc_info:
            _get_message_impl(message_manager, "machine/c", msg["id"])
        assert "not found" in str(exc_info.value).lower()

    def test_not_found_raises(self, message_manager):
        """Non-existent message_id raises ToolError."""
        with pytest.raises(ToolError) as exc_info:
            _get_message_impl(message_manager, "machine/a", "machine/a::machine/b::00000000")
        assert "not found" in str(exc_info.value).lower()


class TestGetThreadTool:
    """Tests for _get_thread_impl authorization and response shape."""

    def test_participant_can_retrieve_thread(self, message_manager):
        """A participant in any message can retrieve the thread."""
        msg = message_manager.send_message("machine/a", "machine/b", "hi")
        rep = message_manager.reply(msg["id"], "machine/b", "yo")

        result = _get_thread_impl(message_manager, "machine/a", rep["id"])
        assert result["count"] == 2
        assert result["root_message_id"] == msg["id"]
        assert result["latest_message_id"] == rep["id"]
        assert len(result["thread"]) == 2

    def test_non_participant_rejected(self, message_manager):
        """Non-participant should get not-found error."""
        msg = message_manager.send_message("machine/a", "machine/b", "hi")
        with pytest.raises(ToolError) as exc_info:
            _get_thread_impl(message_manager, "machine/c", msg["id"])
        assert "not found" in str(exc_info.value).lower()

    def test_response_shape(self, message_manager):
        """Response should include thread, count, root_message_id, latest_message_id."""
        msg = message_manager.send_message("machine/a", "machine/b", "first")
        result = _get_thread_impl(message_manager, "machine/a", msg["id"])
        assert "thread" in result
        assert "count" in result
        assert "root_message_id" in result
        assert "latest_message_id" in result
        assert result["count"] == 1
        assert result["root_message_id"] == msg["id"]
        assert result["latest_message_id"] == msg["id"]


class TestNotifyTTL:
    """Tests for TTL behavior: notify keys use 24h, inbox keys use 7d."""

    def test_send_inbox_uses_7d_ttl(self, message_manager):
        """Inbox key TTL should be 7 days after send_message."""
        message_manager.send_message("machine/a", "machine/b", "hello")
        inbox_key = f"c3po:inbox:machine/b"
        ttl = message_manager.redis.ttl(inbox_key)
        expected = 7 * 24 * 60 * 60
        assert ttl > expected - 10

    def test_send_notify_uses_24h_ttl(self, message_manager):
        """Notify key TTL should be 24 hours after send_message."""
        message_manager.send_message("machine/a", "machine/b", "hello")
        notify_key = f"c3po:notify:machine/b"
        ttl = message_manager.redis.ttl(notify_key)
        expected_24h = 24 * 60 * 60
        expected_7d = 7 * 24 * 60 * 60
        assert ttl > expected_24h - 10
        assert ttl <= expected_7d  # Must be ≤ 7 days (not equal to 7d TTL)

    def test_reply_notify_uses_24h_ttl(self, message_manager):
        """Notify key TTL should be 24 hours after reply()."""
        msg = message_manager.send_message("machine/a", "machine/b", "hello")
        message_manager.reply(msg["id"], "machine/b", "world")
        notify_key = f"c3po:notify:machine/a"
        ttl = message_manager.redis.ttl(notify_key)
        expected_24h = 24 * 60 * 60
        expected_7d = 7 * 24 * 60 * 60
        assert ttl > expected_24h - 10
        assert ttl <= expected_7d
