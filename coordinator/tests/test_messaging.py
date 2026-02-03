"""Tests for C3PO messaging functionality."""

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


class TestGetPendingMessagesTool:
    """Tests for the get_messages tool implementation (message filtering)."""

    def test_get_messages_returns_messages(self, message_manager):
        """get_messages with type=message should return pending messages."""
        message_manager.send_message("a", "b", "message 1")
        message_manager.send_message("c", "b", "message 2")

        result = _get_messages_impl(message_manager, "b", message_type="message")

        assert len(result) == 2
        assert result[0]["message"] == "message 1"
        assert result[1]["message"] == "message 2"
        assert all(m["type"] == "message" for m in result)

    def test_get_messages_is_non_destructive(self, message_manager):
        """get_messages should NOT consume messages (peek semantics)."""
        message_manager.send_message("a", "b", "still here")

        # First call gets the message
        result = _get_messages_impl(message_manager, "b")
        assert len(result) == 1

        # Second call should return same message (not consumed)
        result = _get_messages_impl(message_manager, "b")
        assert len(result) == 1
        assert result[0]["message"] == "still here"


class TestReplyHandling:
    """Tests for reply and wait_for_response."""

    def test_reply_creates_properly_formatted_response(
        self, message_manager
    ):
        """reply should create a properly formatted response."""
        # First send a message to get a valid message_id
        msg = message_manager.send_message("agent-a", "agent-b", "Help?")
        message_id = msg["id"]

        # Now reply
        result = message_manager.reply(
            message_id=message_id,
            from_agent="agent-b",
            response="Here's your answer",
            status="success",
        )

        assert result["message_id"] == message_id
        assert result["from_agent"] == "agent-b"
        assert result["to_agent"] == "agent-a"  # Original sender
        assert result["response"] == "Here's your answer"
        assert result["status"] == "success"
        assert "timestamp" in result

    def test_respond_to_request_backwards_compat(self, message_manager):
        """respond_to_request backwards compat wrapper should work."""
        msg = message_manager.send_message("agent-a", "agent-b", "Help?")
        result = message_manager.respond_to_request(
            request_id=msg["id"],
            from_agent="agent-b",
            response="Answer",
        )
        assert result["message_id"] == msg["id"]
        assert result["response"] == "Answer"

    def test_wait_for_response_returns_when_response_arrives(self, message_manager):
        """wait_for_response should return when a response arrives."""
        # Send a message
        msg = message_manager.send_message("agent-a", "agent-b", "Question?")
        message_id = msg["id"]

        # Send the reply immediately (simulating agent-b responding)
        message_manager.reply(
            message_id=message_id,
            from_agent="agent-b",
            response="Answer!",
        )

        # Now wait should return immediately
        result = message_manager.wait_for_response("agent-a", message_id, timeout=5)

        assert result is not None
        assert result["message_id"] == message_id
        assert result["response"] == "Answer!"

    def test_wait_for_response_times_out_correctly(self, message_manager):
        """wait_for_response should return None on timeout."""
        # Send a message but don't respond
        msg = message_manager.send_message("agent-a", "agent-b", "Question?")
        message_id = msg["id"]

        # Wait with a short timeout
        start = time.time()
        result = message_manager.wait_for_response("agent-a", message_id, timeout=1)
        elapsed = time.time() - start

        assert result is None
        assert elapsed >= 1  # Should have waited at least 1 second

    def test_full_message_reply_cycle(self, message_manager, agent_manager):
        """Integration test: full send -> receive -> ack -> reply -> wait cycle."""
        # Register both agents
        agent_manager.register_agent("agent-a")
        agent_manager.register_agent("agent-b")

        # Agent A sends message to Agent B
        msg = _send_message_impl(
            message_manager,
            agent_manager,
            from_agent="agent-a",
            to="agent-b",
            message="What is 2+2?",
        )
        message_id = msg["id"]

        # Agent B retrieves the message via get_messages
        pending = _get_messages_impl(message_manager, "agent-b", message_type="message")
        assert len(pending) == 1
        assert pending[0]["message"] == "What is 2+2?"
        assert pending[0]["id"] == message_id

        # Agent B acks the message
        _ack_messages_impl(message_manager, "agent-b", [message_id])

        # Agent B replies
        response = _reply_impl(
            message_manager,
            from_agent="agent-b",
            message_id=message_id,
            response="4",
        )
        assert response["to_agent"] == "agent-a"

        # Agent A gets the reply via get_messages
        result = _get_messages_impl(message_manager, "agent-a", message_type="reply")
        assert len(result) == 1
        assert result[0]["response"] == "4"
        assert result[0]["status"] == "success"

    def test_wait_for_message_returns_timeout_dict(self, message_manager):
        """_wait_for_message_impl should return timeout dict on timeout."""
        result = _wait_for_message_impl(
            message_manager,
            agent_id="agent-a",
            timeout=1,
        )

        assert result["status"] == "timeout"
        assert "No messages received" in result["message"]

    def test_parse_message_id(self, message_manager):
        """_parse_message_id should correctly extract sender and receiver."""
        # Test with simple agent IDs
        sender, receiver = message_manager._parse_message_id("alice::bob::a1b2c3d4")
        assert sender == "alice"
        assert receiver == "bob"

        # Test with hyphenated agent IDs
        sender, receiver = message_manager._parse_message_id(
            "agent-a::agent-b::12345678"
        )
        assert sender == "agent-a"
        assert receiver == "agent-b"

    def test_parse_request_id_backwards_compat(self, message_manager):
        """_parse_request_id alias should still work."""
        sender, receiver = message_manager._parse_request_id("alice::bob::a1b2c3d4")
        assert sender == "alice"
        assert receiver == "bob"

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


