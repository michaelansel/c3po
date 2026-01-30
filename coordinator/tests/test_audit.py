"""Tests for C3PO audit logging."""

import json
import pytest
import fakeredis

from coordinator.audit import AuditLogger, AUDIT_KEY


@pytest.fixture
def redis_client():
    """Create a fresh fakeredis client for each test."""
    return fakeredis.FakeRedis()


@pytest.fixture
def audit_logger(redis_client):
    """Create AuditLogger with fakeredis."""
    return AuditLogger(redis_client)


class TestAuditLogger:
    """Tests for audit event logging."""

    def test_auth_success(self, audit_logger):
        """Should log auth success event."""
        entry = audit_logger.auth_success("key123", "machine/*")
        assert entry["event"] == "auth_success"
        assert entry["key_id"] == "key123"
        assert entry["agent_pattern"] == "machine/*"
        assert "timestamp" in entry

    def test_auth_failure(self, audit_logger):
        """Should log auth failure event."""
        entry = audit_logger.auth_failure("invalid_key", source="rest")
        assert entry["event"] == "auth_failure"
        assert entry["reason"] == "invalid_key"
        assert entry["source"] == "rest"

    def test_agent_register(self, audit_logger):
        """Should log agent registration."""
        entry = audit_logger.agent_register("machine/project", key_id="k1")
        assert entry["event"] == "agent_register"
        assert entry["agent_id"] == "machine/project"

    def test_agent_unregister(self, audit_logger):
        """Should log agent unregistration."""
        entry = audit_logger.agent_unregister("machine/project")
        assert entry["event"] == "agent_unregister"

    def test_message_send(self, audit_logger):
        """Should log message send."""
        entry = audit_logger.message_send("a", "b", "req-123")
        assert entry["event"] == "message_send"
        assert entry["from_agent"] == "a"
        assert entry["to_agent"] == "b"

    def test_message_respond(self, audit_logger):
        """Should log response."""
        entry = audit_logger.message_respond("b", "req-123", "success")
        assert entry["event"] == "message_respond"
        assert entry["status"] == "success"

    def test_admin_key_create(self, audit_logger):
        """Should log key creation."""
        entry = audit_logger.admin_key_create("kid1", "m/*")
        assert entry["event"] == "admin_key_create"
        assert entry["key_id"] == "kid1"

    def test_admin_key_revoke(self, audit_logger):
        """Should log key revocation."""
        entry = audit_logger.admin_key_revoke("kid1")
        assert entry["event"] == "admin_key_revoke"

    def test_authorization_denied(self, audit_logger):
        """Should log authorization denial."""
        entry = audit_logger.authorization_denied("m/p", "k1", "other/*")
        assert entry["event"] == "authorization_denied"


class TestAuditRedisStorage:
    """Tests for Redis storage of audit events."""

    def test_entries_stored_in_redis(self, audit_logger, redis_client):
        """Events should be stored in Redis list."""
        audit_logger.auth_success("k1", "*")
        audit_logger.auth_failure("bad")

        count = redis_client.llen(AUDIT_KEY)
        assert count == 2

    def test_get_recent(self, audit_logger):
        """Should retrieve recent entries."""
        audit_logger.auth_success("k1", "*")
        audit_logger.auth_failure("bad")

        entries = audit_logger.get_recent(limit=10)
        assert len(entries) == 2
        # Newest first (lpush)
        assert entries[0]["event"] == "auth_failure"
        assert entries[1]["event"] == "auth_success"

    def test_get_recent_with_filter(self, audit_logger):
        """Should filter by event type."""
        audit_logger.auth_success("k1", "*")
        audit_logger.auth_failure("bad")
        audit_logger.agent_register("m/p")

        entries = audit_logger.get_recent(event_filter="auth_failure")
        assert len(entries) == 1
        assert entries[0]["event"] == "auth_failure"

    def test_get_recent_limit(self, audit_logger):
        """Should respect limit parameter."""
        for i in range(10):
            audit_logger.auth_success(f"k{i}", "*")

        entries = audit_logger.get_recent(limit=3)
        assert len(entries) == 3

    def test_redis_trimming(self, audit_logger, redis_client):
        """Redis list should be trimmed to max entries."""
        from coordinator.audit import AUDIT_MAX_ENTRIES
        # Log more than max entries
        for i in range(AUDIT_MAX_ENTRIES + 50):
            audit_logger.auth_success(f"k{i}", "*")

        count = redis_client.llen(AUDIT_KEY)
        assert count <= AUDIT_MAX_ENTRIES
