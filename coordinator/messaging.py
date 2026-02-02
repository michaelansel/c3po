"""Message passing between agents for C3PO coordinator."""

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

import redis

logger = logging.getLogger("c3po.messaging")


class MessageManager:
    """Manages message queues using Redis."""

    INBOX_PREFIX = "c3po:inbox:"
    NOTIFY_PREFIX = "c3po:notify:"
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

    # Delimiter for message IDs that's unlikely to be in agent IDs
    MESSAGE_ID_DELIMITER = "::"

    def _generate_message_id(self, from_agent: str, to_agent: str) -> str:
        """Generate a unique message ID.

        Format: {from_agent}::{to_agent}::{uuid}
        Uses :: as delimiter (unlikely to be in agent IDs).

        Args:
            from_agent: Sending agent ID
            to_agent: Target agent ID

        Returns:
            Unique message ID string
        """
        unique = uuid.uuid4().hex[:8]
        return f"{from_agent}{self.MESSAGE_ID_DELIMITER}{to_agent}{self.MESSAGE_ID_DELIMITER}{unique}"

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

    def send_message(
        self,
        from_agent: str,
        to_agent: str,
        message: str,
        context: Optional[str] = None,
    ) -> dict:
        """Send a message from one agent to another.

        Args:
            from_agent: ID of the sending agent
            to_agent: ID of the target agent
            message: The message content
            context: Optional context/background for the message

        Returns:
            Message data dict with id, from_agent, to_agent, message, etc.
        """
        message_id = self._generate_message_id(from_agent, to_agent)
        now = datetime.now(timezone.utc).isoformat()

        message_data = {
            "id": message_id,
            "from_agent": from_agent,
            "to_agent": to_agent,
            "message": message,
            "context": context,
            "timestamp": now,
            "status": "pending",
        }

        # Push to target agent's inbox (RPUSH for FIFO order)
        inbox_key = f"{self.INBOX_PREFIX}{to_agent}"
        self.redis.rpush(inbox_key, json.dumps(message_data))

        # Set TTL on inbox key (24h default) - prevents stale messages accumulating
        self.redis.expire(inbox_key, self.MESSAGE_TTL_SECONDS)

        # Push a lightweight notification signal so wait_for_message wakes up
        notify_key = f"{self.NOTIFY_PREFIX}{to_agent}"
        self.redis.rpush(notify_key, "1")
        self.redis.expire(notify_key, self.MESSAGE_TTL_SECONDS)

        logger.info("message_sent message_id=%s from=%s to=%s inbox_key=%s", message_id, from_agent, to_agent, inbox_key)

        return message_data

    # Backwards compat alias
    send_request = send_message

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

    def get_pending_messages(self, agent_id: str) -> list[dict]:
        """Get and consume all pending incoming messages for an agent.

        This removes the messages from the inbox (they are consumed).
        Expired messages are automatically filtered out.

        Args:
            agent_id: The agent whose inbox to check

        Returns:
            List of message dicts, oldest first (FIFO), excluding expired
        """
        inbox_key = f"{self.INBOX_PREFIX}{agent_id}"
        messages = []

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
                messages.append(message)

        logger.info("inbox_consumed agent=%s inbox_key=%s count=%d", agent_id, inbox_key, len(messages))

        return messages

    # Backwards compat alias
    get_pending_requests = get_pending_messages

    def peek_pending_messages(self, agent_id: str) -> list[dict]:
        """Peek at pending messages without consuming them.

        Used by REST API for hooks to check status.
        Expired messages are automatically filtered out.

        Args:
            agent_id: The agent whose inbox to check

        Returns:
            List of message dicts, oldest first (FIFO), excluding expired
        """
        inbox_key = f"{self.INBOX_PREFIX}{agent_id}"

        # Get all items without removing (LRANGE 0 -1 gets all)
        raw_items = self.redis.lrange(inbox_key, 0, -1)
        messages = []

        for data in raw_items:
            if isinstance(data, bytes):
                data = data.decode()
            message = json.loads(data)
            # Skip expired messages
            if not self._is_message_expired(message):
                messages.append(message)

        logger.debug("inbox_peeked agent=%s inbox_key=%s count=%d", agent_id, inbox_key, len(messages))

        return messages

    # Backwards compat alias
    peek_pending_requests = peek_pending_messages

    RESPONSES_PREFIX = "c3po:responses:"

    def _parse_message_id(self, message_id: str) -> tuple[str, str]:
        """Parse a message ID to extract the original sender and receiver.

        Message ID format: {from_agent}::{to_agent}::{uuid}

        Args:
            message_id: The message ID to parse

        Returns:
            Tuple of (from_agent, to_agent)

        Raises:
            ValueError: If message_id is invalid
        """
        parts = message_id.split(self.MESSAGE_ID_DELIMITER)
        if len(parts) != 3:
            raise ValueError(f"Invalid message_id format: {message_id}")

        from_agent, to_agent, _ = parts
        return from_agent, to_agent

    # Backwards compat alias
    _parse_request_id = _parse_message_id

    def reply(
        self,
        message_id: str,
        from_agent: str,
        response: str,
        status: str = "success",
    ) -> dict:
        """Send a reply to a previous message.

        Args:
            message_id: The ID of the original message
            from_agent: The agent sending the reply (must be the original recipient)
            response: The reply content
            status: Reply status, default "success"

        Returns:
            Reply data dict with message_id, from_agent, to_agent, response, etc.
        """
        # Parse message_id to find original sender
        original_sender, original_recipient = self._parse_message_id(message_id)

        # Authorization: only the original recipient can reply to a message
        if from_agent != original_recipient:
            raise ValueError(
                f"Agent '{from_agent}' is not the recipient of message {message_id} "
                f"(recipient is '{original_recipient}')"
            )

        now = datetime.now(timezone.utc).isoformat()

        reply_data = {
            "message_id": message_id,
            "from_agent": from_agent,
            "to_agent": original_sender,
            "response": response,
            "status": status,
            "timestamp": now,
        }

        # Push to original sender's response queue
        response_key = f"{self.RESPONSES_PREFIX}{original_sender}"
        self.redis.rpush(response_key, json.dumps(reply_data))

        logger.info("reply_sent message_id=%s from=%s to=%s status=%s", message_id, from_agent, original_sender, status)

        # Push notification so wait_for_message wakes on replies too
        notify_key = f"{self.NOTIFY_PREFIX}{original_sender}"
        self.redis.rpush(notify_key, "1")
        self.redis.expire(notify_key, self.MESSAGE_TTL_SECONDS)

        return reply_data

    def respond_to_request(
        self,
        request_id: str,
        from_agent: str,
        response: str,
        status: str = "success",
    ) -> dict:
        """Backwards compat wrapper for reply()."""
        return self.reply(request_id, from_agent, response, status)

    def wait_for_response(
        self,
        agent_id: str,
        request_id: str,
        timeout: int = 60,
    ) -> Optional[dict]:
        """Wait for a reply to a specific message.

        Uses Redis BLPOP to block until a reply arrives or timeout.

        Args:
            agent_id: The agent waiting for the reply
            request_id: The message ID to wait for
            timeout: Timeout in seconds (default 60)

        Returns:
            Reply dict if received, or None if timeout
        """
        response_key = f"{self.RESPONSES_PREFIX}{agent_id}"
        deadline = datetime.now(timezone.utc).timestamp() + timeout

        while True:
            remaining = deadline - datetime.now(timezone.utc).timestamp()
            if remaining <= 0:
                logger.info("wait_response_timeout agent=%s message_id=%s timeout=%d", agent_id, request_id, timeout)
                return None

            # BLPOP returns (key, value) or None on timeout
            # Use at least 1 second timeout (0 means wait forever in Redis)
            blpop_timeout = max(1, int(min(remaining, 10)))
            result = self.redis.blpop(response_key, timeout=blpop_timeout)

            if result is None:
                continue

            _, data = result
            if isinstance(data, bytes):
                data = data.decode()

            response = json.loads(data)

            # Check if this is the reply we're waiting for (support both old and new field names)
            resp_id = response.get("message_id") or response.get("request_id")
            if resp_id == request_id:
                logger.info("wait_response_matched agent=%s message_id=%s", agent_id, request_id)
                return response

            # Not our reply, put it back for another waiter
            # Use rpush to maintain FIFO order (append to end of queue)
            logger.debug("wait_response_putback agent=%s got_id=%s wanted=%s", agent_id, resp_id, request_id)
            self.redis.rpush(response_key, json.dumps(response))

    def wait_for_request(
        self,
        agent_id: str,
        timeout: int = 60,
    ) -> Optional[dict]:
        """Wait for notification that a message is available, without consuming it.

        Blocks on a notification channel until a signal arrives or timeout.
        The actual message remains in the inbox for get_pending_messages to consume.
        Uses a polling loop with max 10-second BLPOP intervals for HTTP health.

        Args:
            agent_id: The agent waiting for messages
            timeout: Timeout in seconds (default 60)

        Returns:
            Dict with status="ready" and pending count if notified, or None if timeout
        """
        notify_key = f"{self.NOTIFY_PREFIX}{agent_id}"
        deadline = datetime.now(timezone.utc).timestamp() + timeout

        while True:
            remaining = deadline - datetime.now(timezone.utc).timestamp()
            if remaining <= 0:
                logger.info("wait_request_timeout agent=%s timeout=%d", agent_id, timeout)
                return None

            # Use at least 1 second timeout (0 means wait forever in Redis)
            blpop_timeout = max(1, int(min(remaining, 10)))
            result = self.redis.blpop(notify_key, timeout=blpop_timeout)

            if result is not None:
                # Got a notification — peek at inbox for count
                pending = self.peek_pending_messages(agent_id)
                logger.info("wait_request_notified agent=%s pending=%d", agent_id, len(pending))
                return {"status": "ready", "pending": len(pending)}

    def _get_pending_replies(self, agent_id: str) -> list[dict]:
        """Get and consume all pending replies for an agent.

        This removes the replies from the queue (they are consumed).
        Expired messages are automatically filtered out.

        Args:
            agent_id: The agent whose response queue to check

        Returns:
            List of reply dicts, oldest first (FIFO), excluding expired
        """
        response_key = f"{self.RESPONSES_PREFIX}{agent_id}"
        replies = []

        while True:
            data = self.redis.lpop(response_key)
            if data is None:
                break

            if isinstance(data, bytes):
                data = data.decode()

            message = json.loads(data)
            if not self._is_message_expired(message):
                replies.append(message)

        logger.info("replies_consumed agent=%s count=%d", agent_id, len(replies))
        return replies

    # Backwards compat alias
    _get_pending_responses = _get_pending_replies

    def get_messages(
        self,
        agent_id: str,
        message_type: Optional[str] = None,
    ) -> list[dict]:
        """Get all pending messages (incoming and replies) for an agent.

        Consumes messages from the matching queues. Each message gets a "type"
        field ("message" or "reply") so the caller can distinguish them.

        Args:
            agent_id: The agent whose messages to retrieve
            message_type: Optional filter - "message", "reply", or None (both).
                          Also accepts legacy values "request" and "response".

        Returns:
            List of message dicts with added "type" field, oldest first
        """
        # Normalize legacy type values
        normalized_type = message_type
        if message_type == "request":
            normalized_type = "message"
        elif message_type == "response":
            normalized_type = "reply"

        messages = []

        if normalized_type is None or normalized_type == "message":
            for msg in self.get_pending_messages(agent_id):
                msg["type"] = "message"
                messages.append(msg)

        if normalized_type is None or normalized_type == "reply":
            for msg in self._get_pending_replies(agent_id):
                msg["type"] = "reply"
                messages.append(msg)

        logger.info("get_messages agent=%s type=%s count=%d", agent_id, message_type, len(messages))
        return messages

    def wait_for_message(
        self,
        agent_id: str,
        timeout: int = 60,
        message_type: Optional[str] = None,
        heartbeat_fn: Optional[callable] = None,
    ) -> Optional[list[dict]]:
        """Wait for any message (incoming or reply) to arrive, then return all pending.

        First checks for existing messages (non-blocking). If none, blocks on the
        notification channel until a signal arrives or timeout. Then drains via
        get_messages. Returns None on timeout.

        Args:
            agent_id: The agent waiting for messages
            timeout: Timeout in seconds (default 60)
            message_type: Optional filter - "message", "reply", or None (both).
                          Also accepts legacy "request" and "response".

        Returns:
            List of message dicts if any arrived, or None if timeout
        """
        # First check for existing messages without blocking
        existing = self.get_messages(agent_id, message_type)
        if existing:
            logger.info("wait_for_message immediate agent=%s count=%d", agent_id, len(existing))
            return existing

        # Block on notification channel
        notify_key = f"{self.NOTIFY_PREFIX}{agent_id}"
        deadline = datetime.now(timezone.utc).timestamp() + timeout

        while True:
            remaining = deadline - datetime.now(timezone.utc).timestamp()
            if remaining <= 0:
                logger.info("wait_for_message_timeout agent=%s timeout=%d", agent_id, timeout)
                return None

            blpop_timeout = max(1, int(min(remaining, 10)))
            result = self.redis.blpop(notify_key, timeout=blpop_timeout)

            # Refresh heartbeat so long-polling agents stay "online"
            if heartbeat_fn:
                try:
                    heartbeat_fn()
                except Exception:
                    pass  # Don't let heartbeat failures break message waiting

            if result is not None:
                # Got a notification — drain messages
                messages = self.get_messages(agent_id, message_type)
                if messages:
                    logger.info("wait_for_message_received agent=%s count=%d", agent_id, len(messages))
                    return messages
                # Stale notification (already consumed), continue blocking
