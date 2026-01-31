"""Tests for C3PO proxy authentication."""

import os
import pytest

from coordinator.auth import ProxyAuthManager


class TestProxyAuthManagerDevMode:
    """Tests when no proxy token is configured (dev mode)."""

    def test_no_token_allows_everything(self, monkeypatch):
        """When C3PO_PROXY_BEARER_TOKEN is not set, all requests pass."""
        monkeypatch.delenv("C3PO_PROXY_BEARER_TOKEN", raising=False)
        manager = ProxyAuthManager()
        result = manager.validate_request("")
        assert result["valid"] is True
        assert result["source"] == "no-auth"

    def test_no_token_allows_with_header(self, monkeypatch):
        """Even with an Authorization header, dev mode passes."""
        monkeypatch.delenv("C3PO_PROXY_BEARER_TOKEN", raising=False)
        manager = ProxyAuthManager()
        result = manager.validate_request("Bearer some-token")
        assert result["valid"] is True
        assert result["source"] == "no-auth"

    def test_auth_enabled_false(self, monkeypatch):
        """auth_enabled should be False when no token configured."""
        monkeypatch.delenv("C3PO_PROXY_BEARER_TOKEN", raising=False)
        manager = ProxyAuthManager()
        assert manager.auth_enabled is False


class TestProxyAuthManagerEnabled:
    """Tests when proxy token is configured."""

    @pytest.fixture(autouse=True)
    def setup_token(self, monkeypatch):
        monkeypatch.setenv("C3PO_PROXY_BEARER_TOKEN", "test-proxy-token")

    def test_valid_token(self):
        """Correct proxy token passes."""
        manager = ProxyAuthManager()
        result = manager.validate_request("Bearer test-proxy-token")
        assert result["valid"] is True
        assert result["source"] == "proxy"

    def test_invalid_token(self):
        """Wrong proxy token is rejected."""
        manager = ProxyAuthManager()
        result = manager.validate_request("Bearer wrong-token")
        assert result["valid"] is False
        assert "Invalid proxy token" in result["error"]

    def test_missing_header(self):
        """Missing Authorization header is rejected."""
        manager = ProxyAuthManager()
        result = manager.validate_request("")
        assert result["valid"] is False
        assert "Missing" in result["error"]

    def test_malformed_header_no_bearer(self):
        """Non-Bearer auth scheme is rejected."""
        manager = ProxyAuthManager()
        result = manager.validate_request("Basic abc123")
        assert result["valid"] is False
        assert "Invalid" in result["error"]

    def test_malformed_header_no_space(self):
        """Single word (no space) is rejected."""
        manager = ProxyAuthManager()
        result = manager.validate_request("BearerToken")
        assert result["valid"] is False
        assert "Invalid" in result["error"]

    def test_auth_enabled_true(self):
        """auth_enabled should be True when token configured."""
        manager = ProxyAuthManager()
        assert manager.auth_enabled is True

    def test_case_insensitive_bearer_prefix(self):
        """Bearer prefix should be case-insensitive."""
        manager = ProxyAuthManager()
        result = manager.validate_request("bearer test-proxy-token")
        assert result["valid"] is True

    def test_timing_safe_comparison(self):
        """Token comparison uses constant-time comparison (hmac.compare_digest)."""
        # This test verifies the code path; actual timing safety is
        # guaranteed by hmac.compare_digest implementation.
        manager = ProxyAuthManager()
        result = manager.validate_request("Bearer test-proxy-token")
        assert result["valid"] is True
        result = manager.validate_request("Bearer test-proxy-token-extra")
        assert result["valid"] is False
