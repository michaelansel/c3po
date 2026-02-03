"""Blob storage for C3PO coordinator.

Provides temporary blob storage for transferring large data between agents.
Blobs are stored in Redis with a 24-hour TTL, matching message expiry.
"""

import json
import logging
import uuid
from datetime import datetime, timezone

import redis

logger = logging.getLogger("c3po.blobs")

BLOB_PREFIX = "c3po:blob:"
BLOB_TTL = 86400  # 24 hours
MAX_BLOB_SIZE = 5 * 1024 * 1024  # 5MB


class BlobManager:
    """Manages blob storage in Redis."""

    def __init__(self, redis_client: redis.Redis):
        self.redis = redis_client

    def store_blob(
        self,
        content: bytes,
        filename: str,
        mime_type: str = "application/octet-stream",
        uploader: str = "",
    ) -> dict:
        """Store a blob and return its metadata.

        Args:
            content: Raw bytes to store
            filename: Original filename
            mime_type: MIME type of the content
            uploader: Agent ID of the uploader

        Returns:
            Dict with blob_id, filename, size, mime_type, uploader, expires_in

        Raises:
            ValueError: If content exceeds MAX_BLOB_SIZE
        """
        if len(content) > MAX_BLOB_SIZE:
            raise ValueError(
                f"Blob size ({len(content)} bytes) exceeds maximum "
                f"({MAX_BLOB_SIZE} bytes)"
            )

        blob_id = f"blob-{uuid.uuid4().hex[:16]}"
        key = f"{BLOB_PREFIX}{blob_id}"

        metadata = {
            "blob_id": blob_id,
            "filename": filename,
            "size": len(content),
            "mime_type": mime_type,
            "uploader": uploader,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

        pipe = self.redis.pipeline()
        pipe.hset(key, mapping={
            "content": content,
            "metadata": json.dumps(metadata),
        })
        pipe.expire(key, BLOB_TTL)
        pipe.execute()

        logger.info(
            "blob_stored blob_id=%s filename=%s size=%d uploader=%s",
            blob_id, filename, len(content), uploader,
        )

        return {**metadata, "expires_in": BLOB_TTL}

    def get_blob(self, blob_id: str) -> tuple[bytes, dict] | None:
        """Retrieve blob content and metadata.

        Args:
            blob_id: The blob identifier

        Returns:
            Tuple of (content_bytes, metadata_dict) or None if not found
        """
        key = f"{BLOB_PREFIX}{blob_id}"
        data = self.redis.hgetall(key)
        if not data:
            return None

        content = data[b"content"]
        metadata = json.loads(data[b"metadata"])
        return content, metadata

    def get_blob_metadata(self, blob_id: str) -> dict | None:
        """Retrieve blob metadata only (no content).

        Args:
            blob_id: The blob identifier

        Returns:
            Metadata dict or None if not found
        """
        key = f"{BLOB_PREFIX}{blob_id}"
        raw = self.redis.hget(key, "metadata")
        if raw is None:
            return None
        return json.loads(raw)
