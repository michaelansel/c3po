"""Tests for C3PO authentication and authorization."""

import os
import pytest
import fakeredis

from coordinator.auth import AuthManager


@pytest.fixture
def redis_client():
    """Create a fresh fakeredis client for each test."""
    return fakeredis.FakeRedis()


@pytest.fixture
def auth_manager(redis_client):
    """Create AuthManager with fakeredis."""
    return AuthManager(redis_client)


@pytest.fixture
def auth_manager_with_secret(redis_client, monkeypatch):
    """Create AuthManager with server secret configured."""
    monkeypatch.setenv("C3PO_SERVER_SECRET", "test-secret")
    monkeypatch.setenv("C3PO_ADMIN_KEY", "test-admin-key")
    return AuthManager(redis_client)


class TestTokenParsing:
    """Tests for bearer token parsing."""

    def test_parse_valid_token(self, auth_manager):
        """Should parse 'Bearer secret.key' correctly."""
        secret, key = auth_manager.parse_bearer_token("Bearer mysecret.mykey")
        assert secret == "mysecret"
        assert key == "mykey"

    def test_parse_token_with_dots_in_key(self, auth_manager):
        """Key part can contain dots (split on first only)."""
        secret, key = auth_manager.parse_bearer_token("Bearer sec.key.with.dots")
        assert secret == "sec"
        assert key == "key.with.dots"

    def test_parse_missing_header(self, auth_manager):
        """Missing header raises ValueError."""
        with pytest.raises(ValueError, match="Missing"):
            auth_manager.parse_bearer_token("")

    def test_parse_no_bearer_prefix(self, auth_manager):
        """Token without Bearer prefix raises ValueError."""
        with pytest.raises(ValueError, match="Invalid Authorization"):
            auth_manager.parse_bearer_token("Basic abc123")

    def test_parse_no_dot(self, auth_manager):
        """Token without dot separator raises ValueError."""
        with pytest.raises(ValueError, match="expected"):
            auth_manager.parse_bearer_token("Bearer nodottoken")

    def test_parse_empty_secret(self, auth_manager):
        """Empty secret part raises ValueError."""
        with pytest.raises(ValueError, match="non-empty"):
            auth_manager.parse_bearer_token("Bearer .keyonly")

    def test_parse_empty_key(self, auth_manager):
        """Empty key part raises ValueError."""
        with pytest.raises(ValueError, match="non-empty"):
            auth_manager.parse_bearer_token("Bearer secretonly.")


class TestServerSecret:
    """Tests for server secret validation."""

    def test_no_secret_configured(self, auth_manager):
        """When no server secret is configured, all pass."""
        assert auth_manager.validate_server_secret("anything") is True

    def test_valid_secret(self, auth_manager_with_secret):
        """Correct server secret passes."""
        assert auth_manager_with_secret.validate_server_secret("test-secret") is True

    def test_invalid_secret(self, auth_manager_with_secret):
        """Wrong server secret fails."""
        assert auth_manager_with_secret.validate_server_secret("wrong-secret") is False


class TestKeyGeneration:
    """Tests for API key generation and validation."""

    def test_generate_key(self, auth_manager):
        """Should generate a key and store metadata."""
        raw_key, metadata = auth_manager.generate_key("machine/*", "test key")
        assert raw_key  # Non-empty
        assert metadata["agent_pattern"] == "machine/*"
        assert metadata["description"] == "test key"
        assert metadata["key_id"]  # Non-empty

    def test_validate_generated_key(self, auth_manager):
        """Generated key should validate successfully."""
        raw_key, _ = auth_manager.generate_key("machine/*")
        result = auth_manager.validate_key(raw_key)
        assert result is not None
        assert result["agent_pattern"] == "machine/*"

    def test_validate_invalid_key(self, auth_manager):
        """Invalid key returns None."""
        result = auth_manager.validate_key("not-a-real-key")
        assert result is None

    def test_validate_updates_last_used(self, auth_manager):
        """Validation updates last_used timestamp."""
        raw_key, metadata = auth_manager.generate_key("*")
        original_time = metadata["last_used"]
        # Validate again
        result = auth_manager.validate_key(raw_key)
        assert result["last_used"] >= original_time

    def test_revoke_key(self, auth_manager):
        """Revoked key should no longer validate."""
        raw_key, metadata = auth_manager.generate_key("*")
        key_id = metadata["key_id"]

        assert auth_manager.revoke_key(key_id) is True
        assert auth_manager.validate_key(raw_key) is None

    def test_revoke_nonexistent_key(self, auth_manager):
        """Revoking nonexistent key returns False."""
        assert auth_manager.revoke_key("nonexistent") is False

    def test_list_keys(self, auth_manager):
        """Should list all keys metadata."""
        auth_manager.generate_key("a/*", "key a")
        auth_manager.generate_key("b/*", "key b")

        keys = auth_manager.list_keys()
        assert len(keys) == 2
        patterns = {k["agent_pattern"] for k in keys}
        assert patterns == {"a/*", "b/*"}