class TestWaitForRequest:
    """Tests for wait_for_request notification behavior."""

    def test_wait_for_request_returns_when_message_arrives(self, message_manager):
        """wait_for_request should return ready notification when a message arrives."""
        # Send a message first
        message_manager.send_message("agent-a", "agent-b", "Hello!")

        # Now wait should return immediately with a notification
        result = message_manager.wait_for_request("agent-b", timeout=5)

        assert result is not None
        assert result["status"] == "ready"
        assert result["pending"] >= 1

    def test_wait_for_request_times_out_correctly(self, message_manager):
        """wait_for_request should return None on timeout."""
        # Don't send any message
        start = time.time()
        result = message_manager.wait_for_request("agent-b", timeout=1)
        elapsed = time.time() - start

        assert result is None
        assert elapsed >= 1  # Should have waited at least 1 second

    def test_wait_for_request_multiple_queued_return_in_order(self, message_manager):
        """Multiple queued messages: wait_for_request notifies, get_pending_messages consumes in FIFO."""
        # Queue multiple messages
        message_manager.send_message("agent-a", "agent-b", "first")
        message_manager.send_message("agent-c", "agent-b", "second")
        message_manager.send_message("agent-d", "agent-b", "third")

        # Wait should return a notification with pending >= 1
        result = message_manager.wait_for_request("agent-b", timeout=1)
        assert result is not None
        assert result["status"] == "ready"
        assert result["pending"] >= 1

        # Now consume them via get_pending_messages and verify FIFO order
        pending = message_manager.get_pending_messages("agent-b")
        assert len(pending) == 3
        assert pending[0]["message"] == "first"
        assert pending[1]["message"] == "second"
        assert pending[2]["message"] == "third"

    def test_wait_for_message_message_type_returns_timeout_dict(self, message_manager):
        """_wait_for_message_impl with type=message should return timeout dict on timeout."""
        result = _wait_for_message_impl(
            message_manager,
            agent_id="agent-b",
            timeout=1,
            message_type="message",
        )

        assert result["status"] == "timeout"
        assert "No messages received" in result["message"]

    def test_wait_for_request_does_not_consume_message(self, message_manager):
        """wait_for_request should NOT consume the message (it stays in inbox)."""
        message_manager.send_message("agent-a", "agent-b", "still here")

        # Wait gets the notification
        result = message_manager.wait_for_request("agent-b", timeout=1)
        assert result is not None
        assert result["status"] == "ready"

        # Inbox should still have the message
        pending = message_manager.get_pending_messages("agent-b")
        assert len(pending) == 1
        assert pending[0]["message"] == "still here"

    def test_send_message_pushes_notification(self, message_manager, redis_client):
        """send_message should push a notification signal to the notify key."""
        message_manager.send_message("agent-a", "agent-b", "hello")

        notify_key = f"{message_manager.NOTIFY_PREFIX}agent-b"
        length = redis_client.llen(notify_key)
        assert length == 1

    def test_wait_for_request_notification_loss_does_not_lose_message(
        self, message_manager, redis_client
    ):
        """If the notification signal is lost, the message should still be in the inbox."""
        message_manager.send_message("agent-a", "agent-b", "important")

        # Manually consume the notification signal (simulating connection drop)
        notify_key = f"{message_manager.NOTIFY_PREFIX}agent-b"
        redis_client.lpop(notify_key)

        # Notification is gone, but message should still be in inbox
        pending = message_manager.get_pending_messages("agent-b")
        assert len(pending) == 1
        assert pending[0]["message"] == "important"


