"""Audit logging for C3PO coordinator.

Provides structured JSON audit logging for security-relevant events.
Logs to both Python logger and optionally to Redis for querying.
"""

import json
import logging
from datetime import datetime, timezone
from typing import Optional

import redis

logger = logging.getLogger("c3po.audit")

# Redis audit log config
AUDIT_KEY = "c3po:audit"
AUDIT_MAX_ENTRIES = 1000  # Max entries to keep in Redis
AUDIT_TTL_SECONDS = 7 * 24 * 60 * 60  # 7 days


class AuditLogger:
    """Structured audit logger with Redis storage."""

    def __init__(self, redis_client: redis.Redis):
        self.redis = redis_client

    def _log(self, event: str, **kwargs) -> dict:
        """Log an audit event.

        Args:
            event: Event type (e.g., "auth_success", "agent_register")
            **kwargs: Event-specific data

        Returns:
            The audit entry dict
        """
        entry = {
            "event": event,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **kwargs,
        }

        # Log to Python logger
        logger.info("audit event=%s %s", event,
                     " ".join(f"{k}={v}" for k, v in kwargs.items()))

        # Store in Redis (best effort)
        try:
            serialized = json.dumps(entry)
            pipe = self.redis.pipeline()
            pipe.lpush(AUDIT_KEY, serialized)
            pipe.ltrim(AUDIT_KEY, 0, AUDIT_MAX_ENTRIES - 1)
            pipe.expire(AUDIT_KEY, AUDIT_TTL_SECONDS)
            pipe.execute()
        except Exception:
            pass  # Don't fail operations due to audit logging

        return entry

    def auth_success(self, key_id: str, agent_pattern: str, source: str = "mcp") -> dict:
        """Log successful authentication."""
        return self._log("auth_success", key_id=key_id, agent_pattern=agent_pattern, source=source)

    def auth_failure(self, reason: str, source: str = "mcp") -> dict:
        """Log failed authentication attempt."""
        return self._log("auth_failure", reason=reason, source=source)

    def agent_register(self, agent_id: str, key_id: str = "", source: str = "mcp") -> dict:
        """Log agent registration."""
        return self._log("agent_register", agent_id=agent_id, key_id=key_id, source=source)

    def agent_unregister(self, agent_id: str, key_id: str = "", source: str = "rest") -> dict:
        """Log agent unregistration."""
        return self._log("agent_unregister", agent_id=agent_id, key_id=key_id, source=source)

    def message_send(self, from_agent: str, to_agent: str, request_id: str) -> dict:
        """Log message sent."""
        return self._log("message_send", from_agent=from_agent, to_agent=to_agent, request_id=request_id)

    def message_respond(self, from_agent: str, request_id: str, status: str) -> dict:
        """Log response sent."""
        return self._log("message_respond", from_agent=from_agent, request_id=request_id, status=status)

    def message_receive(self, agent_id: str, count: int) -> dict:
        """Log messages received/consumed."""
        return self._log("message_receive", agent_id=agent_id, count=count)

    def admin_key_create(self, key_id: str, agent_pattern: str, admin_key_id: str = "admin") -> dict:
        """Log API key creation."""
        return self._log("admin_key_create", key_id=key_id, agent_pattern=agent_pattern, admin_key_id=admin_key_id)

    def admin_key_revoke(self, key_id: str, admin_key_id: str = "admin") -> dict:
        """Log API key revocation."""
        return self._log("admin_key_revoke", key_id=key_id, admin_key_id=admin_key_id)

    def authorization_denied(self, agent_id: str, key_id: str, pattern: str) -> dict:
        """Log authorization denial."""
        return self._log("authorization_denied", agent_id=agent_id, key_id=key_id, pattern=pattern)

    def get_recent(self, limit: int = 100, event_filter: Optional[str] = None) -> list[dict]:
        """Get recent audit entries from Redis.

        Args:
            limit: Max entries to return (default 100)
            event_filter: Optional event type filter

        Returns:
            List of audit entry dicts, newest first
        """
        try:
            raw_entries = self.redis.lrange(AUDIT_KEY, 0, limit * 2 if event_filter else limit - 1)
            entries = []
            for raw in raw_entries:
                if isinstance(raw, bytes):
                    raw = raw.decode()
                entry = json.loads(raw)
                if event_filter and entry.get("event") != event_filter:
                    continue
                entries.append(entry)
                if len(entries) >= limit:
                    break
            return entries
        except Exception:
            return []
