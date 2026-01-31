"""Authentication for C3PO coordinator.

Implements proxy-based authentication: the coordinator validates a single
proxy bearer token shared with mcp-auth-proxy and nginx. All requests
reaching the coordinator must carry this token (injected by the proxy
or nginx on behalf of hooks).

When C3PO_PROXY_BEARER_TOKEN is not set, authentication is disabled
(dev mode).
"""

from __future__ import annotations

import hmac
import logging
import os

logger = logging.getLogger("c3po.auth")


class ProxyAuthManager:
    """Validates the proxy bearer token on incoming requests.

    In production, mcp-auth-proxy handles OAuth and forwards requests
    with a shared bearer token. nginx does the same for hook REST calls.
    The coordinator just validates that token.
    """

    def __init__(self):
        self._proxy_token = os.environ.get("C3PO_PROXY_BEARER_TOKEN", "")

    @property
    def auth_enabled(self) -> bool:
        """Whether authentication is active."""
        return bool(self._proxy_token)

    def validate_request(self, authorization: str) -> dict:
        """Validate an incoming request's Authorization header.

        Args:
            authorization: Full Authorization header value, e.g. "Bearer <token>"

        Returns:
            Dict with "valid": True/False and optional error info.
        """
        if not self._proxy_token:
            # Dev mode: no token configured, allow everything
            return {"valid": True, "source": "no-auth"}

        if not authorization:
            return {"valid": False, "error": "Missing Authorization header"}

        parts = authorization.split(None, 1)
        if len(parts) != 2 or parts[0].lower() != "bearer":
            return {"valid": False, "error": "Invalid Authorization format"}

        if not hmac.compare_digest(parts[1], self._proxy_token):
            logger.warning("auth_failed reason=invalid_proxy_token")
            return {"valid": False, "error": "Invalid proxy token"}

        return {"valid": True, "source": "proxy"}
