"""Tests for C3PO blob storage."""

import json
import os
import time

import pytest
import fakeredis
import redis

from coordinator.blobs import (
    BlobManager, BlobStoreFull, BlobTooLarge, BLOB_PREFIX, BLOB_TTL, MAX_BLOB_SIZE,
    SWEEP_INTERVAL, TEMP_MAX_AGE, TEMP_PREFIX,
)


@pytest.fixture
def redis_client():
    """Create a fresh fakeredis client for each test."""
    return fakeredis.FakeRedis()


@pytest.fixture
def blob_manager(redis_client, tmp_path):
    """Create BlobManager with fakeredis."""
    return BlobManager(redis_client, str(tmp_path / "blobs"))


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


class TestDiskStorage:
    """Blob bytes live on disk; Redis holds only metadata."""

    def test_content_on_disk_not_in_redis(self, blob_manager, redis_client):
        meta = blob_manager.store_blob(b"payload", "p.txt")
        key = f"{BLOB_PREFIX}{meta['blob_id']}"
        assert set(redis_client.hkeys(key)) == {b"metadata"}
        with open(os.path.join(blob_manager.blob_dir, meta["blob_id"]), "rb") as f:
            assert f.read() == b"payload"

    def test_expired_metadata_hides_blob(self, blob_manager, redis_client):
        meta = blob_manager.store_blob(b"payload", "p.txt")
        redis_client.delete(f"{BLOB_PREFIX}{meta['blob_id']}")
        assert blob_manager.get_blob(meta["blob_id"]) is None
        assert blob_manager.get_blob_path(meta["blob_id"]) is None

    def test_missing_file_hides_blob(self, blob_manager):
        meta = blob_manager.store_blob(b"payload", "p.txt")
        os.unlink(os.path.join(blob_manager.blob_dir, meta["blob_id"]))
        assert blob_manager.get_blob(meta["blob_id"]) is None

    @pytest.mark.parametrize("bad_id", [
        "../etc/passwd", "blob-../../x", "blob-0123456789abcdeg", "blob-0123456789abcdef/..",
        ".upload-abc", "", "BLOB-0123456789ABCDEF",
    ])
    def test_malformed_ids_never_touch_filesystem(self, blob_manager, redis_client, bad_id):
        # Even with metadata planted under the bad id
        redis_client.hset(f"{BLOB_PREFIX}{bad_id}", mapping={"metadata": json.dumps({"size": 1})})
        assert blob_manager.get_blob(bad_id) is None
        assert blob_manager.get_blob_path(bad_id) is None

    def test_sweep_removes_expired_files_and_stale_temps(self, blob_manager, redis_client):
        keep = blob_manager.store_blob(b"keep", "k.txt")
        gone = blob_manager.store_blob(b"gone", "g.txt")
        redis_client.delete(f"{BLOB_PREFIX}{gone['blob_id']}")
        stale_tmp = os.path.join(blob_manager.blob_dir, f"{TEMP_PREFIX}old")
        fresh_tmp = os.path.join(blob_manager.blob_dir, f"{TEMP_PREFIX}new")
        for p in (stale_tmp, fresh_tmp):
            open(p, "wb").close()
        old = time.time() - TEMP_MAX_AGE - 10
        os.utime(stale_tmp, (old, old))

        assert blob_manager.sweep() == 2
        assert sorted(os.listdir(blob_manager.blob_dir)) == sorted([keep["blob_id"], f"{TEMP_PREFIX}new"])

    def test_sweep_runs_from_store_at_most_hourly(self, blob_manager, redis_client):
        first = blob_manager.store_blob(b"a", "a.txt")
        redis_client.delete(f"{BLOB_PREFIX}{first['blob_id']}")
        blob_manager.store_blob(b"b", "b.txt")  # within the interval: no sweep
        assert first["blob_id"] in os.listdir(blob_manager.blob_dir)
        blob_manager._last_sweep -= SWEEP_INTERVAL + 1
        blob_manager.store_blob(b"c", "c.txt")
        assert first["blob_id"] not in os.listdir(blob_manager.blob_dir)


class TestStoreLimit:
    def test_full_store_rejects_new_uploads(self, redis_client, tmp_path):
        mgr = BlobManager(redis_client, str(tmp_path / "blobs"), store_limit=10)
        mgr.store_blob(b"0123456789", "a.bin")  # reaches the limit
        with pytest.raises(BlobStoreFull):
            mgr.store_blob(b"x", "b.bin")
        assert len(os.listdir(mgr.blob_dir)) == 1

    def test_expired_blobs_free_space(self, redis_client, tmp_path):
        mgr = BlobManager(redis_client, str(tmp_path / "blobs"), store_limit=10)
        meta = mgr.store_blob(b"0123456789", "a.bin")
        redis_client.delete(f"{BLOB_PREFIX}{meta['blob_id']}")
        mgr._last_sweep -= SWEEP_INTERVAL + 1
        mgr.store_blob(b"x", "b.bin")  # sweep runs first and frees the space


class TestIncrementalUpload:
    def test_chunks_commit(self, blob_manager):
        with blob_manager.begin_upload() as upload:
            upload.write(b"hello ")
            upload.write(b"world")
            meta = upload.commit("h.txt", "text/plain", "agent/a")
        assert meta["size"] == 11
        assert blob_manager.get_blob(meta["blob_id"])[0] == b"hello world"

    def test_too_large_leaves_nothing(self, blob_manager):
        with pytest.raises(BlobTooLarge) as exc:
            with blob_manager.begin_upload() as upload:
                upload.write(b"x" * MAX_BLOB_SIZE)
                upload.write(b"x")
        assert exc.value.size == MAX_BLOB_SIZE + 1
        assert os.listdir(blob_manager.blob_dir) == []

    def test_uncommitted_leaves_nothing(self, blob_manager):
        with blob_manager.begin_upload() as upload:
            upload.write(b"partial")
        assert os.listdir(blob_manager.blob_dir) == []

    def test_redis_failure_leaves_nothing(self, blob_manager, monkeypatch):
        def boom(*a, **kw):
            raise redis.ConnectionError("down")
        monkeypatch.setattr(blob_manager.redis, "pipeline", boom)
        with pytest.raises(redis.ConnectionError):
            blob_manager.store_blob(b"data", "d.txt")
        assert os.listdir(blob_manager.blob_dir) == []