class TestReplyPutBack:
    """Tests for reply put-back mechanism when message_id doesn't match."""

    def test_mismatched_reply_is_put_back(self, message_manager):
        """When wait_for_response gets wrong message_id, it should put it back."""
        # Send two messages from agent-a to agent-b
        msg1 = message_manager.send_message("agent-a", "agent-b", "Question 1")
        msg2 = message_manager.send_message("agent-a", "agent-b", "Question 2")
        msg1_id = msg1["id"]
        msg2_id = msg2["id"]

        # Agent-b replies to msg2 FIRST, then msg1
        message_manager.reply(
            message_id=msg2_id,
            from_agent="agent-b",
            response="Answer 2",
        )
        message_manager.reply(
            message_id=msg1_id,
            from_agent="agent-b",
            response="Answer 1",
        )

        # Agent-a waits for msg1 first - should find it even though msg2's
        # reply is in front of the queue
        result1 = message_manager.wait_for_response("agent-a", msg1_id, timeout=5)
        assert result1 is not None
        assert result1["response"] == "Answer 1"
        assert result1["message_id"] == msg1_id

        # Now wait for msg2 - the put-back reply should be available
        result2 = message_manager.wait_for_response("agent-a", msg2_id, timeout=5)
        assert result2 is not None
        assert result2["response"] == "Answer 2"
        assert result2["message_id"] == msg2_id

    def test_put_back_maintains_fifo_for_subsequent_waiters(self, message_manager):
        """Put-back replies should maintain FIFO order using rpush."""
        # Send 3 messages from agent-a to agent-b
        msg1 = message_manager.send_message("agent-a", "agent-b", "Q1")
        msg2 = message_manager.send_message("agent-a", "agent-b", "Q2")
        msg3 = message_manager.send_message("agent-a", "agent-b", "Q3")

        # Reply in order: 3, 2, 1 (reverse order)
        message_manager.reply(msg3["id"], "agent-b", "A3")
        message_manager.reply(msg2["id"], "agent-b", "A2")
        message_manager.reply(msg1["id"], "agent-b", "A1")

        # Wait for msg1 - will consume and put back 3 and 2
        result1 = message_manager.wait_for_response("agent-a", msg1["id"], timeout=5)
        assert result1["response"] == "A1"

        # Wait for msg2 - should find it (was put back)
        result2 = message_manager.wait_for_response("agent-a", msg2["id"], timeout=5)
        assert result2["response"] == "A2"

        # Wait for msg3 - should find it (was put back)
        result3 = message_manager.wait_for_response("agent-a", msg3["id"], timeout=5)
        assert result3["response"] == "A3"

    def test_put_back_uses_rpush_not_lpush(self, message_manager, redis_client):
        """Verify that put-back uses rpush (FIFO) not lpush (LIFO)."""
        # Send 2 messages
        msg1 = message_manager.send_message("agent-a", "agent-b", "Q1")
        msg2 = message_manager.send_message("agent-a", "agent-b", "Q2")

        # Reply to both (msg2 first)
        message_manager.reply(msg2["id"], "agent-b", "A2")
        message_manager.reply(msg1["id"], "agent-b", "A1")

        # Wait for msg1
        result1 = message_manager.wait_for_response("agent-a", msg1["id"], timeout=5)
        assert result1["response"] == "A1"

        # Verify A2 is still available
        result2 = message_manager.wait_for_response("agent-a", msg2["id"], timeout=5)
        assert result2["response"] == "A2"

    def test_multiple_agents_concurrent_responses(self, message_manager):
        """Multiple agents waiting for responses should not interfere."""
        # Agent A sends to Agent C, Agent B sends to Agent C
        msg_a = message_manager.send_message("agent-a", "agent-c", "From A")
        msg_b = message_manager.send_message("agent-b", "agent-c", "From B")

        # Agent C replies to both (B first, then A)
        message_manager.reply(msg_b["id"], "agent-c", "To B")
        message_manager.reply(msg_a["id"], "agent-c", "To A")

        # Agent A should get their reply
        result_a = message_manager.wait_for_response(
            "agent-a", msg_a["id"], timeout=5
        )
        assert result_a["response"] == "To A"

        # Agent B should get their reply
        result_b = message_manager.wait_for_response(
            "agent-b", msg_b["id"], timeout=5
        )
        assert result_b["response"] == "To B"

    def test_threaded_waiters_get_correct_responses(self, message_manager):
        """Multiple threads waiting should each get their correct response."""
        import threading

        # Send 3 messages
        msg1 = message_manager.send_message("agent-a", "agent-b", "Q1")
        msg2 = message_manager.send_message("agent-a", "agent-b", "Q2")
        msg3 = message_manager.send_message("agent-a", "agent-b", "Q3")

        results = {}
        errors = []

        def wait_for(message_id, expected_response, key):
            try:
                result = message_manager.wait_for_response(
                    "agent-a", message_id, timeout=10
                )
                if result is None:
                    errors.append(f"{key}: Got None (timeout)")
                elif result["response"] != expected_response:
                    errors.append(
                        f"{key}: Expected '{expected_response}', got '{result['response']}'"
                    )
                else:
                    results[key] = result
            except Exception as e:
                errors.append(f"{key}: Exception: {e}")

        # Start 3 threads waiting for responses
        t1 = threading.Thread(target=wait_for, args=(msg1["id"], "A1", "r1"))
        t2 = threading.Thread(target=wait_for, args=(msg2["id"], "A2", "r2"))
        t3 = threading.Thread(target=wait_for, args=(msg3["id"], "A3", "r3"))

        t1.start()
        t2.start()
        t3.start()

        # Small delay to ensure threads are waiting
        time.sleep(0.1)

        # Reply in mixed order
        message_manager.reply(msg2["id"], "agent-b", "A2")
        message_manager.reply(msg3["id"], "agent-b", "A3")
        message_manager.reply(msg1["id"], "agent-b", "A1")

        t1.join(timeout=15)
        t2.join(timeout=15)
        t3.join(timeout=15)

        # All threads should have completed successfully
        assert len(errors) == 0, f"Errors: {errors}"
        assert len(results) == 3, f"Missing results: {set(['r1', 'r2', 'r3']) - set(results.keys())}"


