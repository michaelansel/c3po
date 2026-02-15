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
    ACKED_PREFIX = "c3po:acked:"

    # Configuration
    RATE_LIMIT_REQUESTS = 10  # Max requests per window
    RATE_LIMIT_WINDOW_SECONDS = 60  # Window size in seconds
    MESSAGE_TTL_SECONDS = 24 * 60 * 60  # 24 hours

    # Lua script for atomic list compaction.
    # Removes acked entries from a Redis list without a race window.
    # KEYS[1] = list key
    # ARGV[1] = JSON array of acked IDs
    # ARGV[2] = id_field name (e.g. "id")
    # ARGV[3] = fallback_field name (or "" if none)
    # ARGV[4] = TTL in seconds
    _COMPACT_SCRIPT = """\
local acked = cjson.decode(ARGV[1])
local id_field = ARGV[2]
local fallback = ARGV[3]
local ttl = tonumber(ARGV[4])

local acked_set = {}
for _, id in ipairs(acked) do
    acked_set[id] = true
end

local items = redis.call('LRANGE', KEYS[1], 0, -1)
if #items == 0 then return 0 end

redis.call('DEL', KEYS[1])

local kept = 0
for _, raw in ipairs(items) do
    local ok, msg = pcall(cjson.decode, raw)
    if ok then
        local msg_id = msg[id_field]
        if (not msg_id or msg_id == cjson.null) and fallback ~= '' then
            msg_id = msg[fallback]
        end
        if not msg_id or msg_id == cjson.null or not acked_set[msg_id] then
            redis.call('RPUSH', KEYS[1], raw)
            kept = kept + 1
        end
    else
        redis.call('RPUSH', KEYS[1], raw)
        kept = kept + 1
    end
end

if kept > 0 then
    redis.call('EXPIRE', KEYS[1], ttl)
end

return kept
"""

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

        This is a thin wrapper around send_message() that adds reply_to field.

        Args:
            message_id: The ID of the original message
            from_agent: The agent sending the reply
            response: The reply content
            status: Reply status, default "success"

        Returns:
            Message data dict with id, from_agent, to_agent, message, etc.
        """
        # Parse message_id to find original sender and recipient
        original_sender, original_recipient = self._parse_message_id(message_id)

        # Authorization: only the original recipient can reply to a message
        if from_agent != original_recipient:
            raise ValueError(
                f"Agent '{from_agent}' is not the recipient of message {message_id} "
                f"(recipient is '{original_recipient}')"
            )

        # Generate unique ID for this reply (like send_message does)
        reply_id = self._generate_message_id(from_agent, original_sender)

        # Create reply message with same structure as send_message
        # Use 'message' field for consistency with send_message()
        reply_data = {
            "id": reply_id,
            "reply_to": message_id,  # Points to original message
            "from_agent": from_agent,
            "to_agent": original_sender,
            "message": response,  # Response content in message field (consistent with send_message)
            "context": "",  # Empty context
            "status": status,  # Status field
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        # Push to original sender's single messages queue
        messages_key = f"{self.INBOX_PREFIX}{original_sender}"
        self.redis.rpush(messages_key, json.dumps(reply_data))
        self.redis.expire(messages_key, self.MESSAGE_TTL_SECONDS)

        # Push notification so wait_for_message wakes on replies too
        notify_key = f"{self.NOTIFY_PREFIX}{original_sender}"
        self.redis.rpush(notify_key, "1")
        self.redis.expire(notify_key, self.MESSAGE_TTL_SECONDS)

        logger.info("reply_sent message_id=%s from=%s to=%s status=%s", message_id, from_agent, original_sender, status)

        return reply_data

    def _get_acked_ids(self, agent_id: str) -> set[str]:
        """Get the set of acked message/reply IDs for an agent.

        Args:
            agent_id: The agent whose acked set to check

        Returns:
            Set of acked ID strings
        """
        acked_key = f"{self.ACKED_PREFIX}{agent_id}"
        raw = self.redis.smembers(acked_key)
        return {(v.decode() if isinstance(v, bytes) else v) for v in raw}

    def _get_message_id(self, msg: dict) -> Optional[str]:
        """Extract the unique ID from a message or reply dict.

        For replies, the ID is in the "reply_id" field.
        For messages, the ID is in the "id" field.

        Args:
            msg: Message or reply dict

        Returns:
            The unique ID string, or None if not found
        """
        # Check for reply_id first (replies)
        if "reply_id" in msg:
            return msg.get("reply_id")
        # Fallback to id (messages)
        return msg.get("id")

    def peek_messages(
        self,
        agent_id: str,
    ) -> list[dict]:
        """Non-destructive read of all pending messages and replies.

        Filters out acked IDs and expired messages. Messages stay in Redis
        until explicitly acked via ack_messages().

        Args:
            agent_id: The agent whose messages to retrieve

        Returns:
            List of message dicts, oldest first
        """
        acked_ids = self._get_acked_ids(agent_id)

        messages = []
        for msg in self.peek_pending_messages(agent_id):
            msg_id = self._get_message_id(msg)
            if msg_id and msg_id in acked_ids:
                continue
            messages.append(msg)

        logger.info("peek_messages agent=%s count=%d acked=%d",
                     agent_id, len(messages), len(acked_ids))
        return messages

    # Compaction threshold: when acked set exceeds this, compact the lists
    COMPACT_THRESHOLD = 20

    def ack_messages(self, agent_id: str, message_ids: list[str]) -> dict:
        """Acknowledge messages so they no longer appear in peek results.

        Adds IDs to the acked set. When the set grows large, triggers
        compaction to remove acked entries from the underlying Redis lists.

        Args:
            agent_id: The agent acknowledging messages
            message_ids: List of message/reply IDs to acknowledge

        Returns:
            Dict with acked count and compaction status
        """
        if not message_ids:
            return {"acked": 0, "compacted": False}

        acked_key = f"{self.ACKED_PREFIX}{agent_id}"
        self.redis.sadd(acked_key, *message_ids)
        self.redis.expire(acked_key, self.MESSAGE_TTL_SECONDS)

        logger.info("ack_messages agent=%s ids=%s", agent_id, message_ids)

        # Check if compaction is needed
        acked_count = self.redis.scard(acked_key)
        compacted = False
        if acked_count > self.COMPACT_THRESHOLD:
            self._compact_queues(agent_id)
            compacted = True

        return {"acked": len(message_ids), "compacted": compacted}

    def _compact_queues(self, agent_id: str) -> None:
        """Remove acked entries from single messages queue.

        Reads acked IDs, filters them from the queue, then clears
        the acked set.

        Args:
            agent_id: The agent whose queue to compact
        """
        acked_ids = self._get_acked_ids(agent_id)
        if not acked_ids:
            return

        messages_key = f"{self.INBOX_PREFIX}{agent_id}"
        self._compact_list(messages_key, acked_ids, "reply_id", fallback_field="id")

        # Clear the acked set after compaction
        acked_key = f"{self.ACKED_PREFIX}{agent_id}"
        self.redis.delete(acked_key)

        logger.info("compacted agent=%s removed=%d", agent_id, len(acked_ids))

    def _compact_list(
        self,
        key: str,
        acked_ids: set[str],
        id_field: str,
        fallback_field: Optional[str] = None,
    ) -> None:
        """Remove acked entries from a single Redis list.

        Uses a Lua script for atomicity — no messages can be lost between
        reading the list and rewriting it.

        Args:
            key: Redis list key
            acked_ids: Set of IDs to remove
            id_field: Primary field name to check for ID
            fallback_field: Optional fallback field for ID lookup
        """
        self.redis.eval(
            self._COMPACT_SCRIPT,
            1,
            key,
            json.dumps(list(acked_ids)),
            id_field,
            fallback_field or "",
            str(self.MESSAGE_TTL_SECONDS),
        )

    def get_messages(
        self,
        agent_id: str,
    ) -> list[dict]:
        """Get all pending messages (incoming and replies) for an agent.

        Non-destructive: messages remain in Redis until acked via ack_messages().
        Repeated calls may return the same messages.

        Args:
            agent_id: The agent whose messages to retrieve

        Returns:
            List of message dicts, oldest first
        """
        return self.peek_messages(agent_id)

    def wait_for_message(
        self,
        agent_id: str,
        timeout: int = 60,
        heartbeat_fn: Optional[callable] = None,
        shutdown_event=None,
    ):
        """Wait for any message (incoming or reply) to arrive, then return all pending.

        Non-destructive: messages remain in Redis until acked via ack_messages().
        Repeated calls may return the same messages.

        First checks for existing messages (non-blocking). If none, blocks on the
        notification channel until a signal arrives or timeout. Checks inbox on
        every loop iteration (not just on notification) to handle lost notifications.

        Args:
            agent_id: The agent waiting for messages
            timeout: Timeout in seconds (default 60)
            heartbeat_fn: Optional callable to refresh heartbeat
            shutdown_event: Optional threading.Event; if set, return "shutdown" sentinel.

        Returns:
            List of message dicts if any arrived, None if timeout,
            or the string "shutdown" if shutdown_event is set.
        """
        # First check for existing messages without blocking
        existing = self.peek_messages(agent_id)
        if existing:
            logger.info("wait_for_message immediate agent=%s count=%d", agent_id, len(existing))
            return existing

        # Block on notification channel
        notify_key = f"{self.NOTIFY_PREFIX}{agent_id}"
        deadline = datetime.now(timezone.utc).timestamp() + timeout

        cycle = 0
        while True:
            remaining = deadline - datetime.now(timezone.utc).timestamp()
            if remaining <= 0:
                logger.info("wait_for_message_timeout agent=%s timeout=%d cycles=%d", agent_id, timeout, cycle)
                return None

            blpop_timeout = max(1, int(min(remaining, 10)))
            result = self.redis.blpop(notify_key, timeout=blpop_timeout)
            cycle += 1

            if cycle % 6 == 0:  # Every ~60s
                logger.info("wait_for_message_alive agent=%s cycle=%d remaining=%.0fs notified=%s",
                           agent_id, cycle, remaining, result is not None)

            # Check for graceful shutdown
            if shutdown_event and shutdown_event.is_set():
                logger.info("wait_for_message_shutdown agent=%s cycles=%d", agent_id, cycle)
                return "shutdown"

            # Refresh heartbeat so long-polling agents stay "online"
            if heartbeat_fn:
                try:
                    heartbeat_fn()
                except Exception:
                    pass  # Don't let heartbeat failures break message waiting

            # Check inbox on every iteration (notification or timeout)
            # This fixes the 10s polling bug: messages with lost notifications
            # are found within one BLPOP cycle instead of waiting forever.
            messages = self.peek_messages(agent_id)
            if messages:
                logger.info("wait_for_message_received agent=%s count=%d notified=%s cycles=%d",
                           agent_id, len(messages), result is not None, cycle)
                return messages
