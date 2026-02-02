"""Authentication for C3PO coordinator.

Supports three auth mechanisms based on URL path prefix:

- /agent/*  — API key: Bearer <server_secret>.<api_key>
- /oauth/*  — Proxy token: Bearer <proxy_token> (injected by mcp-auth-proxy)
- /admin/*  — Admin key: Bearer <admin_key>

When none of C3PO_SERVER_SECRET, C3PO_PROXY_BEARER_TOKEN, C3PO_ADMIN_KEY
are set, authentication is disabled (dev mode).
"""

from __future__ import annotations

import fnmatch
import hashlib
import hmac
import json
import logging
import os
import secrets
import uuid
from datetime import datetime, timezone
from typing import Optional

import redis

logger = logging.getLogger("c3po.auth")


def _sha256(value: str) -> str:
    """SHA-256 hash a string, return hex digest."""
    return hashlib.sha256(value.encode()).hexdigest()


class AuthManager:
    """Validates requests and manages API keys.

    Auth modes by path prefix:
    - /agent/*: server_secret + per-agent API key
    - /oauth/*: proxy bearer token (from mcp-auth-proxy)
    - /admin/*: admin key
    - /api/health: no auth (public)

    Dev mode: when no secrets are configured, all requests pass.
    """

    API_KEYS_HASH = "c3po:api_keys"      # sha256(api_key) → JSON metadata
    KEY_IDS_HASH = "c3po:key_ids"         # key_id → sha256(api_key)

    def __init__(self, redis_client: Optional[redis.Redis] = None):
        self._server_secret = os.environ.get("C3PO_SERVER_SECRET", "")
        self._proxy_token = os.environ.get("C3PO_PROXY_BEARER_TOKEN", "")
        self._admin_key = os.environ.get("C3PO_ADMIN_KEY", "")
        self.redis = redis_client

    @property
    def auth_enabled(self) -> bool:
        """Whether any authentication is active."""
        return bool(self._server_secret or self._proxy_token or self._admin_key)

    def validate_request(self, authorization: str, path_prefix: str = "") -> dict:
        """Validate an incoming request's Authorization header.

        Args:
            authorization: Full Authorization header value, e.g. "Bearer <token>"
            path_prefix: URL path prefix to determine auth type.
                         One of "/agent", "/oauth", "/admin", or "" for legacy.

        Returns:
            Dict with "valid": True/False, "source", and optional metadata.
            On success for API keys: includes "key_id" and "agent_pattern".
        """
        # Dev mode: no secrets configured → allow everything
        if not self.auth_enabled:
            return {"valid": True, "source": "no-auth"}

        # Public endpoints need no auth
        if path_prefix == "/api":
            return {"valid": True, "source": "public"}

        if not authorization:
            return {"valid": False, "error": "Missing Authorization header"}

        parts = authorization.split(None, 1)
        if len(parts) != 2 or parts[0].lower() != "bearer":
            return {"valid": False, "error": "Invalid Authorization format"}

        token = parts[1]

        # Route to appropriate validator based on path
        if path_prefix == "/agent":
            return self._validate_api_key(token)
        elif path_prefix == "/oauth":
            return self._validate_proxy_token(token)
        elif path_prefix == "/admin":
            return self._validate_admin_key(token)
        else:
            # Legacy fallback: try proxy token (backwards compat during transition)
            return self._validate_proxy_token(token)

    def _validate_api_key(self, token: str) -> dict:
        """Validate a server_secret.api_key token for /agent/* paths."""
        if not self._server_secret:
            return {"valid": False, "error": "Server secret not configured"}

        # Split token into server_secret and api_key
        dot_idx = token.find(".")
        if dot_idx < 0:
            return {"valid": False, "error": "Invalid API key format (expected server_secret.api_key)"}

        provided_secret = token[:dot_idx]
        api_key = token[dot_idx + 1:]

        if not api_key:
            return {"valid": False, "error": "Missing API key after server secret"}

        # Validate server secret
        if not hmac.compare_digest(provided_secret, self._server_secret):
            logger.warning("auth_failed reason=invalid_server_secret")
            return {"valid": False, "error": "Invalid server secret"}

        # Look up API key in Redis
        if self.redis is None:
            return {"valid": False, "error": "Redis not available for API key validation"}

        key_hash = _sha256(api_key)
        raw = self.redis.hget(self.API_KEYS_HASH, key_hash)
        if raw is None:
            logger.warning("auth_failed reason=unknown_api_key")
            return {"valid": False, "error": "Invalid API key"}

        if isinstance(raw, bytes):
            raw = raw.decode()

        metadata = json.loads(raw)
        return {
            "valid": True,
            "source": "api_key",
            "key_id": metadata.get("key_id", ""),
            "agent_pattern": metadata.get("agent_pattern", "*"),
        }

    def _validate_proxy_token(self, token: str) -> dict:
        """Validate proxy bearer token for /oauth/* paths."""
        if not self._proxy_token:
            return {"valid": False, "error": "Proxy token not configured"}

        if not hmac.compare_digest(token, self._proxy_token):
            logger.warning("auth_failed reason=invalid_proxy_token")
            return {"valid": False, "error": "Invalid proxy token"}

        return {"valid": True, "source": "proxy"}

    def _validate_admin_key(self, token: str) -> dict:
        """Validate admin key for /admin/* paths."""
        if not self._admin_key:
            return {"valid": False, "error": "Admin key not configured"}

        if not hmac.compare_digest(token, self._admin_key):
            logger.warning("auth_failed reason=invalid_admin_key")
            return {"valid": False, "error": "Invalid admin key"}

        return {"valid": True, "source": "admin"}

    # --- API Key Management (admin operations) ---

    def create_api_key(
        self,
        agent_pattern: str = "*",
        description: str = "",
    ) -> dict:
        """Create a new API key.

        Args:
            agent_pattern: fnmatch pattern for allowed agent IDs (e.g., "macbook/*")
            description: Human-readable description of the key

        Returns:
            Dict with key_id, api_key (plaintext, only returned once), agent_pattern, created_at
        """
        if self.redis is None:
            raise RuntimeError("Redis not available")

        key_id = str(uuid.uuid4())[:8]
        api_key = secrets.token_urlsafe(32)
        key_hash = _sha256(api_key)
        now = datetime.now(timezone.utc).isoformat()

        metadata = {
            "key_id": key_id,
            "agent_pattern": agent_pattern,
            "description": description,
            "created_at": now,
        }

        # Store in Redis
        pipe = self.redis.pipeline()
        pipe.hset(self.API_KEYS_HASH, key_hash, json.dumps(metadata))
        pipe.hset(self.KEY_IDS_HASH, key_id, key_hash)
        pipe.execute()

        logger.info("api_key_created key_id=%s pattern=%s", key_id, agent_pattern)

        return {
            "key_id": key_id,
            "api_key": api_key,
            "agent_pattern": agent_pattern,
            "description": description,
            "created_at": now,
        }

    def revoke_api_key(self, key_id: str) -> bool:
        """Revoke an API key by key_id.

        Args:
            key_id: The key ID to revoke

        Returns:
            True if key was found and revoked, False if not found
        """
        if self.redis is None:
            raise RuntimeError("Redis not available")

        # Look up hash from key_id
        key_hash = self.redis.hget(self.KEY_IDS_HASH, key_id)
        if key_hash is None:
            return False

        if isinstance(key_hash, bytes):
            key_hash = key_hash.decode()

        # Remove from both hashes
        pipe = self.redis.pipeline()
        pipe.hdel(self.API_KEYS_HASH, key_hash)
        pipe.hdel(self.KEY_IDS_HASH, key_id)
        pipe.execute()

        logger.info("api_key_revoked key_id=%s", key_id)
        return True

    def list_api_keys(self) -> list[dict]:
        """List all API keys (metadata only, no secrets).

        Returns:
            List of key metadata dicts (key_id, agent_pattern, description, created_at)
        """
        if self.redis is None:
            raise RuntimeError("Redis not available")

        raw_keys = self.redis.hgetall(self.API_KEYS_HASH)
        keys = []
        for _hash, raw_meta in raw_keys.items():
            if isinstance(raw_meta, bytes):
                raw_meta = raw_meta.decode()
            metadata = json.loads(raw_meta)
            keys.append(metadata)

        return keys

    @staticmethod
    def validate_agent_pattern(agent_id: str, pattern: str) -> bool:
        """Check if an agent_id matches a key's agent_pattern.

        Uses fnmatch for glob-style matching.

        Args:
            agent_id: The agent ID to check
            pattern: fnmatch pattern (e.g., "macbook/*", "*", "server/homelab-*")

        Returns:
            True if agent_id matches the pattern
        """
        return fnmatch.fnmatch(agent_id, pattern)
