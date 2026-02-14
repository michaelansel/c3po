"""Tests for C3PO messaging functionality with single queue architecture."""

import pytest
import fakeredis
import threading
import time

from coordinator.messaging import MessageManager
from coordinator.agents import AgentManager
from coordinator.server import (
    _send_message_impl,
    _get_messages_impl,
    _reply_impl,
    _wait_for_message_impl,
    _ack_messages_impl,
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
        """send_message to unknown agent should return helpful error."""
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
        assert result["response"] == "Here's your answer"
        assert result["status"] == "success"
        assert "timestamp" in result

    def test_reply_creates_reply_id(self, message_manager):
        """reply should create a unique reply_id."""
        msg = message_manager.send_message("a", "b", "Q")
        result = message_manager.reply(msg["id"], "b", "A")

        assert "reply_id" in result
        assert result["reply_id"].startswith("reply::")

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

        # agent-a should have a notification
        assert redis_client.llen(f"{message_manager.NOTIFY_PREFIX}agent-a") == 1
        # agent-b should NOT have a notification from reply
        assert redis_client.llen(f"{message_manager.NOTIFY_PREFIX}agent-b") == 0

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
        assert result["response"] == "Failed to do that"


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
        assert msgs_a[0]["response"] == "Answer!"
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

    def test_reply_id_in_get_messages(self, message_manager):
        """reply_id should appear in get_messages results."""
        msg = message_manager.send_message("a", "b", "Q")
        message_manager.reply(msg["id"], "b", "A")

        msgs = message_manager.get_messages("a")
        assert len(msgs) == 1
        assert "reply_id" in msgs[0]
        assert msgs[0]["reply_id"].startswith("reply::")


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
        found = any(m.get("response") == "A" for m in result)
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
        reply_id = msgs[0]["reply_id"]

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

    def test_ack_messages_impl_rejects_empty(self, message_manager):
        """_ack_messages_impl should reject empty message_ids."""
        with pytest.raises(ToolError):
            _ack_messages_impl(message_manager, "b", [])


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
