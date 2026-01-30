"""Tests for C3PO comprehensive rate limiting."""

import pytest
import fakeredis

from coordinator.rate_limit import RateLimiter, RATE_LIMITS


@pytest.fixture
def redis_client():
    """Create a fresh fakeredis client for each test."""
    return fakeredis.FakeRedis()


@pytest.fixture
def limiter(redis_client):
    """Create RateLimiter with fakeredis."""
    return RateLimiter(redis_client)


class TestRateLimiter:
    """Tests for the general-purpose rate limiter."""

    def test_allows_under_limit(self, limiter):
        """Requests under limit are allowed."""
        allowed, count = limiter.check_and_record("send_request", "agent-a")
        assert allowed is True
        assert count == 1

    def test_blocks_over_limit(self, limiter):
        """Requests over limit are blocked."""
        max_req = RATE_LIMITS["send_request"][0]
        for _ in range(max_req):
            limiter.check_and_record("send_request", "agent-a")

        allowed, count = limiter.check_and_record("send_request", "agent-a")
        assert allowed is False
        assert count == max_req

    def test_per_identity(self, limiter):
        """Rate limits are per-identity."""
        max_req = RATE_LIMITS["send_request"][0]
        for _ in range(max_req):
            limiter.check_and_record("send_request", "agent-a")

        # Different identity should still be allowed
        allowed, _ = limiter.check_and_record("send_request", "agent-b")
        assert allowed is True

    def test_per_operation(self, limiter):
        """Rate limits are per-operation."""
        max_req = RATE_LIMITS["send_request"][0]
        for _ in range(max_req):
            limiter.check_and_record("send_request", "agent-a")

        # Different operation should still be allowed
        allowed, _ = limiter.check_and_record("list_agents", "agent-a")
        assert allowed is True

    def test_custom_limits(self, limiter):
        """Custom limits override defaults."""
        for _ in range(3):
            limiter.check_and_record("custom_op", "agent-a",
                                     max_requests=3, window_seconds=60)

        allowed, _ = limiter.check_and_record("custom_op", "agent-a",
                                               max_requests=3, window_seconds=60)
        assert allowed is False

    def test_check_only_doesnt_count(self, limiter):
        """check_only doesn't increment the counter."""
        allowed, count = limiter.check_only("send_request", "agent-a")
        assert allowed is True
        assert count == 0

        # Still at 0 after check_only
        allowed, count = limiter.check_only("send_request", "agent-a")
        assert count == 0

    def test_different_operations_have_different_limits(self, limiter):
        """send_request (10/60s) vs list_agents (30/60s) have different limits."""
        send_limit = RATE_LIMITS["send_request"][0]
        list_limit = RATE_LIMITS["list_agents"][0]
        assert send_limit != list_limit  # They should differ

    def test_unknown_operation_uses_defaults(self, limiter):
        """Unknown operations get generous default (60/60s)."""
        for _ in range(59):
            allowed, _ = limiter.check_and_record("unknown_op", "agent-a")
            assert allowed is True

    def test_rest_register_limit(self, limiter):
        """REST register has its own limit."""
        max_req = RATE_LIMITS["rest_register"][0]
        for _ in range(max_req):
            limiter.check_and_record("rest_register", "192.168.1.1")

        allowed, _ = limiter.check_and_record("rest_register", "192.168.1.1")
        assert allowed is False
