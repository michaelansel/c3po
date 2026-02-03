"""Tests for C3PO blob storage."""

import pytest
import fakeredis

from coordinator.blobs import BlobManager, BLOB_PREFIX, BLOB_TTL, MAX_BLOB_SIZE


@pytest.fixture
def redis_client():
    """Create a fresh fakeredis client for each test."""
    return fakeredis.FakeRedis()


@pytest.fixture
def blob_manager(redis_client):
    """Create BlobManager with fakeredis."""
    return BlobManager(redis_client)


class TestStoreBlob:
    """Tests for storing blobs."""

    def test_store_and_retrieve(self, blob_manager):
        """Round-trip: store then get should return same content."""
        content = b"hello world"
        meta = blob_manager.store_blob(content, "test.txt", "text/plain", "agent/a")

        assert meta["blob_id"].startswith("blob-")
        assert meta["filename"] == "test.txt"
        assert meta["size"] == len(content)
        assert meta["mime_type"] == "text/plain"
        assert meta["uploader"] == "agent/a"
        assert meta["expires_in"] == BLOB_TTL

        result = blob_manager.get_blob(meta["blob_id"])
        assert result is not None
        retrieved_content, retrieved_meta = result
        assert retrieved_content == content
        assert retrieved_meta["filename"] == "test.txt"

    def test_store_binary_content(self, blob_manager):
        """Should handle binary content correctly."""
        content = bytes(range(256)) * 100
        meta = blob_manager.store_blob(content, "data.bin")

        result = blob_manager.get_blob(meta["blob_id"])
        assert result is not None
        assert result[0] == content

    def test_size_limit_enforced(self, blob_manager):
        """Should reject blobs exceeding MAX_BLOB_SIZE."""
        content = b"x" * (MAX_BLOB_SIZE + 1)
        with pytest.raises(ValueError, match="exceeds maximum"):
            blob_manager.store_blob(content, "too-big.txt")

    def test_exactly_at_size_limit(self, blob_manager):
        """Should accept blobs exactly at MAX_BLOB_SIZE."""
        content = b"x" * MAX_BLOB_SIZE
        meta = blob_manager.store_blob(content, "max.txt")
        assert meta["size"] == MAX_BLOB_SIZE

    def test_ttl_set(self, blob_manager, redis_client):
        """Should set TTL on the blob key."""
        content = b"test"
        meta = blob_manager.store_blob(content, "test.txt")

        key = f"{BLOB_PREFIX}{meta['blob_id']}"
        ttl = redis_client.ttl(key)
        assert ttl > 0
        assert ttl <= BLOB_TTL

    def test_unique_blob_ids(self, blob_manager):
        """Each blob should get a unique ID."""
        ids = set()
        for i in range(10):
            meta = blob_manager.store_blob(b"test", f"file{i}.txt")
            ids.add(meta["blob_id"])
        assert len(ids) == 10


class TestGetBlob:
    """Tests for retrieving blobs."""

    def test_not_found_returns_none(self, blob_manager):
        """Should return None for non-existent blob."""
        result = blob_manager.get_blob("blob-doesnotexist")
        assert result is None

    def test_metadata_only(self, blob_manager):
        """get_blob_metadata should return metadata without content."""
        content = b"hello"
        meta = blob_manager.store_blob(content, "test.txt", "text/plain", "agent/a")

        metadata = blob_manager.get_blob_metadata(meta["blob_id"])
        assert metadata is not None
        assert metadata["filename"] == "test.txt"
        assert metadata["size"] == 5
        assert metadata["mime_type"] == "text/plain"

    def test_metadata_not_found(self, blob_manager):
        """get_blob_metadata should return None for non-existent blob."""
        result = blob_manager.get_blob_metadata("blob-doesnotexist")
        assert result is None

    def test_default_mime_type(self, blob_manager):
        """Should use application/octet-stream as default mime_type."""
        meta = blob_manager.store_blob(b"data", "file.bin")
        assert meta["mime_type"] == "application/octet-stream"
