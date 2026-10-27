"""Blob storage for C3PO coordinator.

Provides temporary blob storage for transferring large data between agents.
Blob bytes live as files in a directory (C3PO_BLOB_DIR, a docker volume in
production); their metadata lives in Redis with a 24-hour TTL, matching
message expiry. The Redis key is the source of truth: a blob exists only while
its metadata key does, and files whose key has expired are deleted by a sweep
that runs at most hourly from store_blob.
"""

import json
import logging
import os
import re
import tempfile
import time
import uuid
from datetime import datetime, timezone

import redis

logger = logging.getLogger("c3po.blobs")

BLOB_PREFIX = "c3po:blob:"
BLOB_TTL = 86400  # 24 hours
MAX_BLOB_SIZE = 5 * 1024 * 1024  # 5MB
SWEEP_INTERVAL = 3600  # seconds between sweeps of expired blob files
TEMP_PREFIX = ".upload-"
TEMP_MAX_AGE = 3600  # abandoned partial uploads older than this are swept

# blob_id is used as a filename, so anything else is rejected before it
# reaches the filesystem.
BLOB_ID_RE = re.compile(r"^blob-[0-9a-f]{16}$")


class BlobTooLarge(ValueError):
    """Raised when content exceeds MAX_BLOB_SIZE. `size` is the size seen so far."""

    def __init__(self, size: int):
        self.size = size
        super().__init__(
            f"Blob size ({size} bytes) exceeds maximum ({MAX_BLOB_SIZE} bytes)"
        )


class BlobStoreFull(Exception):
    """Raised when the blob directory already holds store_limit bytes."""


def default_blob_dir() -> str:
    return os.environ.get("C3PO_BLOB_DIR") or os.path.join(
        tempfile.gettempdir(), "c3po-blobs"
    )


def default_store_limit() -> int:
    """Total bytes the blob directory may hold (C3PO_BLOB_STORE_LIMIT, default 1GiB).

    Keeps a burst of uploads from filling the host's disk; at the per-agent
    rate limit, 24h of 5MB uploads would otherwise be tens of GB.
    """
    return int(os.environ.get("C3PO_BLOB_STORE_LIMIT") or 1024 * 1024 * 1024)


