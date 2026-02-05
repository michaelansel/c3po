"""Tests for C3PO authentication (AuthManager)."""

import hashlib
import json
import os
import pytest
import fakeredis

from coordinator.auth import AuthManager


class TestAuthManagerDevMode:
    """Tests when no auth tokens are configured (dev mode)."""

    def test_no_token_allows_everything(self, monkeypatch):
        """When no secrets are set, all requests pass."""
        monkeypatch.delenv("C3PO_SERVER_SECRET", raising=False)
        monkeypatch.delenv("C3PO_PROXY_BEARER_TOKEN", raising=False)
        monkeypatch.delenv("C3PO_ADMIN_KEY", raising=False)
        manager = AuthManager()
        result = manager.validate_request("")
        assert result["valid"] is True
        assert result["source"] == "no-auth"

    def test_no_token_allows_with_header(self, monkeypatch):
        """Even with an Authorization header, dev mode passes."""
        monkeypatch.delenv("C3PO_SERVER_SECRET", raising=False)
        monkeypatch.delenv("C3PO_PROXY_BEARER_TOKEN", raising=False)
        monkeypatch.delenv("C3PO_ADMIN_KEY", raising=False)
        manager = AuthManager()
        result = manager.validate_request("Bearer some-token")
        assert result["valid"] is True
        assert result["source"] == "no-auth"

    def test_auth_enabled_false(self, monkeypatch):
        """auth_enabled should be False when no token configured."""
        monkeypatch.delenv("C3PO_SERVER_SECRET", raising=False)
        monkeypatch.delenv("C3PO_PROXY_BEARER_TOKEN", raising=False)
        monkeypatch.delenv("C3PO_ADMIN_KEY", raising=False)
        manager = AuthManager()
        assert manager.auth_enabled is False


class TestAuthManagerProxyToken:
    """Tests for proxy token validation (/oauth paths)."""

    @pytest.fixture(autouse=True)
    def setup_token(self, monkeypatch):
        monkeypatch.setenv("C3PO_PROXY_BEARER_TOKEN", "test-proxy-token")
        monkeypatch.delenv("C3PO_SERVER_SECRET", raising=False)
        monkeypatch.delenv("C3PO_ADMIN_KEY", raising=False)

    def test_valid_token(self):
        """Correct proxy token passes on /oauth path."""
        manager = AuthManager()
        result = manager.validate_request("Bearer test-proxy-token", "/oauth")
        assert result["valid"] is True
        assert result["source"] == "proxy"

    def test_invalid_token(self):
        """Wrong proxy token is rejected."""
        manager = AuthManager()
        result = manager.validate_request("Bearer wrong-token", "/oauth")
        assert result["valid"] is False
        assert "Invalid proxy token" in result["error"]

    def test_missing_header(self):
        """Missing Authorization header is rejected."""
        manager = AuthManager()
        result = manager.validate_request("", "/oauth")
        assert result["valid"] is False
        assert "Missing" in result["error"]

    def test_malformed_header_no_bearer(self):
        """Non-Bearer auth scheme is rejected."""
        manager = AuthManager()
        result = manager.validate_request("Basic abc123", "/oauth")
        assert result["valid"] is False
        assert "Invalid" in result["error"]

    def test_malformed_header_no_space(self):
        """Single word (no space) is rejected."""
        manager = AuthManager()
        result = manager.validate_request("BearerToken", "/oauth")
        assert result["valid"] is False
        assert "Invalid" in result["error"]

    def test_auth_enabled_true(self):
        """auth_enabled should be True when token configured."""
        manager = AuthManager()
        assert manager.auth_enabled is True

    def test_case_insensitive_bearer_prefix(self):
        """Bearer prefix should be case-insensitive."""
        manager = AuthManager()
        result = manager.validate_request("bearer test-proxy-token", "/oauth")
        assert result["valid"] is True

    def test_legacy_fallback_uses_proxy_token(self):
        """Empty path_prefix falls back to proxy token validation."""
        manager = AuthManager()
        result = manager.validate_request("Bearer test-proxy-token", "")
        assert result["valid"] is True
        assert result["source"] == "proxy"