class TestGetPendingReplies:
    """Tests for _get_pending_replies private method."""

    def test_drains_response_queue(self, message_manager):
        """_get_pending_replies should consume all replies."""
        # Send and reply to two messages
        msg1 = message_manager.send_message("agent-a", "agent-b", "Q1")
        msg2 = message_manager.send_message("agent-a", "agent-b", "Q2")
        message_manager.reply(msg1["id"], "agent-b", "A1")
        message_manager.reply(msg2["id"], "agent-b", "A2")

        replies = message_manager._get_pending_replies("agent-a")
        assert len(replies) == 2
        assert replies[0]["response"] == "A1"
        assert replies[1]["response"] == "A2"

        # Second call should be empty (consumed)
        replies = message_manager._get_pending_replies("agent-a")
        assert len(replies) == 0

    def test_backwards_compat_alias(self, message_manager):
        """_get_pending_responses alias should work."""
        msg = message_manager.send_message("agent-a", "agent-b", "Q")
        message_manager.reply(msg["id"], "agent-b", "A")
        replies = message_manager._get_pending_responses("agent-a")
        assert len(replies) == 1

    def test_empty_returns_empty_list(self, message_manager):
        """Empty response queue should return empty list."""
        replies = message_manager._get_pending_replies("nonexistent")
        assert replies == []