class BlobManager:
    """Manages blob storage: bytes on disk, metadata in Redis."""

    def __init__(self, redis_client: redis.Redis, blob_dir: str | None = None,
                 store_limit: int | None = None):
        self.redis = redis_client
        self.blob_dir = blob_dir or default_blob_dir()
        self.store_limit = store_limit if store_limit is not None else default_store_limit()
        os.makedirs(self.blob_dir, exist_ok=True)
        self._last_sweep = 0.0

    def _path(self, blob_id: str) -> str | None:
        if not BLOB_ID_RE.match(blob_id):
            return None
        return os.path.join(self.blob_dir, blob_id)

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
            BlobTooLarge (a ValueError): If content exceeds MAX_BLOB_SIZE
            BlobStoreFull: If the blob directory is at store_limit
        """
        if len(content) > MAX_BLOB_SIZE:
            raise BlobTooLarge(len(content))
        with self.begin_upload() as upload:
            upload.write(content)
            return upload.commit(filename, mime_type, uploader)

    def begin_upload(self) -> "BlobUpload":
        """Start an incremental upload, for bodies that shouldn't be held in memory.

        Use as a context manager: write() chunks, then commit(). Leaving the
        block without committing (including on BlobTooLarge) removes the
        partial file, so a failed upload leaves no blob behind.

        Raises BlobStoreFull if the directory already holds store_limit bytes.
        Concurrent uploads can overshoot the limit by a few MAX_BLOB_SIZEs.
        """
        self._maybe_sweep()
        if self._used_bytes() >= self.store_limit:
            raise BlobStoreFull(
                f"Blob storage is full ({self.store_limit} bytes); blobs expire after 24 hours"
            )
        return BlobUpload(self)

    def _used_bytes(self) -> int:
        used = 0
        for entry in os.scandir(self.blob_dir):
            try:
                used += entry.stat().st_size
            except FileNotFoundError:
                pass
        return used

    def _commit(self, tmp_path: str, size: int, filename: str, mime_type: str, uploader: str) -> dict:
        blob_id = f"blob-{uuid.uuid4().hex[:16]}"
        metadata = {
            "blob_id": blob_id,
            "filename": filename,
            "size": size,
            "mime_type": mime_type,
            "uploader": uploader,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

        # File first, then the metadata key that makes it visible.
        path = self._path(blob_id)
        os.replace(tmp_path, path)
        try:
            pipe = self.redis.pipeline()
            pipe.hset(f"{BLOB_PREFIX}{blob_id}", mapping={"metadata": json.dumps(metadata)})
            pipe.expire(f"{BLOB_PREFIX}{blob_id}", BLOB_TTL)
            pipe.execute()
        except BaseException:
            _unlink(path)
            raise

        logger.info(
            "blob_stored blob_id=%s filename=%s size=%d uploader=%s",
            blob_id, filename, size, uploader,
        )

        return {**metadata, "expires_in": BLOB_TTL}

    def get_blob(self, blob_id: str) -> tuple[bytes, dict] | None:
        """Retrieve blob content and metadata.

        Args:
            blob_id: The blob identifier

        Returns:
            Tuple of (content_bytes, metadata_dict) or None if not found,
            expired, or blob_id is malformed
        """
        found = self.get_blob_path(blob_id)
        if found is None:
            return None
        path, metadata = found
        try:
            with open(path, "rb") as f:
                return f.read(), metadata
        except FileNotFoundError:
            return None

    def get_blob_path(self, blob_id: str) -> tuple[str, dict] | None:
        """Return (file_path, metadata) for a live blob, or None.

        None when blob_id is malformed, the metadata key has expired, or the
        file is missing. The file can still be swept between this call and a
        later open; callers that open it must handle FileNotFoundError.
        """
        path = self._path(blob_id)
        if path is None:
            return None
        metadata = self.get_blob_metadata(blob_id)
        if metadata is None or not os.path.exists(path):
            return None
        return path, metadata

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

    def _maybe_sweep(self) -> None:
        now = time.monotonic()
        if self._last_sweep and now - self._last_sweep < SWEEP_INTERVAL:
            return
        self._last_sweep = now
        try:
            self.sweep()
        except Exception:
            logger.exception("blob_sweep_failed")

    def sweep(self) -> int:
        """Delete files whose metadata has expired, and stale partial uploads.

        Returns the number of files removed.
        """
        removed = 0
        now = time.time()
        for name in os.listdir(self.blob_dir):
            path = os.path.join(self.blob_dir, name)
            if name.startswith(TEMP_PREFIX):
                try:
                    stale = now - os.path.getmtime(path) > TEMP_MAX_AGE
                except FileNotFoundError:
                    continue
                if stale:
                    removed += _unlink(path)
            elif BLOB_ID_RE.match(name) and not self.redis.exists(f"{BLOB_PREFIX}{name}"):
                removed += _unlink(path)
        if removed:
            logger.info("blob_sweep removed=%d", removed)
        return removed


class BlobUpload:
    """An in-progress upload: a temp file in blob_dir. See BlobManager.begin_upload."""

    def __init__(self, manager: BlobManager):
        self._manager = manager
        fd, self._tmp_path = tempfile.mkstemp(prefix=TEMP_PREFIX, dir=manager.blob_dir)
        self._file = os.fdopen(fd, "wb")
        self.size = 0
        self._done = False

    def write(self, chunk: bytes) -> None:
        """Append a chunk. Raises BlobTooLarge once the total passes MAX_BLOB_SIZE."""
        self.size += len(chunk)
        if self.size > MAX_BLOB_SIZE:
            raise BlobTooLarge(self.size)
        self._file.write(chunk)

    def commit(self, filename: str, mime_type: str = "application/octet-stream", uploader: str = "") -> dict:
        """Make the blob visible and return its metadata (as store_blob does)."""
        self._file.close()
        meta = self._manager._commit(self._tmp_path, self.size, filename, mime_type, uploader)
        self._done = True
        return meta

    def abort(self) -> None:
        if not self._done:
            self._file.close()
            _unlink(self._tmp_path)
            self._done = True

    def __enter__(self) -> "BlobUpload":
        return self

    def __exit__(self, *exc) -> None:
        self.abort()


def _unlink(path: str) -> int:
    try:
        os.unlink(path)
        return 1
    except FileNotFoundError:
        return 0