class TestAuthManagerAdminKey:
    """Tests for admin key validation (/admin paths)."""

    @pytest.fixture(autouse=True)
    def setup_admin_key(self, monkeypatch):
        monkeypatch.setenv("C3PO_ADMIN_KEY", "test-admin-key")
        monkeypatch.setenv("C3PO_SERVER_SECRET", "test-server-secret")
        monkeypatch.delenv("C3PO_PROXY_BEARER_TOKEN", raising=False)

    def test_valid_admin_key_composite(self):
        """Correct server_secret.admin_key passes on /admin path."""
        manager = AuthManager()
        result = manager.validate_request("Bearer test-server-secret.test-admin-key", "/admin")
        assert result["valid"] is True
        assert result["source"] == "admin"

    def test_invalid_admin_key(self):
        """Wrong admin key is rejected."""
        manager = AuthManager()
        result = manager.validate_request("Bearer test-server-secret.wrong-key", "/admin")
        assert result["valid"] is False
        assert "Invalid admin key" in result["error"]

    def test_bare_admin_key_rejected_with_server_secret(self):
        """Bare admin_key (without server_secret prefix) is rejected when server_secret is set."""
        manager = AuthManager()
        result = manager.validate_request("Bearer test-admin-key", "/admin")
        assert result["valid"] is False
        assert "Invalid admin key" in result["error"]

    def test_bare_admin_key_rejected_without_server_secret(self, monkeypatch):
        """Bare admin_key is rejected when server_secret is not set (server_secret required)."""
        monkeypatch.delenv("C3PO_SERVER_SECRET", raising=False)
        manager = AuthManager()
        result = manager.validate_request("Bearer test-admin-key", "/admin")
        assert result["valid"] is False
        assert "Server secret not configured" in result["error"]

    def test_invalid_admin_key_composite_bad_secret(self):
        """Wrong server_secret in composite admin token is rejected."""
        manager = AuthManager()
        result = manager.validate_request("Bearer wrong-secret.test-admin-key", "/admin")
        assert result["valid"] is False

    def test_invalid_admin_key_composite_bad_key(self):
        """Wrong admin_key in composite admin token is rejected."""
        manager = AuthManager()
        result = manager.validate_request("Bearer test-server-secret.wrong-key", "/admin")
        assert result["valid"] is False

    def test_no_dot_in_token_rejected(self):
        """Token without dot separator is rejected with format error."""
        manager = AuthManager()
        result = manager.validate_request("Bearer nodothere", "/admin")
        assert result["valid"] is False
        assert "Invalid admin key format" in result["error"]


class TestAuthManagerApiKey:
    """Tests for API key validation (/agent paths)."""

    @pytest.fixture(autouse=True)
    def setup_api_key(self, monkeypatch):
        monkeypatch.setenv("C3PO_SERVER_SECRET", "test-server-secret")
        monkeypatch.delenv("C3PO_PROXY_BEARER_TOKEN", raising=False)
        monkeypatch.delenv("C3PO_ADMIN_KEY", raising=False)

    @pytest.fixture
    def redis_client(self):
        return fakeredis.FakeRedis()

    def test_valid_api_key(self, redis_client):
        """Valid server_secret.api_key token passes (composite token from create_api_key)."""
        manager = AuthManager(redis_client)
        # Create a key first — returns composite token (server_secret.raw_key)
        key_data = manager.create_api_key(agent_pattern="macbook/*", description="test")
        composite_key = key_data["api_key"]

        # The composite key already includes server_secret prefix
        result = manager.validate_request(f"Bearer {composite_key}", "/agent")
        assert result["valid"] is True
        assert result["source"] == "api_key"
        assert result["agent_pattern"] == "macbook/*"
        assert result["key_id"] == key_data["key_id"]

    def test_invalid_server_secret(self, redis_client):
        """Wrong server secret is rejected."""
        manager = AuthManager(redis_client)
        result = manager.validate_request("Bearer wrong-secret.some-key", "/agent")
        assert result["valid"] is False
        assert "Invalid server secret" in result["error"]

    def test_invalid_api_key(self, redis_client):
        """Unknown API key is rejected."""
        manager = AuthManager(redis_client)
        result = manager.validate_request("Bearer test-server-secret.nonexistent-key", "/agent")
        assert result["valid"] is False
        assert "Invalid API key" in result["error"]

    def test_missing_api_key_after_dot(self, redis_client):
        """Token with dot but no API key portion is rejected."""
        manager = AuthManager(redis_client)
        result = manager.validate_request("Bearer test-server-secret.", "/agent")
        assert result["valid"] is False
        assert "Missing API key" in result["error"]

    def test_no_dot_in_token(self, redis_client):
        """Token without dot separator is rejected."""
        manager = AuthManager(redis_client)
        result = manager.validate_request("Bearer no-dot-here", "/agent")
        assert result["valid"] is False
        assert "Invalid API key format" in result["error"]

    def test_no_redis_rejects(self):
        """API key validation fails gracefully without Redis."""
        manager = AuthManager(None)
        result = manager.validate_request("Bearer test-server-secret.some-key", "/agent")
        assert result["valid"] is False
        assert "Redis not available" in result["error"]