class TestGetMessages:
    """Tests for the unified get_messages method."""

    def test_returns_both_messages_and_replies(self, message_manager):
        """get_messages with no filter should return both types."""
        # Create a message to agent-b and a reply to agent-a
        msg = message_manager.send_message("agent-a", "agent-b", "Question?")
        message_manager.reply(msg["id"], "agent-b", "Answer!")

        # Agent-a should see the reply
        msgs_a = message_manager.get_messages("agent-a")
        assert len(msgs_a) == 1
        assert msgs_a[0]["type"] == "reply"
        assert msgs_a[0]["response"] == "Answer!"

        # Agent-b should see the message
        msgs_b = message_manager.get_messages("agent-b")
        assert len(msgs_b) == 1
        assert msgs_b[0]["type"] == "message"
        assert msgs_b[0]["message"] == "Question?"

    def test_filter_message_only(self, message_manager):
        """get_messages with type=message should only return messages."""
        msg = message_manager.send_message("agent-a", "agent-b", "Q")
        message_manager.reply(msg["id"], "agent-b", "A")

        # Agent-a: filter for messages only (should be empty, message went to b)
        msgs = message_manager.get_messages("agent-a", message_type="message")
        assert len(msgs) == 0

        # Reply should still be there (not consumed by message filter)
        msgs = message_manager.get_messages("agent-a", message_type="reply")
        assert len(msgs) == 1

    def test_filter_reply_only(self, message_manager):
        """get_messages with type=reply should only return replies."""
        message_manager.send_message("agent-a", "agent-b", "Q")

        # Agent-b: filter for replies only (should be empty)
        msgs = message_manager.get_messages("agent-b", message_type="reply")
        assert len(msgs) == 0

        # Message should still be there (not consumed by reply filter)
        msgs = message_manager.get_messages("agent-b", message_type="message")
        assert len(msgs) == 1

    def test_legacy_type_values(self, message_manager):
        """get_messages should accept legacy 'request' and 'response' type values."""
        msg = message_manager.send_message("agent-a", "agent-b", "Q")
        message_manager.reply(msg["id"], "agent-b", "A")

        # Legacy "request" should work like "message"
        msgs_b = message_manager.get_messages("agent-b", message_type="request")
        assert len(msgs_b) == 1
        assert msgs_b[0]["type"] == "message"

        # Legacy "response" should work like "reply"
        msgs_a = message_manager.get_messages("agent-a", message_type="response")
        assert len(msgs_a) == 1
        assert msgs_a[0]["type"] == "reply"

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


class TestReplyNotifiesSender:
    """Tests that reply pushes a notification to the sender."""

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


