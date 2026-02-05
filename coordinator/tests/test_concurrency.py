"""Concurrency tests for C3PO coordinator subsystems.

Exercises race conditions and thread safety across MessageManager,
AgentManager, and RateLimiter using threading and concurrent.futures.
"""

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import fakeredis
import pytest

from coordinator.agents import AgentManager
from coordinator.messaging import MessageManager
from coordinator.rate_limit import RateLimiter
from coordinator.server import _wait_for_message_impl


# ---------------------------------------------------------------------------
# Fixtures (inline, matching existing test pattern — no conftest.py exists)
# ---------------------------------------------------------------------------

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


@pytest.fixture
def rate_limiter(redis_client):
    """Create RateLimiter with fakeredis."""
    return RateLimiter(redis_client)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run_threads(n, target, args_fn=None):
    """Run *n* threads calling *target*, collect results and errors.

    Args:
        n: Number of threads to launch.
        target: Callable to run in each thread.
        args_fn: Optional callable(index) → tuple of args.

    Returns:
        (results, errors) — both lists.
    """
    results = []
    errors = []
    lock = threading.Lock()

    def worker(idx):
        try:
            args = args_fn(idx) if args_fn else ()
            r = target(*args)
            with lock:
                results.append(r)
        except Exception as e:
            with lock:
                errors.append(f"Thread {idx}: {e}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    return results, errors


# ===========================================================================
# 1. Concurrent message sends
# ===========================================================================

class TestConcurrentMessageSends:
    """Target: MessageManager.send_message"""

    def test_concurrent_sends_no_message_loss(self, message_manager):
        """20 threads each send 1 message — peek must show exactly 20."""
        target_agent = "test/target"

        def send(idx):
            return message_manager.send_message(
                from_agent=f"sender-{idx}",
                to_agent=target_agent,
                message=f"msg-{idx}",
            )

        results, errors = _run_threads(20, send, args_fn=lambda i: (i,))
        assert errors == [], f"Thread errors: {errors}"

        msgs = message_manager.peek_messages(target_agent)
        assert len(msgs) == 20
        ids = {m["id"] for m in msgs}
        assert len(ids) == 20

    def test_concurrent_sends_unique_ids(self, message_manager):
        """10 threads send messages; returned IDs must all be unique."""
        target_agent = "test/target"

        def send(idx):
            return message_manager.send_message(
                from_agent=f"sender-{idx}",
                to_agent=target_agent,
                message=f"msg-{idx}",
            )

        results, errors = _run_threads(10, send, args_fn=lambda i: (i,))
        assert errors == [], f"Thread errors: {errors}"

        returned_ids = [r["id"] for r in results]
        assert len(returned_ids) == len(set(returned_ids))


# ===========================================================================
# 2. Concurrent ack and compaction
# ===========================================================================

class TestConcurrentAckAndCompaction:
    """Target: MessageManager.ack_messages + _compact_queues"""

    def test_concurrent_acks_preserve_unacked_messages(self, message_manager):
        """Send 30. Keep 5. Split 25 into 5 batches acked by 5 threads.
        After all complete, peek must show exactly the 5 kept messages."""
        agent = "test/acker"
        sent = []
        for i in range(30):
            m = message_manager.send_message("sender", agent, f"msg-{i}")
            sent.append(m)

        keep_ids = {sent[i]["id"] for i in range(5)}
        ack_targets = [s for s in sent if s["id"] not in keep_ids]
        batches = [ack_targets[i::5] for i in range(5)]

        def ack_batch(idx):
            ids = [m["id"] for m in batches[idx]]
            return message_manager.ack_messages(agent, ids)

        results, errors = _run_threads(5, ack_batch, args_fn=lambda i: (i,))
        assert errors == [], f"Thread errors: {errors}"

        remaining = message_manager.peek_messages(agent)
        remaining_ids = {m["id"] for m in remaining}
        assert remaining_ids == keep_ids

    def test_compaction_ack_race_no_duplicates(self, message_manager):
        """Thread A acks IDs 1-21 (triggers compaction). Thread B acks 22-25.
        After both, peek must show exactly messages 26-30, no duplicates."""
        agent = "test/acker"
        sent = []
        for i in range(30):
            m = message_manager.send_message("sender", agent, f"msg-{i}")
            sent.append(m)

        batch_a = [s["id"] for s in sent[:21]]
        batch_b = [s["id"] for s in sent[21:25]]
        expected_ids = {s["id"] for s in sent[25:]}

        barrier = threading.Barrier(2)

        def ack_a():
            barrier.wait()
            return message_manager.ack_messages(agent, batch_a)

        def ack_b():
            barrier.wait()
            return message_manager.ack_messages(agent, batch_b)

        threads = [
            threading.Thread(target=ack_a),
            threading.Thread(target=ack_b),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        remaining = message_manager.peek_messages(agent)
        remaining_ids = [m["id"] for m in remaining]
        # No duplicates
        assert len(remaining_ids) == len(set(remaining_ids)), "Duplicate messages found"
        assert set(remaining_ids) == expected_ids

    def test_repeated_compaction_integrity(self, message_manager):
        """3 rounds: send 25, ack 21, keep 4. After 3 rounds peek shows 12."""
        agent = "test/repeater"
        all_keep_ids = set()

        for _round in range(3):
            sent = []
            for i in range(25):
                m = message_manager.send_message("sender", agent, f"r{_round}-msg-{i}")
                sent.append(m)
            keep = {s["id"] for s in sent[:4]}
            all_keep_ids |= keep
            ack_ids = [s["id"] for s in sent if s["id"] not in keep]
            message_manager.ack_messages(agent, ack_ids)

        remaining = message_manager.peek_messages(agent)
        remaining_ids = {m["id"] for m in remaining}
        assert remaining_ids == all_keep_ids
        assert len(remaining) == 12


# ===========================================================================
# 3. Concurrent agent registration
# ===========================================================================

class TestConcurrentAgentRegistration:
    """Target: AgentManager.register_agent + _resolve_collision"""

    def test_concurrent_registration_unique_returned_ids(self, agent_manager):
        """10 threads register 'test/agent' with different session_ids.
        All 10 returned IDs must be unique."""

        def register(idx):
            return agent_manager.register_agent(
                agent_id="test/agent",
                session_id=f"session-{idx}",
            )

        results, errors = _run_threads(10, register, args_fn=lambda i: (i,))
        assert errors == [], f"Thread errors: {errors}"

        returned_ids = [r["id"] for r in results]
        assert len(returned_ids) == len(set(returned_ids)), (
            f"Duplicate returned IDs: {returned_ids}"
        )

    def test_concurrent_registration_registry_state(self, agent_manager):
        """After concurrent registration, registry has no duplicate IDs
        and one agent has the base ID 'test/agent'."""

        def register(idx):
            return agent_manager.register_agent(
                agent_id="test/agent",
                session_id=f"session-{idx}",
            )

        results, errors = _run_threads(10, register, args_fn=lambda i: (i,))
        assert errors == [], f"Thread errors: {errors}"

        agents = agent_manager.list_agents()
        agent_ids = [a["id"] for a in agents]
        # No duplicates in registry
        assert len(agent_ids) == len(set(agent_ids)), (
            f"Duplicate IDs in registry: {agent_ids}"
        )
        # Base ID should exist
        assert "test/agent" in agent_ids


# ===========================================================================
# 4. Concurrent rate limiting
# ===========================================================================

class TestConcurrentRateLimiting:
    """Target: RateLimiter.check_and_record"""

    def test_concurrent_rate_limit_approximate_enforcement(self, rate_limiter):
        """20 threads hit the same identity with limit=10. Allowed count
        should be between 10 and 15 (TOCTOU gap tolerance)."""
        allowed_count = 0
        lock = threading.Lock()
        errors = []

        def check(idx):
            nonlocal allowed_count
            try:
                allowed, _ = rate_limiter.check_and_record(
                    "test_op", "agent-a",
                    max_requests=10, window_seconds=60,
                )
                if allowed:
                    with lock:
                        allowed_count += 1
            except Exception as e:
                with lock:
                    errors.append(f"Thread {idx}: {e}")

        threads = [threading.Thread(target=check, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert errors == [], f"Thread errors: {errors}"
        assert 10 <= allowed_count <= 15, (
            f"Expected 10-15 allowed, got {allowed_count}"
        )

    def test_concurrent_rate_limit_per_identity_isolation(self, rate_limiter):
        """10 identities × 5 calls each, all with limit=5. All 50 should be allowed."""
        denied = []
        lock = threading.Lock()
        errors = []

        def check(idx):
            try:
                for _ in range(5):
                    allowed, _ = rate_limiter.check_and_record(
                        "test_op", f"agent-{idx}",
                        max_requests=5, window_seconds=60,
                    )
                    if not allowed:
                        with lock:
                            denied.append(idx)
            except Exception as e:
                with lock:
                    errors.append(f"Thread {idx}: {e}")

        threads = [threading.Thread(target=check, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert errors == [], f"Thread errors: {errors}"
        assert denied == [], f"Unexpected denials for identities: {denied}"


# ===========================================================================
# 5. Wait-for-message non-blocking
# ===========================================================================

class TestWaitForMessageNonBlocking:
    """Target: _wait_for_message_impl + MessageManager.wait_for_message"""

    def test_wait_does_not_block_other_operations(
        self, redis_client, message_manager, agent_manager
    ):
        """A blocked wait_for_message must not prevent other operations."""
        # Register agents so list_agents has something to return
        agent_manager.register_agent("test/waiter", session_id="s1")
        agent_manager.register_agent("test/other", session_id="s2")

        waiter_result = {}
        waiter_error = []

        def waiter():
            try:
                r = _wait_for_message_impl(message_manager, "test/waiter", timeout=3)
                waiter_result["value"] = r
            except Exception as e:
                waiter_error.append(str(e))

        t = threading.Thread(target=waiter)
        t.start()
        time.sleep(0.5)

        # These operations should complete quickly while waiter is blocked
        t0 = time.time()
        message_manager.send_message("test/x", "test/y", "hi")
        send_elapsed = time.time() - t0

        t0 = time.time()
        message_manager.peek_messages("test/other")
        peek_elapsed = time.time() - t0

        t0 = time.time()
        agent_manager.list_agents()
        list_elapsed = time.time() - t0

        # Unblock waiter
        message_manager.send_message("test/unblock", "test/waiter", "wake up")
        t.join(timeout=5)

        assert waiter_error == [], f"Waiter errors: {waiter_error}"
        assert send_elapsed < 1.0, f"send_message took {send_elapsed}s"
        assert peek_elapsed < 1.0, f"peek_messages took {peek_elapsed}s"
        assert list_elapsed < 1.0, f"list_agents took {list_elapsed}s"

    def test_multiple_waiters_independent(self, message_manager):
        """5 waiters on different agents. Only agent-2 gets a message.
        agent-2 should return received; others should timeout."""
        results = {}
        errors = []
        lock = threading.Lock()

        def waiter(idx):
            agent = f"test/agent-{idx}"
            try:
                r = _wait_for_message_impl(message_manager, agent, timeout=3)
                with lock:
                    results[idx] = r
            except Exception as e:
                with lock:
                    errors.append(f"Thread {idx}: {e}")

        threads = [threading.Thread(target=waiter, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()

        time.sleep(0.5)
        message_manager.send_message("test/sender", "test/agent-2", "hello agent-2")

        for t in threads:
            t.join(timeout=5)

        assert errors == [], f"Thread errors: {errors}"
        assert results[2]["status"] == "received"
        for i in [0, 1, 3, 4]:
            assert results[i]["status"] == "timeout", (
                f"agent-{i} expected timeout, got {results[i]['status']}"
            )


# ===========================================================================
# 6. Concurrent peek and send
# ===========================================================================

class TestConcurrentPeekAndSend:
    """Target: MessageManager.peek_messages + send_message concurrent access"""

    def test_peek_during_concurrent_sends(self, message_manager):
        """Thread A sends 20 messages. Thread B peeks 20 times.
        No exceptions, final peek shows all 20."""
        agent = "test/target"
        peek_results = []
        errors = []
        lock = threading.Lock()

        def sender():
            try:
                for i in range(20):
                    message_manager.send_message(f"sender-{i}", agent, f"msg-{i}")
            except Exception as e:
                with lock:
                    errors.append(f"Sender: {e}")

        def peeker():
            try:
                for _ in range(20):
                    msgs = message_manager.peek_messages(agent)
                    with lock:
                        peek_results.append(len(msgs))
            except Exception as e:
                with lock:
                    errors.append(f"Peeker: {e}")

        t_send = threading.Thread(target=sender)
        t_peek = threading.Thread(target=peeker)
        t_send.start()
        t_peek.start()
        t_send.join(timeout=10)
        t_peek.join(timeout=10)

        assert errors == [], f"Thread errors: {errors}"

        final = message_manager.peek_messages(agent)
        assert len(final) == 20

        # Each peeked message must have the required fields
        for m in final:
            assert "id" in m
            assert "from_agent" in m
            assert "message" in m
            assert "timestamp" in m

    def test_all_messages_structurally_valid_after_concurrent_sends(
        self, message_manager
    ):
        """10 threads simultaneously send messages. All messages valid afterwards."""
        agent = "test/target"
        required_fields = {"id", "from_agent", "to_agent", "message", "timestamp", "status"}

        def send(idx):
            return message_manager.send_message(
                f"sender-{idx}", agent, f"payload-{idx}"
            )

        results, errors = _run_threads(10, send, args_fn=lambda i: (i,))
        assert errors == [], f"Thread errors: {errors}"

        msgs = message_manager.peek_messages(agent)
        assert len(msgs) == 10
        for m in msgs:
            missing = required_fields - set(m.keys())
            assert not missing, f"Message missing fields {missing}: {m}"


# ===========================================================================
# 7. Compaction under load
# ===========================================================================

class TestCompactionUnderLoad:
    """Target: _compact_list during concurrent writes"""

    def test_compaction_during_continuous_send(self, message_manager):
        """Send 25 (phase 1). Thread A acks 21 (compaction). Thread B sends 10.
        After both: all 4 un-acked phase-1 + all 10 phase-2 = 14 total.

        Note: _compact_list (LRANGE then DELETE+RPUSH) is non-atomic, so
        messages sent between LRANGE and DELETE may be lost. This test
        documents that race condition — it will fail when the race triggers.
        """
        agent = "test/compactor"
        phase1 = []
        for i in range(25):
            m = message_manager.send_message("sender", agent, f"p1-{i}")
            phase1.append(m)

        keep_ids = {phase1[i]["id"] for i in range(4)}
        ack_ids = [m["id"] for m in phase1 if m["id"] not in keep_ids]

        barrier = threading.Barrier(2)
        phase2_ids = []
        errors = []
        lock = threading.Lock()

        def acker():
            try:
                barrier.wait()
                message_manager.ack_messages(agent, ack_ids)
            except Exception as e:
                with lock:
                    errors.append(f"Acker: {e}")

        def sender():
            try:
                barrier.wait()
                for i in range(10):
                    m = message_manager.send_message("sender", agent, f"p2-{i}")
                    with lock:
                        phase2_ids.append(m["id"])
            except Exception as e:
                with lock:
                    errors.append(f"Sender: {e}")

        t_ack = threading.Thread(target=acker)
        t_send = threading.Thread(target=sender)
        t_ack.start()
        t_send.start()
        t_ack.join(timeout=10)
        t_send.join(timeout=10)

        assert errors == [], f"Thread errors: {errors}"

        remaining = message_manager.peek_messages(agent)
        remaining_ids = {m["id"] for m in remaining}

        # All kept phase-1 messages must survive
        assert keep_ids.issubset(remaining_ids), (
            f"Lost phase-1 messages: {keep_ids - remaining_ids}"
        )
        # All phase-2 messages must survive
        assert set(phase2_ids).issubset(remaining_ids), (
            f"Lost phase-2 messages: {set(phase2_ids) - remaining_ids}"
        )
        assert len(remaining) == 14

    def test_compaction_message_ordering_preserved(self, message_manager):
        """Send 30 in order. Ack 21 (triggers compaction). Remaining 9
        must preserve their original relative order."""
        agent = "test/orderer"
        sent = []
        for i in range(30):
            m = message_manager.send_message("sender", agent, f"msg-{i}")
            sent.append(m)

        ack_ids = [s["id"] for s in sent[:21]]
        expected_order = [s["id"] for s in sent[21:]]

        message_manager.ack_messages(agent, ack_ids)

        remaining = message_manager.peek_messages(agent)
        remaining_ids = [m["id"] for m in remaining]

        assert remaining_ids == expected_order


# ===========================================================================
# 8. Message delivery latency
# ===========================================================================

class TestMessageDeliveryLatency:
    """Target: wait_for_message + send_message end-to-end latency"""

    def test_send_to_receive_latency_under_one_second(self, message_manager):
        """Send → wait_for_message receive latency must be < 1 second."""
        agent = "test/listener"
        receive_time = {}
        errors = []

        def waiter():
            try:
                r = _wait_for_message_impl(message_manager, agent, timeout=5)
                receive_time["t"] = time.time()
                receive_time["result"] = r
            except Exception as e:
                errors.append(str(e))

        t = threading.Thread(target=waiter)
        t.start()
        time.sleep(0.3)

        send_time = time.time()
        message_manager.send_message("test/sender", agent, "ping")

        t.join(timeout=5)
        assert errors == [], f"Waiter errors: {errors}"
        assert receive_time["result"]["status"] == "received"
        latency = receive_time["t"] - send_time
        assert latency < 1.0, f"Latency {latency:.3f}s exceeds 1s"

    def test_send_to_receive_latency_multiple_rounds(self, message_manager):
        """5 sequential send/receive rounds, each < 1 second."""
        agent = "test/listener"

        for round_num in range(5):
            receive_time = {}
            errors = []

            def waiter():
                try:
                    r = _wait_for_message_impl(message_manager, agent, timeout=5)
                    receive_time["t"] = time.time()
                    receive_time["result"] = r
                except Exception as e:
                    errors.append(str(e))

            t = threading.Thread(target=waiter)
            t.start()
            time.sleep(0.3)

            send_time = time.time()
            m = message_manager.send_message("test/sender", agent, f"round-{round_num}")

            t.join(timeout=5)
            assert errors == [], f"Round {round_num} waiter errors: {errors}"
            assert receive_time["result"]["status"] == "received"
            latency = receive_time["t"] - send_time
            assert latency < 1.0, f"Round {round_num} latency {latency:.3f}s exceeds 1s"

            # Ack the message before the next round
            msg_ids = [msg["id"] for msg in receive_time["result"]["messages"]]
            message_manager.ack_messages(agent, msg_ids)