class TestApiKeyManagement:
    """Tests for API key CRUD operations."""

    @pytest.fixture
    def redis_client(self):
        return fakeredis.FakeRedis()

    @pytest.fixture
    def manager(self, redis_client, monkeypatch):
        monkeypatch.setenv("C3PO_SERVER_SECRET", "test-secret")
        monkeypatch.delenv("C3PO_PROXY_BEARER_TOKEN", raising=False)
        monkeypatch.delenv("C3PO_ADMIN_KEY", raising=False)
        return AuthManager(redis_client)

    def test_create_api_key(self, manager):
        """create_api_key returns key_id, composite api_key, pattern, and timestamps."""
        result = manager.create_api_key(agent_pattern="laptop/*", description="My laptop")
        assert "key_id" in result
        assert "api_key" in result
        assert result["agent_pattern"] == "laptop/*"
        assert result["description"] == "My laptop"
        assert "created_at" in result
        # Composite token should contain a dot (server_secret.raw_key)
        assert "." in result["api_key"]

    def test_create_api_key_stores_bcrypt_hash(self, manager, redis_client):
        """create_api_key stores bcrypt hash in Redis metadata."""
        result = manager.create_api_key(agent_pattern="test/*")
        # Check Redis storage has bcrypt_hash
        raw_key = result["api_key"].split(".", 1)[1]  # Extract raw key from composite
        key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
        raw = redis_client.hget(AuthManager.API_KEYS_HASH, key_hash)
        metadata = json.loads(raw.decode())
        assert "bcrypt_hash" in metadata
        assert metadata["bcrypt_hash"].startswith("$2")

    def test_list_api_keys(self, manager):
        """list_api_keys returns metadata without secrets."""
        manager.create_api_key(agent_pattern="a/*")
        manager.create_api_key(agent_pattern="b/*")
        keys = manager.list_api_keys()
        assert len(keys) == 2
        # Should not contain plaintext api_key
        for k in keys:
            assert "api_key" not in k
            assert "key_id" in k
            assert "agent_pattern" in k

    def test_revoke_api_key(self, manager):
        """revoke_api_key removes the key."""
        key_data = manager.create_api_key(agent_pattern="test/*")
        assert manager.revoke_api_key(key_data["key_id"]) is True
        # Key should be gone
        keys = manager.list_api_keys()
        assert len(keys) == 0

    def test_revoke_nonexistent_key(self, manager):
        """revoke_api_key returns False for unknown key_id."""
        assert manager.revoke_api_key("nonexistent") is False

    def test_revoked_key_cannot_authenticate(self, manager):
        """After revocation, the API key should fail authentication."""
        key_data = manager.create_api_key(agent_pattern="test/*")
        composite_key = key_data["api_key"]
        manager.revoke_api_key(key_data["key_id"])

        result = manager.validate_request(f"Bearer {composite_key}", "/agent")
        assert result["valid"] is False


class TestAgentPatternValidation:
    """Tests for validate_agent_pattern static method."""

    def test_wildcard_matches_all(self):
        assert AuthManager.validate_agent_pattern("anything/here", "*") is True

    def test_prefix_pattern(self):
        assert AuthManager.validate_agent_pattern("macbook/project", "macbook/*") is True
        assert AuthManager.validate_agent_pattern("server/project", "macbook/*") is False

    def test_exact_match(self):
        assert AuthManager.validate_agent_pattern("macbook/project", "macbook/project") is True
        assert AuthManager.validate_agent_pattern("macbook/other", "macbook/project") is False

    def test_complex_pattern(self):
        assert AuthManager.validate_agent_pattern("server/homelab-1", "server/homelab-*") is True
        assert AuthManager.validate_agent_pattern("server/prod-1", "server/homelab-*") is False


class TestPublicPathAuth:
    """Tests that /api/health path requires no auth."""

    def test_public_path_always_passes(self, monkeypatch):
        """Public /api path passes even when auth is enabled."""
        monkeypatch.setenv("C3PO_PROXY_BEARER_TOKEN", "some-token")
        manager = AuthManager()
        result = manager.validate_request("", "/api")
        assert result["valid"] is True
        assert result["source"] == "public"