class TestWaitForMessage:
    """Tests for the unified wait_for_message method."""

    def test_wakes_on_message(self, message_manager):
        """wait_for_message should return when a message is already queued."""
        message_manager.send_message("agent-a", "agent-b", "Hello!")

        result = message_manager.wait_for_message("agent-b", timeout=5)
        assert result is not None
        assert len(result) >= 1
        assert result[0]["type"] == "message"
        assert result[0]["message"] == "Hello!"

    def test_wakes_on_reply(self, message_manager):
        """wait_for_message should return when a reply is already queued."""
        msg = message_manager.send_message("agent-a", "agent-b", "Q")
        message_manager.reply(msg["id"], "agent-b", "A")

        result = message_manager.wait_for_message("agent-a", timeout=5)
        assert result is not None
        assert len(result) >= 1
        assert result[0]["type"] == "reply"
        assert result[0]["response"] == "A"

    def test_times_out(self, message_manager):
        """wait_for_message should return None on timeout."""
        start = time.time()
        result = message_manager.wait_for_message("agent-b", timeout=1)
        elapsed = time.time() - start

        assert result is None
        assert elapsed >= 1

    def test_filter_message_type(self, message_manager):
        """wait_for_message with type=message should only return messages."""
        message_manager.send_message("agent-a", "agent-b", "Hello!")

        result = message_manager.wait_for_message("agent-b", timeout=5, message_type="message")
        assert result is not None
        assert all(m["type"] == "message" for m in result)

    def test_filter_reply_type(self, message_manager):
        """wait_for_message with type=reply should only return replies."""
        msg = message_manager.send_message("agent-a", "agent-b", "Q")
        message_manager.reply(msg["id"], "agent-b", "A")

        result = message_manager.wait_for_message("agent-a", timeout=5, message_type="reply")
        assert result is not None
        assert all(m["type"] == "reply" for m in result)

    def test_returns_messages_directly(self, message_manager):
        """wait_for_message should return messages directly, not a notification."""
        message_manager.send_message("agent-a", "agent-b", "Direct")

        result = message_manager.wait_for_message("agent-b", timeout=5)
        assert isinstance(result, list)
        assert result[0]["message"] == "Direct"

    def test_impl_returns_received_dict(self, message_manager):
        """_wait_for_message_impl should wrap results in status dict."""
        message_manager.send_message("agent-a", "agent-b", "Test")

        result = _wait_for_message_impl(message_manager, "agent-b", timeout=5)
        assert result["status"] == "received"
        assert "messages" in result
        assert len(result["messages"]) >= 1


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
        msgs = message_manager.get_messages("a", message_type="reply")
        assert len(msgs) == 1
        reply_id = msgs[0]["reply_id"]

        # Ack reply
        message_manager.ack_messages("a", [reply_id])

        # Reply gone
        msgs = message_manager.get_messages("a", message_type="reply")
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


class TestPeekPendingReplies:
    """Tests for peek_pending_replies method."""

    def test_non_destructive(self, message_manager):
        """peek_pending_replies should not consume replies."""
        msg = message_manager.send_message("a", "b", "Q")
        message_manager.reply(msg["id"], "b", "A")

        # Peek twice
        replies = message_manager.peek_pending_replies("a")
        assert len(replies) == 1

        replies = message_manager.peek_pending_replies("a")
        assert len(replies) == 1

    def test_empty_returns_empty_list(self, message_manager):
        """Empty response queue should return empty list."""
        replies = message_manager.peek_pending_replies("nonexistent")
        assert replies == []


class TestReplyId:
    """Tests for reply_id field on replies."""

    def test_reply_has_reply_id(self, message_manager):
        """reply() should include a unique reply_id field."""
        msg = message_manager.send_message("a", "b", "Q")
        result = message_manager.reply(msg["id"], "b", "A")

        assert "reply_id" in result
        assert result["reply_id"].startswith("reply::")

    def test_reply_ids_are_unique(self, message_manager):
        """Different replies should have different reply_ids."""
        msg1 = message_manager.send_message("a", "b", "Q1")
        msg2 = message_manager.send_message("a", "b", "Q2")

        reply1 = message_manager.reply(msg1["id"], "b", "A1")
        reply2 = message_manager.reply(msg2["id"], "b", "A2")

        assert reply1["reply_id"] != reply2["reply_id"]

    def test_reply_id_in_get_messages(self, message_manager):
        """reply_id should appear in get_messages results."""
        msg = message_manager.send_message("a", "b", "Q")
        message_manager.reply(msg["id"], "b", "A")

        msgs = message_manager.get_messages("a", message_type="reply")
        assert len(msgs) == 1
        assert "reply_id" in msgs[0]
        assert msgs[0]["reply_id"].startswith("reply::")


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
        assert result[0]["message"] == "important"
        # Should find within ~10s (one BLPOP cycle), not at the 15s timeout
        assert elapsed < 12
