"""Message passing between agents for C3PO coordinator."""

import json
import uuid
from datetime import datetime, timezone
from typing import Optional

import redis


class MessageManager:
    """Manages message queues using Redis."""

    INBOX_PREFIX = "c3po:inbox:"
    RATE_LIMIT_PREFIX = "c3po:rate:"

    # Configuration
    RATE_LIMIT_REQUESTS = 10  # Max requests per window
    RATE_LIMIT_WINDOW_SECONDS = 60  # Window size in seconds
    MESSAGE_TTL_SECONDS = 24 * 60 * 60  # 24 hours

    def __init__(self, redis_client: redis.Redis):
        """Initialize with Redis client.

        Args:
            redis_client: Redis client instance (can be real or fakeredis)
        """
        self.redis = redis_client

    # Delimiter for request IDs that's unlikely to be in agent IDs
    REQUEST_ID_DELIMITER = "::"

    def _generate_request_id(self, from_agent: str, to_agent: str) -> str:
        """Generate a unique request ID.

        Format: {from_agent}::{to_agent}::{uuid}
        Uses :: as delimiter (unlikely to be in agent IDs).

        Args:
            from_agent: Sending agent ID
            to_agent: Target agent ID

        Returns:
            Unique request ID string
        """
        unique = uuid.uuid4().hex[:8]
        return f"{from_agent}{self.REQUEST_ID_DELIMITER}{to_agent}{self.REQUEST_ID_DELIMITER}{unique}"

    def check_rate_limit(self, agent_id: str) -> tuple[bool, int]:
        """Check if an agent has exceeded rate limit.

        Uses a sliding window counter in Redis.

        Args:
            agent_id: The agent to check

        Returns:
            Tuple of (is_allowed, current_count)
        """
        rate_key = f"{self.RATE_LIMIT_PREFIX}{agent_id}"
        now = datetime.now(timezone.utc).timestamp()
        window_start = now - self.RATE_LIMIT_WINDOW_SECONDS

        # Remove old entries outside the window
        self.redis.zremrangebyscore(rate_key, "-inf", window_start)

        # Count current requests in window
        current_count = self.redis.zcard(rate_key)

        if current_count >= self.RATE_LIMIT_REQUESTS:
            return False, current_count

        return True, current_count

    def record_request(self, agent_id: str) -> None:
        """Record a request for rate limiting.

        Args:
            agent_id: The agent making the request
        """
        rate_key = f"{self.RATE_LIMIT_PREFIX}{agent_id}"
        now = datetime.now(timezone.utc).timestamp()

        # Add this request to the sorted set (score = timestamp)
        self.redis.zadd(rate_key, {f"{now}": now})

        # Set expiry on the rate limit key
        self.redis.expire(rate_key, self.RATE_LIMIT_WINDOW_SECONDS * 2)

    def send_request(
        self,
        from_agent: str,
        to_agent: str,
        message: str,
        context: Optional[str] = None,
    ) -> dict:
        """Send a request from one agent to another.

        Args:
            from_agent: ID of the sending agent
            to_agent: ID of the target agent
            message: The request message
            context: Optional context/background for the request

        Returns:
            Request data dict with id, from_agent, to_agent, message, etc.
        """
        request_id = self._generate_request_id(from_agent, to_agent)
        now = datetime.now(timezone.utc).isoformat()

        request_data = {
            "id": request_id,
            "from_agent": from_agent,
            "to_agent": to_agent,
            "message": message,
            "context": context,
            "timestamp": now,
            "status": "pending",
        }

        # Push to target agent's inbox (RPUSH for FIFO order)
        inbox_key = f"{self.INBOX_PREFIX}{to_agent}"
        self.redis.rpush(inbox_key, json.dumps(request_data))

        # Set TTL on inbox key (24h default) - prevents stale messages accumulating
        self.redis.expire(inbox_key, self.MESSAGE_TTL_SECONDS)

        return request_data

    def _is_message_expired(self, message: dict) -> bool:
        """Check if a message has expired based on its timestamp.

        Args:
            message: Message dict with timestamp field

        Returns:
            True if message is expired, False otherwise
        """
        timestamp_str = message.get("timestamp")
        if not timestamp_str:
            return False  # No timestamp, assume not expired

        try:
            msg_time = datetime.fromisoformat(timestamp_str)
            now = datetime.now(timezone.utc)
            age_seconds = (now - msg_time).total_seconds()
            return age_seconds > self.MESSAGE_TTL_SECONDS
        except (ValueError, TypeError):
            return False  # Invalid timestamp, assume not expired

    def _filter_expired_messages(self, messages: list[dict]) -> list[dict]:
        """Filter out expired messages from a list.

        Args:
            messages: List of message dicts

        Returns:
            List with expired messages removed
        """
        return [m for m in messages if not self._is_message_expired(m)]

    def get_pending_requests(self, agent_id: str) -> list[dict]:
        """Get and consume all pending requests for an agent.

        This removes the requests from the inbox (they are consumed).
        Expired messages are automatically filtered out.

        Args:
            agent_id: The agent whose inbox to check

        Returns:
            List of request dicts, oldest first (FIFO), excluding expired
        """
        inbox_key = f"{self.INBOX_PREFIX}{agent_id}"
        requests = []

        # Pop all messages from the list
        while True:
            data = self.redis.lpop(inbox_key)
            if data is None:
                break

            if isinstance(data, bytes):
                data = data.decode()

            message = json.loads(data)
            # Skip expired messages
            if not self._is_message_expired(message):
                requests.append(message)

        return requests

    def peek_pending_requests(self, agent_id: str) -> list[dict]:
        """Peek at pending requests without consuming them.

        Used by REST API for hooks to check status.
        Expired messages are automatically filtered out.

        Args:
            agent_id: The agent whose inbox to check

        Returns:
            List of request dicts, oldest first (FIFO), excluding expired
        """
        inbox_key = f"{self.INBOX_PREFIX}{agent_id}"

        # Get all items without removing (LRANGE 0 -1 gets all)
        raw_items = self.redis.lrange(inbox_key, 0, -1)
        requests = []

        for data in raw_items:
            if isinstance(data, bytes):
                data = data.decode()
            message = json.loads(data)
            # Skip expired messages
            if not self._is_message_expired(message):
                requests.append(message)

        return requests

    def pending_count(self, agent_id: str) -> int:
        """Get the count of pending requests for an agent.

        Args:
            agent_id: The agent whose inbox to check

        Returns:
            Number of pending requests
        """
        inbox_key = f"{self.INBOX_PREFIX}{agent_id}"
        return self.redis.llen(inbox_key)

    RESPONSES_PREFIX = "c3po:responses:"

    def _parse_request_id(self, request_id: str) -> tuple[str, str]:
        """Parse a request ID to extract the original sender and receiver.

        Request ID format: {from_agent}::{to_agent}::{uuid}

        Args:
            request_id: The request ID to parse

        Returns:
            Tuple of (from_agent, to_agent)

        Raises:
            ValueError: If request_id is invalid
        """
        parts = request_id.split(self.REQUEST_ID_DELIMITER)
        if len(parts) != 3:
            raise ValueError(f"Invalid request_id format: {request_id}")

        from_agent, to_agent, _ = parts
        return from_agent, to_agent

    def respond_to_request(
        self,
        request_id: str,
        from_agent: str,
        response: str,
        status: str = "success",
    ) -> dict:
        """Send a response to a previous request.

        Args:
            request_id: The ID of the original request
            from_agent: The agent sending the response (must be the original recipient)
            response: The response message
            status: Response status, default "success"

        Returns:
            Response data dict with request_id, from_agent, to_agent, response, etc.
        """
        # Parse request_id to find original sender
        original_sender, original_recipient = self._parse_request_id(request_id)

        now = datetime.now(timezone.utc).isoformat()

        response_data = {
            "request_id": request_id,
            "from_agent": from_agent,
            "to_agent": original_sender,
            "response": response,
            "status": status,
            "timestamp": now,
        }

        # Push to original sender's response queue
        response_key = f"{self.RESPONSES_PREFIX}{original_sender}"
        self.redis.rpush(response_key, json.dumps(response_data))

        return response_data

    def wait_for_response(
        self,
        agent_id: str,
        request_id: str,
        timeout: int = 60,
    ) -> Optional[dict]:
        """Wait for a response to a specific request.

        Uses Redis BLPOP to block until a response arrives or timeout.

        Args:
            agent_id: The agent waiting for the response
            request_id: The request ID to wait for
            timeout: Timeout in seconds (default 60)

        Returns:
            Response dict if received, or None if timeout
        """
        response_key = f"{self.RESPONSES_PREFIX}{agent_id}"
        deadline = datetime.now(timezone.utc).timestamp() + timeout

        while True:
            remaining = deadline - datetime.now(timezone.utc).timestamp()
            if remaining <= 0:
                return None

            # BLPOP returns (key, value) or None on timeout
            # Use at least 1 second timeout (0 means wait forever in Redis)
            blpop_timeout = max(1, int(min(remaining, 1)))
            result = self.redis.blpop(response_key, timeout=blpop_timeout)

            if result is None:
                continue

            _, data = result
            if isinstance(data, bytes):
                data = data.decode()

            response = json.loads(data)

            # Check if this is the response we're waiting for
            if response.get("request_id") == request_id:
                return response

            # Not our response, put it back for another waiter
            # (push to front since it was originally at front)
            self.redis.lpush(response_key, json.dumps(response))

    def wait_for_request(
        self,
        agent_id: str,
        timeout: int = 60,
    ) -> Optional[dict]:
        """Wait for an incoming request using blocking Redis BLPOP.

        This is an alternative to polling get_pending_requests.
        Blocks until a request arrives or timeout.

        Args:
            agent_id: The agent waiting for requests
            timeout: Timeout in seconds (default 60)

        Returns:
            Request dict if received, or None if timeout
        """
        inbox_key = f"{self.INBOX_PREFIX}{agent_id}"

        # BLPOP returns (key, value) or None on timeout
        # timeout=0 means block forever, so ensure at least 1 second
        blpop_timeout = max(1, timeout)
        result = self.redis.blpop(inbox_key, timeout=blpop_timeout)

        if result is None:
            return None

        _, data = result
        if isinstance(data, bytes):
            data = data.decode()

        return json.loads(data)