class TestApiKeyValidationCache:
    """Tests for bcrypt result caching in _validate_api_key."""

    @pytest.fixture(autouse=True)
    def setup_api_key(self, monkeypatch):
        monkeypatch.setenv("C3PO_SERVER_SECRET", "test-server-secret")
        monkeypatch.delenv("C3PO_PROXY_BEARER_TOKEN", raising=False)
        monkeypatch.delenv("C3PO_ADMIN_KEY", raising=False)

    @pytest.fixture
    def redis_client(self):
        return fakeredis.FakeRedis()

    def test_api_key_validation_caches_bcrypt_result(self, redis_client, monkeypatch):
        """Second validation should hit cache and not call bcrypt.checkpw again."""
        import bcrypt as bcrypt_module
        manager = AuthManager(redis_client)
        key_data = manager.create_api_key(agent_pattern="test/*")
        composite_key = key_data["api_key"]

        # Track bcrypt.checkpw calls
        original_checkpw = bcrypt_module.checkpw
        call_count = 0

        def counting_checkpw(password, hashed):
            nonlocal call_count
            call_count += 1
            return original_checkpw(password, hashed)

        monkeypatch.setattr(bcrypt_module, "checkpw", counting_checkpw)

        # First call - should use bcrypt
        result1 = manager.validate_request(f"Bearer {composite_key}", "/agent")
        assert result1["valid"] is True
        assert call_count == 1

        # Second call - should hit cache, not call bcrypt
        result2 = manager.validate_request(f"Bearer {composite_key}", "/agent")
        assert result2["valid"] is True
        assert call_count == 1  # Still 1, not 2

    def test_api_key_cache_expires(self, redis_client, monkeypatch):
        """After TTL expires, bcrypt should be called again."""
        import bcrypt as bcrypt_module
        import time as time_module
        manager = AuthManager(redis_client)
        key_data = manager.create_api_key(agent_pattern="test/*")
        composite_key = key_data["api_key"]

        # Track bcrypt.checkpw calls
        original_checkpw = bcrypt_module.checkpw
        call_count = 0

        def counting_checkpw(password, hashed):
            nonlocal call_count
            call_count += 1
            return original_checkpw(password, hashed)

        monkeypatch.setattr(bcrypt_module, "checkpw", counting_checkpw)

        # First call
        result1 = manager.validate_request(f"Bearer {composite_key}", "/agent")
        assert result1["valid"] is True
        assert call_count == 1

        # Simulate time passing beyond TTL by manipulating the cache entry
        for key_hash in manager._auth_cache:
            _, auth_result = manager._auth_cache[key_hash]
            # Set expiry to the past
            manager._auth_cache[key_hash] = (time_module.time() - 1, auth_result)

        # Third call - cache expired, should call bcrypt again
        result3 = manager.validate_request(f"Bearer {composite_key}", "/agent")
        assert result3["valid"] is True
        assert call_count == 2

    def test_api_key_cache_invalidated_on_revoke(self, redis_client):
        """Revoking a key should invalidate its cache entry."""
        manager = AuthManager(redis_client)
        key_data = manager.create_api_key(agent_pattern="test/*")
        composite_key = key_data["api_key"]

        # Validate to populate cache
        result1 = manager.validate_request(f"Bearer {composite_key}", "/agent")
        assert result1["valid"] is True

        # Revoke the key
        manager.revoke_api_key(key_data["key_id"])

        # Validate again - should fail (key gone from Redis, cache should be invalidated)
        result2 = manager.validate_request(f"Bearer {composite_key}", "/agent")
        assert result2["valid"] is False

    def test_failed_bcrypt_not_cached(self, redis_client, monkeypatch):
        """Failed bcrypt validation should not be cached."""
        import bcrypt as bcrypt_module
        manager = AuthManager(redis_client)

        # Create a key and store it with a known hash
        key_data = manager.create_api_key(agent_pattern="test/*")
        correct_key = key_data["api_key"]

        # Track bcrypt.checkpw calls
        original_checkpw = bcrypt_module.checkpw
        call_count = 0

        def counting_checkpw(password, hashed):
            nonlocal call_count
            call_count += 1
            return original_checkpw(password, hashed)

        monkeypatch.setattr(bcrypt_module, "checkpw", counting_checkpw)

        # Try with wrong key - should not be cached
        wrong_key = "test-server-secret.wrong-key-that-doesnt-exist"
        result1 = manager.validate_request(f"Bearer {wrong_key}", "/agent")
        assert result1["valid"] is False
        # bcrypt not called because key not found in Redis
        assert call_count == 0

        # Try with correct key - should call bcrypt
        result2 = manager.validate_request(f"Bearer {correct_key}", "/agent")
        assert result2["valid"] is True
        assert call_count == 1
