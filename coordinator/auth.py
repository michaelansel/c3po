"""Authentication and authorization for C3PO coordinator.

Implements a two-layer bearer token scheme:
- Server secret: shared between nginx and coordinator for pre-authentication
- API key: per-agent key for identity and authorization

Token format: "Bearer <server_secret>.<api_key>"
"""

import fnmatch
import hashlib
import hmac
import json
import logging
import os
import secrets
from datetime import datetime, timezone
from typing import Optional

import redis

logger = logging.getLogger("c3po.auth")


class AuthManager:
    """Manages API key authentication and authorization."""

    API_KEYS_HASH = "c3po:api_keys"  # sha256(key) -> metadata JSON
    KEY_IDS_HASH = "c3po:key_ids"    # key_id -> sha256(key) for revocation

    def __init__(self, redis_client: redis.Redis):
        self.redis = redis_client
        self._server_secret = os.environ.get("C3PO_SERVER_SECRET", "")
        self._admin_key = os.environ.get("C3PO_ADMIN_KEY", "")

    @staticmethod
    def _hash_key(raw_key: str) -> str:
        """SHA-256 hash of an API key for storage."""
        return hashlib.sha256(raw_key.encode()).hexdigest()

    def parse_bearer_token(self, authorization: str) -> tuple[str, str]:
        """Parse Authorization header into (server_secret, api_key).

        Args:
            authorization: Full Authorization header value, e.g. "Bearer secret.key"

        Returns:
            Tuple of (server_secret, api_key)

        Raises:
            ValueError: If format is invalid
        """
        if not authorization:
            raise ValueError("Missing Authorization header")

        parts = authorization.split(None, 1)
        if len(parts) != 2 or parts[0].lower() != "bearer":
            raise ValueError("Invalid Authorization format, expected: Bearer <token>")

        token = parts[1]
        dot_idx = token.find(".")
        if dot_idx < 0:
            raise ValueError("Invalid token format, expected: <server_secret>.<api_key>")

        server_secret = token[:dot_idx]
        api_key = token[dot_idx + 1:]

        if not server_secret or not api_key:
            raise ValueError("Both server_secret and api_key must be non-empty")

        return server_secret, api_key

    def validate_server_secret(self, provided_secret: str) -> bool:
        """Validate the server secret using constant-time comparison."""
        if not self._server_secret:
            # No server secret configured — skip this layer
            return True
        return hmac.compare_digest(provided_secret, self._server_secret)

    def generate_key(
        self,
        agent_pattern: str,
        description: str = "",
    ) -> tuple[str, dict]:
        """Generate a new API key.

        Args:
            agent_pattern: Glob pattern for allowed agent IDs (e.g. "macbook/*")
            description: Human-readable description

        Returns:
            Tuple of (raw_api_key, metadata_dict)
        """
        raw_key = secrets.token_urlsafe(32)
        key_hash = self._hash_key(raw_key)
        key_id = secrets.token_urlsafe(8)
        now = datetime.now(timezone.utc).isoformat()

        metadata = {
            "key_id": key_id,
            "agent_pattern": agent_pattern,
            "description": description,
            "created_at": now,
            "last_used": now,
        }

        self.redis.hset(self.API_KEYS_HASH, key_hash, json.dumps(metadata))
        self.redis.hset(self.KEY_IDS_HASH, key_id, key_hash)

        logger.info("api_key_created key_id=%s pattern=%s", key_id, agent_pattern)
        return raw_key, metadata

    def validate_key(self, raw_key: str) -> Optional[dict]:
        """Validate an API key and return its metadata.

        Updates last_used on success.

        Args:
            raw_key: The raw API key string

        Returns:
            Metadata dict if valid, None if invalid
        """
        key_hash = self._hash_key(raw_key)
        data = self.redis.hget(self.API_KEYS_HASH, key_hash)
        if data is None:
            return None

        if isinstance(data, bytes):
            data = data.decode()

        metadata = json.loads(data)

        # Update last_used
        metadata["last_used"] = datetime.now(timezone.utc).isoformat()
        self.redis.hset(self.API_KEYS_HASH, key_hash, json.dumps(metadata))

        return metadata

    def validate_bearer_token(self, authorization: str) -> dict:
        """Validate a full bearer token (both layers).

        Args:
            authorization: Full Authorization header value

        Returns:
            Dict with "valid": True and metadata, or "valid": False with error info

        """
        try:
            server_secret, api_key = self.parse_bearer_token(authorization)
        except ValueError as e:
            return {"valid": False, "error": "unauthorized", "message": str(e)}

        if not self.validate_server_secret(server_secret):
            logger.warning("auth_failed reason=invalid_server_secret")
            return {"valid": False, "error": "unauthorized", "message": "Invalid server secret"}

        # Check if it's the admin key
        if self._admin_key and api_key == self._admin_key:
            return {
                "valid": True,
                "is_admin": True,
                "key_id": "admin",
                "agent_pattern": "*",
            }

        metadata = self.validate_key(api_key)
        if metadata is None:
            logger.warning("auth_failed reason=invalid_api_key")
            return {"valid": False, "error": "unauthorized", "message": "Invalid API key"}

        return {
            "valid": True,
            "is_admin": False,
            **metadata,
        }

    def check_agent_authorization(self, auth_result: dict, agent_id: str) -> bool:
        """Check if an authenticated key is authorized to act as agent_id.

        Args:
            auth_result: Result from validate_bearer_token
            agent_id: The agent ID being accessed

        Returns:
            True if authorized
        """
        if not auth_result.get("valid"):
            return False

        if auth_result.get("is_admin"):
            return True

        pattern = auth_result.get("agent_pattern", "")
        return fnmatch.fnmatch(agent_id, pattern)

    def revoke_key(self, key_id: str) -> bool:
        """Revoke an API key by its key_id.

        Args:
            key_id: The key ID to revoke

        Returns:
            True if key was found and revoked
        """
        key_hash = self.redis.hget(self.KEY_IDS_HASH, key_id)
        if key_hash is None:
            return False

        if isinstance(key_hash, bytes):
            key_hash = key_hash.decode()

        self.redis.hdel(self.API_KEYS_HASH, key_hash)
        self.redis.hdel(self.KEY_IDS_HASH, key_id)

        logger.info("api_key_revoked key_id=%s", key_id)
        return True

    def list_keys(self) -> list[dict]:
        """List all API keys (metadata only, no raw values).

        Returns:
            List of metadata dicts
        """
        all_data = self.redis.hgetall(self.API_KEYS_HASH)
        keys = []
        for _hash, data in all_data.items():
            if isinstance(data, bytes):
                data = data.decode()
            keys.append(json.loads(data))
        return keys

    def get_full_bearer_token(self, raw_api_key: str) -> str:
        """Construct the full bearer token from server secret + api key.

        Args:
            raw_api_key: The raw API key

        Returns:
            Full token in format "server_secret.api_key"
        """
        return f"{self._server_secret}.{raw_api_key}"