class TestBearerTokenValidation:
    """Tests for full bearer token validation (both layers)."""

    def test_valid_token(self, auth_manager_with_secret):
        """Full valid token authenticates successfully."""
        raw_key, _ = auth_manager_with_secret.generate_key("machine/*")
        result = auth_manager_with_secret.validate_bearer_token(
            f"Bearer test-secret.{raw_key}"
        )
        assert result["valid"] is True
        assert result["is_admin"] is False
        assert result["agent_pattern"] == "machine/*"

    def test_invalid_server_secret(self, auth_manager_with_secret):
        """Wrong server secret fails before checking api_key."""
        raw_key, _ = auth_manager_with_secret.generate_key("*")
        result = auth_manager_with_secret.validate_bearer_token(
            f"Bearer wrong-secret.{raw_key}"
        )
        assert result["valid"] is False
        assert "server secret" in result["message"].lower()

    def test_invalid_api_key(self, auth_manager_with_secret):
        """Valid server secret but invalid api_key fails."""
        result = auth_manager_with_secret.validate_bearer_token(
            "Bearer test-secret.invalid-key"
        )
        assert result["valid"] is False
        assert "api key" in result["message"].lower()

    def test_admin_key(self, auth_manager_with_secret):
        """Admin key bypasses normal key lookup."""
        result = auth_manager_with_secret.validate_bearer_token(
            "Bearer test-secret.test-admin-key"
        )
        assert result["valid"] is True
        assert result["is_admin"] is True
        assert result["agent_pattern"] == "*"

    def test_missing_token(self, auth_manager_with_secret):
        """Missing token returns unauthorized."""
        result = auth_manager_with_secret.validate_bearer_token("")
        assert result["valid"] is False

    def test_malformed_token(self, auth_manager_with_secret):
        """Malformed token returns unauthorized."""
        result = auth_manager_with_secret.validate_bearer_token("not-a-token")
        assert result["valid"] is False


class TestAgentAuthorization:
    """Tests for agent pattern matching."""

    def test_exact_match(self, auth_manager_with_secret):
        """Exact agent pattern matches only that ID."""
        raw_key, _ = auth_manager_with_secret.generate_key("machine/project")
        result = auth_manager_with_secret.validate_bearer_token(f"Bearer test-secret.{raw_key}")

        assert auth_manager_with_secret.check_agent_authorization(result, "machine/project") is True
        assert auth_manager_with_secret.check_agent_authorization(result, "machine/other") is False

    def test_glob_pattern(self, auth_manager_with_secret):
        """Glob pattern matches matching IDs."""
        raw_key, _ = auth_manager_with_secret.generate_key("machine/*")
        result = auth_manager_with_secret.validate_bearer_token(f"Bearer test-secret.{raw_key}")

        assert auth_manager_with_secret.check_agent_authorization(result, "machine/project") is True
        assert auth_manager_with_secret.check_agent_authorization(result, "machine/other") is True
        assert auth_manager_with_secret.check_agent_authorization(result, "other/project") is False

    def test_wildcard_all(self, auth_manager_with_secret):
        """Wildcard '*' matches everything."""
        raw_key, _ = auth_manager_with_secret.generate_key("*")
        result = auth_manager_with_secret.validate_bearer_token(f"Bearer test-secret.{raw_key}")

        assert auth_manager_with_secret.check_agent_authorization(result, "anything/goes") is True

    def test_admin_always_authorized(self, auth_manager_with_secret):
        """Admin key is always authorized."""
        result = auth_manager_with_secret.validate_bearer_token(
            "Bearer test-secret.test-admin-key"
        )
        assert auth_manager_with_secret.check_agent_authorization(result, "any/agent") is True

    def test_invalid_auth_denied(self, auth_manager):
        """Invalid auth result always denied."""
        result = {"valid": False}
        assert auth_manager.check_agent_authorization(result, "any/agent") is False


class TestFullBearerToken:
    """Tests for full bearer token construction."""

    def test_get_full_bearer_token(self, auth_manager_with_secret):
        """Full token combines server secret and api key."""
        raw_key, _ = auth_manager_with_secret.generate_key("*")
        full = auth_manager_with_secret.get_full_bearer_token(raw_key)
        assert full == f"test-secret.{raw_key}"

        # Should validate correctly
        result = auth_manager_with_secret.validate_bearer_token(f"Bearer {full}")
        assert result["valid"] is True
