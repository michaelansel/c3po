"""Comprehensive rate limiting for C3PO coordinator.

Provides per-agent and per-IP rate limiting with configurable limits
per operation type. Uses Redis sorted sets with sliding windows.
"""

import logging
from datetime import datetime, timezone

import redis

logger = logging.getLogger("c3po.rate_limit")


# Rate limit configurations: (max_requests, window_seconds)
RATE_LIMITS = {
    "send_message": (100, 60),
    "reply": (100, 60),
    "get_messages": (30, 60),
    "wait_for_message": (30, 60),
    "ack_messages": (30, 60),
    "list_agents": (30, 60),
    "rest_register": (5, 60),
    "rest_pending": (30, 60),
    "rest_unregister": (5, 60),
    "upload_blob": (10, 60),
    "fetch_blob": (30, 60),
    "rest_blob_upload": (10, 60),
    "rest_blob_download": (30, 60),
}


class RateLimiter:
    """General-purpose rate limiter using Redis sorted sets."""

    PREFIX = "c3po:rate:"

    def __init__(self, redis_client: redis.Redis):
        self.redis = redis_client

    def _key(self, operation: str, identity: str) -> str:
        """Build Redis key for rate tracking."""
        return f"{self.PREFIX}{operation}:{identity}"

    def check_and_record(
        self,
        operation: str,
        identity: str,
        max_requests: int | None = None,
        window_seconds: int | None = None,
    ) -> tuple[bool, int]:
        """Check rate limit and record the request if allowed.

        Args:
            operation: Operation type (must be in RATE_LIMITS or provide overrides)
            identity: Agent ID or IP address
            max_requests: Override max requests (default: from RATE_LIMITS)
            window_seconds: Override window (default: from RATE_LIMITS)

        Returns:
            Tuple of (is_allowed, current_count)
        """
        if max_requests is None or window_seconds is None:
            config = RATE_LIMITS.get(operation)
            if config:
                max_requests = max_requests or config[0]
                window_seconds = window_seconds or config[1]
            else:
                # Unknown operation, use generous defaults
                max_requests = max_requests or 60
                window_seconds = window_seconds or 60

        key = self._key(operation, identity)
        now = datetime.now(timezone.utc).timestamp()
        window_start = now - window_seconds

        pipe = self.redis.pipeline()
        pipe.zremrangebyscore(key, "-inf", window_start)
        pipe.zcard(key)
        results = pipe.execute()
        current_count = results[1]

        if current_count >= max_requests:
            logger.warning(
                "rate_limited operation=%s identity=%s count=%d limit=%d",
                operation, identity, current_count, max_requests,
            )
            return False, current_count

        # Record the request
        pipe = self.redis.pipeline()
        pipe.zadd(key, {f"{now}": now})
        pipe.expire(key, window_seconds * 2)
        pipe.execute()

        return True, current_count + 1

    def check_only(
        self,
        operation: str,
        identity: str,
        max_requests: int | None = None,
        window_seconds: int | None = None,
    ) -> tuple[bool, int]:
        """Check rate limit without recording.

        Same as check_and_record but doesn't increment the counter.
        """
        if max_requests is None or window_seconds is None:
            config = RATE_LIMITS.get(operation)
            if config:
                max_requests = max_requests or config[0]
                window_seconds = window_seconds or config[1]
            else:
                max_requests = max_requests or 60
                window_seconds = window_seconds or 60

        key = self._key(operation, identity)
        now = datetime.now(timezone.utc).timestamp()
        window_start = now - window_seconds

        self.redis.zremrangebyscore(key, "-inf", window_start)
        current_count = self.redis.zcard(key)

        return current_count < max_requests, current_count
