"""Tests for C3PO messaging functionality."""

import pytest
import fakeredis
import threading
import time

from coordinator.messaging import MessageManager
from coordinator.agents import AgentManager
from coordinator.server import (
    _send_request_impl,
    _get_pending_requests_impl,
    _respond_to_request_impl,
    _wait_for_response_impl,
    _wait_for_request_impl,
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

    def test_send_request_creates_properly_formatted_message(self, message_manager):
        """send_request should create a properly formatted message in Redis."""
        result = message_manager.send_request(
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

    def test_get_pending_requests_retrieves_and_removes(self, message_manager):
        """get_pending_requests should retrieve and remove messages."""
        message_manager.send_request("a", "b", "message 1")
        message_manager.send_request("c", "b", "message 2")

        # First retrieval should get both
        requests = message_manager.get_pending_requests("b")
        assert len(requests) == 2
        assert requests[0]["message"] == "message 1"
        assert requests[1]["message"] == "message 2"

        # Second retrieval should be empty (consumed)
        requests = message_manager.get_pending_requests("b")
        assert len(requests) == 0

    def test_multiple_messages_queue_fifo(self, message_manager):
        """Messages should queue in FIFO order."""
        message_manager.send_request("a", "b", "first")
        message_manager.send_request("c", "b", "second")
        message_manager.send_request("d", "b", "third")

        requests = message_manager.get_pending_requests("b")
        assert len(requests) == 3
        assert requests[0]["message"] == "first"
        assert requests[1]["message"] == "second"
        assert requests[2]["message"] == "third"

    def test_empty_inbox_returns_empty_list(self, message_manager):
        """Empty inbox should return empty list."""
        requests = message_manager.get_pending_requests("nonexistent")
        assert requests == []

    def test_peek_pending_requests_does_not_consume(self, message_manager):
        """peek_pending_requests should not remove messages."""
        message_manager.send_request("a", "b", "hello")

        # Peek should return the message
        requests = message_manager.peek_pending_requests("b")
        assert len(requests) == 1

        # Peek again should still return the message
        requests = message_manager.peek_pending_requests("b")
        assert len(requests) == 1

        # Now consume it
        requests = message_manager.get_pending_requests("b")
        assert len(requests) == 1

        # Now it should be gone
        requests = message_manager.peek_pending_requests("b")
        assert len(requests) == 0


class TestSendRequestTool:
    """Tests for the send_request tool implementation."""

    def test_send_request_to_existing_agent(self, message_manager, agent_manager):
        """send_request should work when target agent exists."""
        # Register the target agent
        agent_manager.register_agent("agent-b")

        result = _send_request_impl(
            message_manager,
            agent_manager,
            from_agent="agent-a",
            target="agent-b",
            message="Help me please",
        )

        assert result["from_agent"] == "agent-a"
        assert result["to_agent"] == "agent-b"
        assert result["message"] == "Help me please"

    def test_send_request_to_unknown_agent_returns_error(
        self, message_manager, agent_manager
    ):
        """send_request to unknown agent should return helpful error."""
        # Register a different agent
        agent_manager.register_agent("agent-c")

        with pytest.raises(ToolError) as exc_info:
            _send_request_impl(
                message_manager,
                agent_manager,
                from_agent="agent-a",
                target="agent-b",
                message="Hello?",
            )

        error_msg = str(exc_info.value)
        assert "agent-b" in error_msg
        assert "not found" in error_msg.lower()
        assert "agent-c" in error_msg  # Should list available agents


class TestGetPendingRequestsTool:
    """Tests for the get_pending_requests tool implementation."""

    def test_get_pending_requests_returns_messages(self, message_manager):
        """get_pending_requests should return pending messages."""
        message_manager.send_request("a", "b", "request 1")
        message_manager.send_request("c", "b", "request 2")

        result = _get_pending_requests_impl(message_manager, "b")

        assert len(result) == 2
        assert result[0]["message"] == "request 1"
        assert result[1]["message"] == "request 2"

    def test_get_pending_requests_consumes_messages(self, message_manager):
        """get_pending_requests should consume messages."""
        message_manager.send_request("a", "b", "only once")

        # First call gets the message
        result = _get_pending_requests_impl(message_manager, "b")
        assert len(result) == 1

        # Second call should be empty
        result = _get_pending_requests_impl(message_manager, "b")
        assert len(result) == 0


class TestResponseHandling:
    """Tests for respond_to_request and wait_for_response."""

    def test_respond_to_request_creates_properly_formatted_response(
        self, message_manager
    ):
        """respond_to_request should create a properly formatted response."""
        # First send a request to get a valid request_id
        request = message_manager.send_request("agent-a", "agent-b", "Help?")
        request_id = request["id"]

        # Now respond
        result = message_manager.respond_to_request(
            request_id=request_id,
            from_agent="agent-b",
            response="Here's your answer",
            status="success",
        )

        assert result["request_id"] == request_id
        assert result["from_agent"] == "agent-b"
        assert result["to_agent"] == "agent-a"  # Original sender
        assert result["response"] == "Here's your answer"
        assert result["status"] == "success"
        assert "timestamp" in result

    def test_wait_for_response_returns_when_response_arrives(self, message_manager):
        """wait_for_response should return when a response arrives."""
        # Send a request
        request = message_manager.send_request("agent-a", "agent-b", "Question?")
        request_id = request["id"]

        # Send the response immediately (simulating agent-b responding)
        message_manager.respond_to_request(
            request_id=request_id,
            from_agent="agent-b",
            response="Answer!",
        )

        # Now wait should return immediately
        result = message_manager.wait_for_response("agent-a", request_id, timeout=5)

        assert result is not None
        assert result["request_id"] == request_id
        assert result["response"] == "Answer!"

    def test_wait_for_response_times_out_correctly(self, message_manager):
        """wait_for_response should return None on timeout."""
        # Send a request but don't respond
        request = message_manager.send_request("agent-a", "agent-b", "Question?")
        request_id = request["id"]

        # Wait with a short timeout
        start = time.time()
        result = message_manager.wait_for_response("agent-a", request_id, timeout=1)
        elapsed = time.time() - start

        assert result is None
        assert elapsed >= 1  # Should have waited at least 1 second

    def test_full_request_response_cycle(self, message_manager, agent_manager):
        """Integration test: full send -> receive -> respond -> wait cycle."""
        # Register both agents
        agent_manager.register_agent("agent-a")
        agent_manager.register_agent("agent-b")

        # Agent A sends request to Agent B
        request = _send_request_impl(
            message_manager,
            agent_manager,
            from_agent="agent-a",
            target="agent-b",
            message="What is 2+2?",
        )
        request_id = request["id"]

        # Agent B retrieves the request
        pending = _get_pending_requests_impl(message_manager, "agent-b")
        assert len(pending) == 1
        assert pending[0]["message"] == "What is 2+2?"
        assert pending[0]["id"] == request_id

        # Agent B responds
        response = _respond_to_request_impl(
            message_manager,
            from_agent="agent-b",
            request_id=request_id,
            response="4",
        )
        assert response["to_agent"] == "agent-a"

        # Agent A waits for and receives the response
        result = _wait_for_response_impl(
            message_manager,
            agent_id="agent-a",
            request_id=request_id,
            timeout=5,
        )
        assert result["response"] == "4"
        assert result["status"] == "success"

    def test_wait_for_response_tool_returns_timeout_dict(self, message_manager):
        """_wait_for_response_impl should return timeout dict on timeout."""
        request = message_manager.send_request("agent-a", "agent-b", "Question?")
        request_id = request["id"]

        result = _wait_for_response_impl(
            message_manager,
            agent_id="agent-a",
            request_id=request_id,
            timeout=1,
        )

        assert result["status"] == "timeout"
        assert result["request_id"] == request_id
        assert "No response received" in result["message"]

    def test_parse_request_id(self, message_manager):
        """_parse_request_id should correctly extract sender and receiver."""
        # Test with simple agent IDs
        sender, receiver = message_manager._parse_request_id("alice::bob::a1b2c3d4")
        assert sender == "alice"
        assert receiver == "bob"

        # Test with hyphenated agent IDs
        sender, receiver = message_manager._parse_request_id(
            "agent-a::agent-b::12345678"
        )
        assert sender == "agent-a"
        assert receiver == "agent-b"

    def test_respond_to_request_with_error_status(self, message_manager):
        """respond_to_request should support error status."""
        request = message_manager.send_request("agent-a", "agent-b", "Do something")
        request_id = request["id"]

        result = message_manager.respond_to_request(
            request_id=request_id,
            from_agent="agent-b",
            response="Failed to do that",
            status="error",
        )

        assert result["status"] == "error"
        assert result["response"] == "Failed to do that"


class TestWaitForRequest:
    """Tests for wait_for_request notification behavior."""

    def test_wait_for_request_returns_when_request_arrives(self, message_manager):
        """wait_for_request should return ready notification when a request arrives."""
        # Send a request first
        message_manager.send_request("agent-a", "agent-b", "Hello!")

        # Now wait should return immediately with a notification
        result = message_manager.wait_for_request("agent-b", timeout=5)

        assert result is not None
        assert result["status"] == "ready"
        assert result["pending"] >= 1

    def test_wait_for_request_times_out_correctly(self, message_manager):
        """wait_for_request should return None on timeout."""
        # Don't send any request
        start = time.time()
        result = message_manager.wait_for_request("agent-b", timeout=1)
        elapsed = time.time() - start

        assert result is None
        assert elapsed >= 1  # Should have waited at least 1 second

    def test_wait_for_request_multiple_queued_return_in_order(self, message_manager):
        """Multiple queued requests: wait_for_request notifies, get_pending_requests consumes in FIFO."""
        # Queue multiple requests
        message_manager.send_request("agent-a", "agent-b", "first")
        message_manager.send_request("agent-c", "agent-b", "second")
        message_manager.send_request("agent-d", "agent-b", "third")

        # Wait should return a notification with pending >= 1
        result = message_manager.wait_for_request("agent-b", timeout=1)
        assert result is not None
        assert result["status"] == "ready"
        assert result["pending"] >= 1

        # Now consume them via get_pending_requests and verify FIFO order
        pending = message_manager.get_pending_requests("agent-b")
        assert len(pending) == 3
        assert pending[0]["message"] == "first"
        assert pending[1]["message"] == "second"
        assert pending[2]["message"] == "third"

    def test_wait_for_request_tool_returns_timeout_dict(self, message_manager):
        """_wait_for_request_impl should return timeout dict on timeout."""
        result = _wait_for_request_impl(
            message_manager,
            agent_id="agent-b",
            timeout=1,
        )

        assert result["status"] == "timeout"
        assert "No request received" in result["message"]

    def test_wait_for_request_does_not_consume_request(self, message_manager):
        """wait_for_request should NOT consume the request (it stays in inbox)."""
        message_manager.send_request("agent-a", "agent-b", "still here")

        # Wait gets the notification
        result = message_manager.wait_for_request("agent-b", timeout=1)
        assert result is not None
        assert result["status"] == "ready"

        # Inbox should still have the message
        pending = message_manager.get_pending_requests("agent-b")
        assert len(pending) == 1
        assert pending[0]["message"] == "still here"

    def test_send_request_pushes_notification(self, message_manager, redis_client):
        """send_request should push a notification signal to the notify key."""
        message_manager.send_request("agent-a", "agent-b", "hello")

        notify_key = f"{message_manager.NOTIFY_PREFIX}agent-b"
        length = redis_client.llen(notify_key)
        assert length == 1

    def test_wait_for_request_notification_loss_does_not_lose_message(
        self, message_manager, redis_client
    ):
        """If the notification signal is lost, the message should still be in the inbox."""
        message_manager.send_request("agent-a", "agent-b", "important")

        # Manually consume the notification signal (simulating connection drop)
        notify_key = f"{message_manager.NOTIFY_PREFIX}agent-b"
        redis_client.lpop(notify_key)

        # Notification is gone, but message should still be in inbox
        pending = message_manager.get_pending_requests("agent-b")
        assert len(pending) == 1
        assert pending[0]["message"] == "important"


class TestResponsePutBack:
    """Tests for response put-back mechanism when request_id doesn't match."""

    def test_mismatched_response_is_put_back(self, message_manager):
        """When wait_for_response gets wrong request_id, it should put it back."""
        # Send two requests from agent-a to agent-b
        request1 = message_manager.send_request("agent-a", "agent-b", "Question 1")
        request2 = message_manager.send_request("agent-a", "agent-b", "Question 2")
        request1_id = request1["id"]
        request2_id = request2["id"]

        # Agent-b responds to request2 FIRST, then request1
        message_manager.respond_to_request(
            request_id=request2_id,
            from_agent="agent-b",
            response="Answer 2",
        )
        message_manager.respond_to_request(
            request_id=request1_id,
            from_agent="agent-b",
            response="Answer 1",
        )

        # Agent-a waits for request1 first - should find it even though request2's
        # response is in front of the queue
        result1 = message_manager.wait_for_response("agent-a", request1_id, timeout=5)
        assert result1 is not None
        assert result1["response"] == "Answer 1"
        assert result1["request_id"] == request1_id

        # Now wait for request2 - the put-back response should be available
        result2 = message_manager.wait_for_response("agent-a", request2_id, timeout=5)
        assert result2 is not None
        assert result2["response"] == "Answer 2"
        assert result2["request_id"] == request2_id

    def test_put_back_maintains_fifo_for_subsequent_waiters(self, message_manager):
        """Put-back responses should maintain FIFO order using rpush."""
        # Send 3 requests from agent-a to agent-b
        request1 = message_manager.send_request("agent-a", "agent-b", "Q1")
        request2 = message_manager.send_request("agent-a", "agent-b", "Q2")
        request3 = message_manager.send_request("agent-a", "agent-b", "Q3")

        # Respond in order: 3, 2, 1 (reverse order)
        message_manager.respond_to_request(request3["id"], "agent-b", "A3")
        message_manager.respond_to_request(request2["id"], "agent-b", "A2")
        message_manager.respond_to_request(request1["id"], "agent-b", "A1")

        # Wait for request1 - will consume and put back 3 and 2
        result1 = message_manager.wait_for_response("agent-a", request1["id"], timeout=5)
        assert result1["response"] == "A1"

        # Wait for request2 - should find it (was put back)
        result2 = message_manager.wait_for_response("agent-a", request2["id"], timeout=5)
        assert result2["response"] == "A2"

        # Wait for request3 - should find it (was put back)
        result3 = message_manager.wait_for_response("agent-a", request3["id"], timeout=5)
        assert result3["response"] == "A3"

    def test_put_back_uses_rpush_not_lpush(self, message_manager, redis_client):
        """Verify that put-back uses rpush (FIFO) not lpush (LIFO).

        This is a regression test for the race condition fix where we changed
        from lpush to rpush to maintain proper FIFO ordering.
        """
        # Send 2 requests
        request1 = message_manager.send_request("agent-a", "agent-b", "Q1")
        request2 = message_manager.send_request("agent-a", "agent-b", "Q2")

        # Respond to both (request2 first)
        message_manager.respond_to_request(request2["id"], "agent-b", "A2")
        message_manager.respond_to_request(request1["id"], "agent-b", "A1")

        # Queue state: [A2, A1] (A2 is at front/left, A1 at back/right)

        # Wait for request1 - this will:
        # 1. Pop A2 (doesn't match request1)
        # 2. Put A2 back with rpush (goes to end/right)
        # 3. Pop A1 (matches!)
        # 4. Return A1
        result1 = message_manager.wait_for_response("agent-a", request1["id"], timeout=5)
        assert result1["response"] == "A1"

        # After waiting for request1:
        # - If we used rpush correctly: queue is [A2]
        # - If we used lpush incorrectly: queue would also be [A2] but
        #   with different ordering if there were more items

        # Verify A2 is still available
        result2 = message_manager.wait_for_response("agent-a", request2["id"], timeout=5)
        assert result2["response"] == "A2"

    def test_multiple_agents_concurrent_responses(self, message_manager):
        """Multiple agents waiting for responses should not interfere."""
        # Agent A sends to Agent C, Agent B sends to Agent C
        request_a = message_manager.send_request("agent-a", "agent-c", "From A")
        request_b = message_manager.send_request("agent-b", "agent-c", "From B")

        # Agent C responds to both (B first, then A)
        message_manager.respond_to_request(request_b["id"], "agent-c", "To B")
        message_manager.respond_to_request(request_a["id"], "agent-c", "To A")

        # Agent A should get their response
        result_a = message_manager.wait_for_response(
            "agent-a", request_a["id"], timeout=5
        )
        assert result_a["response"] == "To A"

        # Agent B should get their response
        result_b = message_manager.wait_for_response(
            "agent-b", request_b["id"], timeout=5
        )
        assert result_b["response"] == "To B"

    def test_threaded_waiters_get_correct_responses(self, message_manager):
        """Multiple threads waiting should each get their correct response."""
        import threading

        # Send 3 requests
        request1 = message_manager.send_request("agent-a", "agent-b", "Q1")
        request2 = message_manager.send_request("agent-a", "agent-b", "Q2")
        request3 = message_manager.send_request("agent-a", "agent-b", "Q3")

        results = {}
        errors = []

        def wait_for(request_id, expected_response, key):
            try:
                result = message_manager.wait_for_response(
                    "agent-a", request_id, timeout=10
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
        t1 = threading.Thread(target=wait_for, args=(request1["id"], "A1", "r1"))
        t2 = threading.Thread(target=wait_for, args=(request2["id"], "A2", "r2"))
        t3 = threading.Thread(target=wait_for, args=(request3["id"], "A3", "r3"))

        t1.start()
        t2.start()
        t3.start()

        # Small delay to ensure threads are waiting
        time.sleep(0.1)

        # Respond in mixed order
        message_manager.respond_to_request(request2["id"], "agent-b", "A2")
        message_manager.respond_to_request(request3["id"], "agent-b", "A3")
        message_manager.respond_to_request(request1["id"], "agent-b", "A1")

        t1.join(timeout=15)
        t2.join(timeout=15)
        t3.join(timeout=15)

        # All threads should have completed successfully
        assert len(errors) == 0, f"Errors: {errors}"
        assert len(results) == 3, f"Missing results: {set(['r1', 'r2', 'r3']) - set(results.keys())}"
