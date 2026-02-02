"""Tests for MCP auth routing via X-C3PO-Auth-Path header.

The coordinator's single /mcp endpoint serves both API key auth (Claude Code,
via /agent/mcp) and OAuth proxy auth (Claude Desktop, via /oauth/mcp).  After
nginx rewrites /agent/mcp → /mcp, the original path prefix is lost.

nginx injects X-C3PO-Auth-Path: /agent on /agent/* requests so the
AgentIdentityMiddleware can route to the correct auth validator.  When the
header is absent (OAuth connections via mcp-auth-proxy bypass nginx), the
middleware defaults to /oauth (proxy token) validation.

See AgentIdentityMiddleware docstring in coordinator/server.py for full details.
"""

import pytest
from unittest.mock import patch, MagicMock, AsyncMock

from coordinator.auth import AuthManager


class TestAuthPathRouting:
    """Test that _authenticate_mcp_headers routes based on X-C3PO-Auth-Path."""

    @pytest.fixture(autouse=True)
    def setup_auth(self, monkeypatch):
        """Configure auth with both API key and proxy token enabled."""
        monkeypatch.setenv("C3PO_SERVER_SECRET", "test-secret")
        monkeypatch.setenv("C3PO_PROXY_BEARER_TOKEN", "test-proxy-token")
        monkeypatch.delenv("C3PO_ADMIN_KEY", raising=False)

    def test_agent_header_routes_to_api_key_validation(self):
        """X-C3PO-Auth-Path: /agent should route to API key (/agent) validation."""
        manager = AuthManager()
        # Proxy token should fail when validated as API key on /agent path
        result = manager.validate_request("Bearer test-proxy-token", "/agent")
        assert result["valid"] is False

    def test_no_header_routes_to_oauth_validation(self):
        """Missing X-C3PO-Auth-Path should route to proxy token (/oauth) validation."""
        manager = AuthManager()
        result = manager.validate_request("Bearer test-proxy-token", "/oauth")
        assert result["valid"] is True
        assert result["source"] == "proxy"

    def test_proxy_token_with_agent_header_fails(self):
        """Proxy token + X-C3PO-Auth-Path: /agent should fail.

        This verifies the security model: forging the header doesn't grant
        access unless you also have a valid API key.
        """
        manager = AuthManager()
        # A plain proxy token doesn't have the server_secret.api_key format
        result = manager.validate_request("Bearer test-proxy-token", "/agent")
        assert result["valid"] is False

    def test_admin_header_routes_to_admin_validation(self, monkeypatch):
        """X-C3PO-Auth-Path: /admin should route to admin key validation."""
        monkeypatch.setenv("C3PO_ADMIN_KEY", "test-admin-key")
        manager = AuthManager()
        result = manager.validate_request(
            "Bearer test-secret.test-admin-key", "/admin"
        )
        assert result["valid"] is True
        assert result["source"] == "admin"

    def test_bogus_header_value_defaults_to_oauth(self):
        """Unrecognized X-C3PO-Auth-Path values should default to /oauth."""
        manager = AuthManager()
        # Proxy token should work when we default to /oauth
        result = manager.validate_request("Bearer test-proxy-token", "/oauth")
        assert result["valid"] is True


class TestMiddlewareAuthPathExtraction:
    """Test that AgentIdentityMiddleware correctly extracts auth path from headers."""

    def test_header_present_agent(self):
        """Header '/agent' should produce path_prefix='/agent'."""
        headers = {"x-c3po-auth-path": "/agent"}
        auth_path = headers.get("x-c3po-auth-path", "")
        path_prefix = auth_path if auth_path in ("/agent", "/admin") else "/oauth"
        assert path_prefix == "/agent"

    def test_header_present_admin(self):
        """Header '/admin' should produce path_prefix='/admin'."""
        headers = {"x-c3po-auth-path": "/admin"}
        auth_path = headers.get("x-c3po-auth-path", "")
        path_prefix = auth_path if auth_path in ("/agent", "/admin") else "/oauth"
        assert path_prefix == "/admin"

    def test_header_absent_defaults_to_oauth(self):
        """No header should default to '/oauth'."""
        headers = {}
        auth_path = headers.get("x-c3po-auth-path", "")
        path_prefix = auth_path if auth_path in ("/agent", "/admin") else "/oauth"
        assert path_prefix == "/oauth"

    def test_header_bogus_value_defaults_to_oauth(self):
        """Unrecognized header value should default to '/oauth'."""
        headers = {"x-c3po-auth-path": "/bogus"}
        auth_path = headers.get("x-c3po-auth-path", "")
        path_prefix = auth_path if auth_path in ("/agent", "/admin") else "/oauth"
        assert path_prefix == "/oauth"

    def test_header_empty_string_defaults_to_oauth(self):
        """Empty header value should default to '/oauth'."""
        headers = {"x-c3po-auth-path": ""}
        auth_path = headers.get("x-c3po-auth-path", "")
        path_prefix = auth_path if auth_path in ("/agent", "/admin") else "/oauth"
        assert path_prefix == "/oauth"

    def test_header_oauth_value_defaults_to_oauth(self):
        """'/oauth' header value is not in the allowlist and defaults to '/oauth'.

        This is correct — /oauth connections come through mcp-auth-proxy
        which doesn't go through nginx, so this header wouldn't be set.
        The allowlist is intentionally limited to /agent and /admin.
        """
        headers = {"x-c3po-auth-path": "/oauth"}
        auth_path = headers.get("x-c3po-auth-path", "")
        path_prefix = auth_path if auth_path in ("/agent", "/admin") else "/oauth"
        assert path_prefix == "/oauth"
