"""Tests for REST API endpoints (/api/health, /agent/api/*, /admin/api/*)."""

import fakeredis
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from coordinator.agents import AgentManager
from coordinator.audit import AuditLogger
from coordinator.auth import AuthManager
from coordinator.blobs import BlobManager
from coordinator.messaging import MessageManager
from coordinator.rate_limit import RateLimiter


@pytest.fixture
def redis_client():
    """Create a fresh fakeredis client for each test."""
    return fakeredis.FakeRedis()


@pytest.fixture
def agent_manager(redis_client):
    """Create AgentManager with fakeredis."""
    return AgentManager(redis_client)


@pytest.fixture
def message_manager(redis_client):
    """Create MessageManager with fakeredis."""
    return MessageManager(redis_client)


@pytest.fixture
def mcp_app(redis_client, agent_manager, message_manager, monkeypatch):
    """Create the MCP app with test Redis client."""
    # Disable auth for REST API tests (tests exercise endpoint logic, not auth)
    monkeypatch.delenv("C3PO_SERVER_SECRET", raising=False)
    monkeypatch.delenv("C3PO_PROXY_BEARER_TOKEN", raising=False)
    monkeypatch.delenv("C3PO_ADMIN_KEY", raising=False)

    # Monkeypatch the module-level clients before importing server
    import coordinator.server as server_module

    monkeypatch.setattr(server_module, "redis_client", redis_client)
    monkeypatch.setattr(server_module, "agent_manager", agent_manager)
    monkeypatch.setattr(server_module, "message_manager", message_manager)
    monkeypatch.setattr(server_module, "auth_manager", AuthManager(redis_client))
    monkeypatch.setattr(server_module, "rate_limiter", RateLimiter(redis_client))
    monkeypatch.setattr(server_module, "audit_logger", AuditLogger(redis_client))
    monkeypatch.setattr(server_module, "blob_manager", BlobManager(redis_client))

    return server_module.mcp.http_app()


@pytest_asyncio.fixture
async def client(mcp_app):
    """Create async test client."""
    transport = ASGITransport(app=mcp_app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


class TestHealthEndpoint:
    """Tests for /api/health endpoint."""

    @pytest.mark.asyncio
    async def test_health_returns_ok(self, client):
        """Health endpoint should return status ok."""
        response = await client.get("/api/health")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"

    @pytest.mark.asyncio
    async def test_health_returns_agents_online_count(self, client, agent_manager):
        """Health endpoint should return count of online agents."""
        # Initially no agents
        response = await client.get("/api/health")
        data = response.json()
        assert data["agents_online"] == 0

        # Register some agents
        agent_manager.register_agent("agent-1")
        agent_manager.register_agent("agent-2")

        response = await client.get("/api/health")
        data = response.json()
        assert data["agents_online"] == 2


class TestPendingEndpoint:
    """Tests for /agent/api/pending endpoint."""

    @pytest.mark.asyncio
    async def test_pending_requires_machine_name_header(self, client):
        """Pending endpoint should require X-Machine-Name header."""
        response = await client.get("/agent/api/pending")

        assert response.status_code == 400
        data = response.json()
        assert "Missing X-Machine-Name header" in data["error"]

    @pytest.mark.asyncio
    async def test_pending_returns_empty_for_unknown_agent(self, client):
        """Pending endpoint should return empty for unknown agent."""
        response = await client.get(
            "/agent/api/pending", headers={"X-Machine-Name": "unknown-agent/project"}
        )

        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 0
        assert data["messages"] == []

    @pytest.mark.asyncio
    async def test_pending_rejects_bare_machine_name(self, client):
        """Pending endpoint should reject bare machine name without project."""
        response = await client.get(
            "/agent/api/pending", headers={"X-Machine-Name": "bare-machine"}
        )

        assert response.status_code == 400
        assert "Bare machine name" in response.json()["error"]

    @pytest.mark.asyncio
    async def test_pending_returns_count_without_consuming(
        self, client, message_manager, agent_manager
    ):
        """Pending endpoint should return count without consuming messages."""
        # Register agents
        agent_manager.register_agent("sender/proj")
        agent_manager.register_agent("receiver/proj")

        # Send a message
        message_manager.send_message(
            "sender/proj", "receiver/proj", "Test message", context="Test context"
        )

        # Check pending - should show 1
        response = await client.get(
            "/agent/api/pending", headers={"X-Machine-Name": "receiver/proj"}
        )
        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 1
        assert len(data["messages"]) == 1
        assert data["messages"][0]["message"] == "Test message"

        # Check again - should still show 1 (not consumed)
        response = await client.get(
            "/agent/api/pending", headers={"X-Machine-Name": "receiver/proj"}
        )
        data = response.json()
        assert data["count"] == 1

    @pytest.mark.asyncio
    async def test_pending_returns_multiple_messages(
        self, client, message_manager, agent_manager
    ):
        """Pending endpoint should return all pending messages."""
        agent_manager.register_agent("sender/proj")
        agent_manager.register_agent("receiver/proj")

        # Send multiple messages
        message_manager.send_message("sender/proj", "receiver/proj", "Message 1")
        message_manager.send_message("sender/proj", "receiver/proj", "Message 2")
        message_manager.send_message("sender/proj", "receiver/proj", "Message 3")

        response = await client.get(
            "/agent/api/pending", headers={"X-Machine-Name": "receiver/proj"}
        )

        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 3
        assert len(data["messages"]) == 3

        # Check FIFO order
        assert data["messages"][0]["message"] == "Message 1"
        assert data["messages"][1]["message"] == "Message 2"
        assert data["messages"][2]["message"] == "Message 3"


    @pytest.mark.asyncio
    async def test_pending_filters_acked_messages(
        self, client, message_manager, agent_manager
    ):
        """Pending endpoint should not return messages that have been acked."""
        agent_manager.register_agent("sender/proj")
        agent_manager.register_agent("receiver/proj")

        # Send two messages
        msg1 = message_manager.send_message("sender/proj", "receiver/proj", "Message 1")
        message_manager.send_message("sender/proj", "receiver/proj", "Message 2")

        # Ack the first message
        msg1_id = msg1["id"]
        message_manager.ack_messages("receiver/proj", [msg1_id])

        # Pending should only show the un-acked message
        response = await client.get(
            "/agent/api/pending", headers={"X-Machine-Name": "receiver/proj"}
        )
        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 1
        assert data["messages"][0]["message"] == "Message 2"


class TestUnregisterEndpoint:
    """Tests for /agent/api/unregister endpoint."""

    @pytest.mark.asyncio
    async def test_unregister_requires_machine_name_header(self, client):
        """Unregister endpoint should require X-Machine-Name header."""
        response = await client.post("/agent/api/unregister")

        assert response.status_code == 400
        data = response.json()
        assert "Missing X-Machine-Name header" in data["error"]

    @pytest.mark.asyncio
    async def test_unregister_removes_registered_agent(self, client, agent_manager, redis_client):
        """Unregister endpoint should remove a registered agent and clean up inbox key."""
        # Register an agent first
        agent_manager.register_agent("machine/to-remove")
        assert agent_manager.get_agent("machine/to-remove") is not None

        # Unregister the agent
        response = await client.post(
            "/agent/api/unregister", headers={"X-Machine-Name": "machine/to-remove"}
        )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert "unregistered" in data["message"].lower() or "Unregistered" in data["message"]
        assert "machine/to-remove" in data["message"]

        # Verify agent is no longer registered
        assert agent_manager.get_agent("machine/to-remove") is None

        # Verify inbox key is also cleaned up
        inbox_key = "c3po:inbox:machine/to-remove"
        assert redis_client.llen(inbox_key) == 0
        assert redis_client.exists(inbox_key) == 0

    @pytest.mark.asyncio
    async def test_unregister_unknown_agent_returns_ok(self, client, agent_manager):
        """Unregister endpoint should succeed for unknown agent (idempotent)."""
        # Verify agent doesn't exist
        assert agent_manager.get_agent("machine/nonexistent") is None

        # Unregister should still succeed
        response = await client.post(
            "/agent/api/unregister", headers={"X-Machine-Name": "machine/nonexistent"}
        )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert "not registered" in data["message"]

    @pytest.mark.asyncio
    async def test_unregister_does_not_affect_other_agents(self, client, agent_manager):
        """Unregister should only remove the specified agent."""
        # Register multiple agents
        agent_manager.register_agent("machine/agent-1")
        agent_manager.register_agent("machine/agent-2")
        agent_manager.register_agent("machine/agent-3")

        # Unregister one
        response = await client.post(
            "/agent/api/unregister", headers={"X-Machine-Name": "machine/agent-2"}
        )

        assert response.status_code == 200

        # Check only agent-2 was removed
        assert agent_manager.get_agent("machine/agent-1") is not None
        assert agent_manager.get_agent("machine/agent-2") is None
        assert agent_manager.get_agent("machine/agent-3") is not None

    @pytest.mark.asyncio
    async def test_unregister_reflects_in_agent_count(self, client, agent_manager):
        """Unregistered agent should be reflected in health endpoint count."""
        # Register agents
        agent_manager.register_agent("machine/agent-1")
        agent_manager.register_agent("machine/agent-2")

        # Check initial count
        response = await client.get("/api/health")
        assert response.json()["agents_online"] == 2

        # Unregister one
        await client.post("/agent/api/unregister", headers={"X-Machine-Name": "machine/agent-1"})

        # Check updated count
        response = await client.get("/api/health")
        assert response.json()["agents_online"] == 1

    @pytest.mark.asyncio
    async def test_unregister_with_pending_messages_keeps_registry(
        self, client, agent_manager, message_manager
    ):
        """Unregister with pending messages should keep agent in registry as offline."""
        agent_manager.register_agent("machine/with-messages")
        # Put a message in the inbox
        message_manager.send_message("machine/sender", "machine/with-messages", "hello")

        response = await client.post(
            "/agent/api/unregister",
            headers={"X-Machine-Name": "machine/with-messages"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["kept"] is True
        assert data["pending_messages"] is True

        # Agent should still be in registry, but offline
        agent = agent_manager.get_agent("machine/with-messages")
        assert agent is not None
        assert agent["status"] == "offline"

    @pytest.mark.asyncio
    async def test_unregister_with_keep_param_keeps_empty_registry(
        self, client, agent_manager
    ):
        """Unregister with ?keep=true should keep agent even if inbox is empty."""
        agent_manager.register_agent("machine/watcher-agent")

        response = await client.post(
            "/agent/api/unregister?keep=true",
            headers={"X-Machine-Name": "machine/watcher-agent"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["kept"] is True

        # Agent should still be in registry, but offline
        agent = agent_manager.get_agent("machine/watcher-agent")
        assert agent is not None
        assert agent["status"] == "offline"

    @pytest.mark.asyncio
    async def test_unregister_with_empty_inbox_deletes_inbox_key(
        self, client, agent_manager, redis_client, message_manager
    ):
        """Normal unregister with empty inbox should remove inbox key from Redis."""
        agent_manager.register_agent("machine/clean-exit")

        response = await client.post(
            "/agent/api/unregister",
            headers={"X-Machine-Name": "machine/clean-exit"},
        )

        assert response.status_code == 200
        # Inbox key should be gone
        inbox_key = "c3po:inbox:machine/clean-exit"
        assert redis_client.exists(inbox_key) == 0

    @pytest.mark.asyncio
    async def test_api_wait_returns_immediately_if_messages_exist(
        self, client, agent_manager, message_manager
    ):
        """GET /agent/api/wait should return immediately if messages are in the inbox."""
        agent_manager.register_agent("machine/waiter")
        message_manager.send_message("machine/sender", "machine/waiter", "you have mail")

        response = await client.get(
            "/agent/api/wait?timeout=5",
            headers={"X-Machine-Name": "machine/waiter"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "received"
        assert data["count"] == 1
        assert len(data["messages"]) == 1
        assert data["messages"][0]["message"] == "you have mail"

    @pytest.mark.asyncio
    async def test_api_wait_times_out_with_no_messages(self, client, agent_manager):
        """GET /agent/api/wait with empty inbox should return timeout after ~1s."""
        agent_manager.register_agent("machine/empty-waiter")

        import time
        start = time.monotonic()
        response = await client.get(
            "/agent/api/wait?timeout=1",
            headers={"X-Machine-Name": "machine/empty-waiter"},
            timeout=10.0,  # httpx client timeout (larger than the wait timeout)
        )
        elapsed = time.monotonic() - start

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "timeout"
        assert data["count"] == 0
        assert elapsed >= 1.0  # Should have waited at least 1 second

    @pytest.mark.asyncio
    async def test_api_wait_does_not_touch_heartbeat(self, client, agent_manager, redis_client):
        """GET /agent/api/wait should NOT update last_seen (watcher pattern)."""
        import json as _json
        agent_manager.register_agent("machine/watched")
        # Record last_seen before the wait
        before_data = _json.loads(redis_client.hget("c3po:agents", "machine/watched"))
        before_last_seen = before_data["last_seen"]

        # Artificially make it look old so the wait doesn't appear to have touched it
        import time
        time.sleep(0.05)

        await client.get(
            "/agent/api/wait?timeout=1",
            headers={"X-Machine-Name": "machine/watched"},
            timeout=10.0,
        )

        after_data = _json.loads(redis_client.hget("c3po:agents", "machine/watched"))
        after_last_seen = after_data["last_seen"]
        # last_seen should be unchanged (heartbeat not touched)
        assert after_last_seen == before_last_seen

    @pytest.mark.asyncio
    async def test_api_wait_returns_retry_on_shutdown(self, client, agent_manager):
        """GET /agent/api/wait returns status=retry with Retry-After header on server shutdown."""
        import asyncio
        import coordinator.server as server_module
        import threading

        agent_manager.register_agent("machine/shutdown-waiter")
        shutdown_event = threading.Event()
        original = server_module._shutdown_event
        server_module._shutdown_event = shutdown_event

        async def _trigger_shutdown():
            await asyncio.sleep(0.1)
            shutdown_event.set()
            try:
                server_module.redis_client.rpush(
                    "c3po:notify:machine/shutdown-waiter", "shutdown"
                )
            except Exception:
                pass

        try:
            trigger_task = asyncio.create_task(_trigger_shutdown())
            response = await client.get(
                "/agent/api/wait?timeout=10",
                headers={"X-Machine-Name": "machine/shutdown-waiter"},
                timeout=5.0,
            )
            await trigger_task
        finally:
            server_module._shutdown_event = original

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "retry"
        assert data["count"] == 0
        assert response.headers.get("retry-after") == "15"


class TestRegisterEndpoint:
    """Tests for /agent/api/register endpoint."""

    @pytest.mark.asyncio
    async def test_register_requires_machine_name_header(self, client):
        """Register endpoint should require X-Machine-Name header."""
        response = await client.post("/agent/api/register")
        assert response.status_code == 400
        assert "Missing X-Machine-Name header" in response.json()["error"]

    @pytest.mark.asyncio
    async def test_register_with_machine_and_project(self, client):
        """Register endpoint should construct agent ID from machine + project."""
        response = await client.post(
            "/agent/api/register",
            headers={
                "X-Machine-Name": "macbook",
                "X-Project-Name": "myproject",
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == "macbook/myproject"

    @pytest.mark.asyncio
    async def test_register_with_composite_machine_name(self, client):
        """Register endpoint should accept composite machine/project in X-Machine-Name."""
        response = await client.post(
            "/agent/api/register",
            headers={"X-Machine-Name": "macbook/myproject"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == "macbook/myproject"


class TestAdminKeyEndpoints:
    """Tests for /admin/api/keys endpoints (dev mode - no auth)."""

    @pytest.mark.asyncio
    async def test_create_key(self, client):
        """Should create an API key."""
        response = await client.post(
            "/admin/api/keys",
            json={"agent_pattern": "macbook/*", "description": "Test key"},
        )
        assert response.status_code == 201
        data = response.json()
        assert "key_id" in data
        assert "api_key" in data
        assert data["agent_pattern"] == "macbook/*"

    @pytest.mark.asyncio
    async def test_list_keys(self, client):
        """Should list API keys."""
        # Create a key first
        await client.post(
            "/admin/api/keys",
            json={"agent_pattern": "test/*"},
        )
        response = await client.get("/admin/api/keys")
        assert response.status_code == 200
        data = response.json()
        assert "keys" in data
        assert len(data["keys"]) == 1

    @pytest.mark.asyncio
    async def test_revoke_key(self, client):
        """Should revoke an API key."""
        # Create a key first
        create_resp = await client.post(
            "/admin/api/keys",
            json={"agent_pattern": "test/*"},
        )
        key_id = create_resp.json()["key_id"]

        # Revoke it
        response = await client.delete(f"/admin/api/keys/{key_id}")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

        # Verify it's gone
        list_resp = await client.get("/admin/api/keys")
        assert len(list_resp.json()["keys"]) == 0

    @pytest.mark.asyncio
    async def test_revoke_nonexistent_key(self, client):
        """Should return 404 for nonexistent key."""
        response = await client.delete("/admin/api/keys/nonexistent")
        assert response.status_code == 404


class TestInputValidation:
    """Tests for REST endpoint input validation."""

    @pytest.mark.asyncio
    async def test_pending_rejects_invalid_agent_id_format(self, client):
        """Pending endpoint should reject invalid agent ID format."""
        response = await client.get(
            "/agent/api/pending", headers={"X-Machine-Name": "-invalid/proj"}
        )

        assert response.status_code == 400
        data = response.json()
        assert "Invalid" in data["error"]

    @pytest.mark.asyncio
    async def test_pending_accepts_valid_agent_id(self, client):
        """Pending endpoint should accept valid agent ID format."""
        response = await client.get(
            "/agent/api/pending", headers={"X-Machine-Name": "valid-agent/proj_123"}
        )

        assert response.status_code == 200
        data = response.json()
        assert "count" in data

    @pytest.mark.asyncio
    async def test_unregister_rejects_invalid_agent_id_format(self, client):
        """Unregister endpoint should reject invalid agent ID format."""
        response = await client.post(
            "/agent/api/unregister", headers={"X-Machine-Name": " spaces/not-allowed"}
        )

        assert response.status_code == 400
        data = response.json()
        assert "Invalid" in data["error"]

    @pytest.mark.asyncio
    async def test_unregister_accepts_valid_agent_id(self, client):
        """Unregister endpoint should accept valid agent ID format."""
        response = await client.post(
            "/agent/api/unregister", headers={"X-Machine-Name": "valid.agent/proj-1"}
        )

        assert response.status_code == 200


class TestBlobUploadEndpoint:
    """Tests for /agent/api/blob POST endpoint."""

    @pytest.mark.asyncio
    async def test_upload_raw_body(self, client):
        """Should accept raw body upload with headers."""
        response = await client.post(
            "/agent/api/blob",
            content=b"hello world",
            headers={
                "Content-Type": "text/plain",
                "X-Filename": "test.txt",
                "X-Machine-Name": "test/proj",
            },
        )

        assert response.status_code == 201
        data = response.json()
        assert data["blob_id"].startswith("blob-")
        assert data["filename"] == "test.txt"
        assert data["size"] == 11
        assert "expires_in" in data

    @pytest.mark.asyncio
    async def test_upload_empty_body(self, client):
        """Should reject empty upload."""
        response = await client.post(
            "/agent/api/blob",
            content=b"",
            headers={"X-Filename": "empty.txt"},
        )

        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_upload_too_large(self, client):
        """Should reject upload over 5MB."""
        response = await client.post(
            "/agent/api/blob",
            content=b"x" * (5 * 1024 * 1024 + 1),
            headers={"X-Filename": "big.bin"},
        )

        assert response.status_code == 413


class TestBlobDownloadEndpoint:
    """Tests for /agent/api/blob/{blob_id} GET endpoint."""

    @pytest.mark.asyncio
    async def test_download_existing_blob(self, client):
        """Should return blob content with correct headers."""
        # Upload first
        upload_resp = await client.post(
            "/agent/api/blob",
            content=b"test content",
            headers={
                "Content-Type": "text/plain",
                "X-Filename": "test.txt",
                "X-Machine-Name": "test/proj",
            },
        )
        blob_id = upload_resp.json()["blob_id"]

        # Download
        response = await client.get(f"/agent/api/blob/{blob_id}")

        assert response.status_code == 200
        assert response.content == b"test content"
        assert "text/plain" in response.headers["content-type"]
        assert "test.txt" in response.headers["content-disposition"]

    @pytest.mark.asyncio
    async def test_download_not_found(self, client):
        """Should return 404 for non-existent blob."""
        response = await client.get("/agent/api/blob/blob-doesnotexist")

        assert response.status_code == 404
        data = response.json()
        assert data["code"] == "BLOB_NOT_FOUND"

    @pytest.mark.asyncio
    async def test_download_binary_blob(self, client):
        """Should handle binary content correctly."""
        binary_data = bytes(range(256))
        upload_resp = await client.post(
            "/agent/api/blob",
            content=binary_data,
            headers={
                "Content-Type": "application/octet-stream",
                "X-Filename": "data.bin",
            },
        )
        blob_id = upload_resp.json()["blob_id"]

        response = await client.get(f"/agent/api/blob/{blob_id}")
        assert response.status_code == 200
        assert response.content == binary_data


class TestAdminListAgentsEndpoint:
    """Tests for GET /admin/api/agents endpoint."""

    @pytest.mark.asyncio
    async def test_list_returns_empty_when_no_agents(self, client):
        """Should return empty list when no agents registered."""
        response = await client.get("/admin/api/agents")

        assert response.status_code == 200
        data = response.json()
        assert data["agents"] == []
        assert data["count"] == 0

    @pytest.mark.asyncio
    async def test_list_returns_agents_with_status(self, client, agent_manager):
        """Should return agents with their status."""
        agent_manager.register_agent("machine/proj1")
        agent_manager.register_agent("machine/proj2")

        response = await client.get("/admin/api/agents")

        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 2
        agent_ids = {a["id"] for a in data["agents"]}
        assert agent_ids == {"machine/proj1", "machine/proj2"}
        # All freshly registered agents should be online
        for agent in data["agents"]:
            assert agent["status"] == "online"

    @pytest.mark.asyncio
    async def test_filter_by_status_online(self, client, agent_manager, redis_client):
        """Should filter to only online agents."""
        import json
        from datetime import datetime, timedelta, timezone

        agent_manager.register_agent("machine/online-agent")
        agent_manager.register_agent("machine/offline-agent")

        # Make one agent offline
        old_time = (
            datetime.now(timezone.utc) - timedelta(seconds=agent_manager.AGENT_TIMEOUT_SECONDS + 10)
        ).isoformat()
        data = json.loads(redis_client.hget(agent_manager.AGENTS_KEY, "machine/offline-agent"))
        data["last_seen"] = old_time
        redis_client.hset(agent_manager.AGENTS_KEY, "machine/offline-agent", json.dumps(data))

        response = await client.get("/admin/api/agents?status=online")

        assert response.status_code == 200
        result = response.json()
        assert result["count"] == 1
        assert result["agents"][0]["id"] == "machine/online-agent"

    @pytest.mark.asyncio
    async def test_filter_by_status_offline(self, client, agent_manager, redis_client):
        """Should filter to only offline agents."""
        import json
        from datetime import datetime, timedelta, timezone

        agent_manager.register_agent("machine/online-agent")
        agent_manager.register_agent("machine/offline-agent")

        # Make one agent offline
        old_time = (
            datetime.now(timezone.utc) - timedelta(seconds=agent_manager.AGENT_TIMEOUT_SECONDS + 10)
        ).isoformat()
        data = json.loads(redis_client.hget(agent_manager.AGENTS_KEY, "machine/offline-agent"))
        data["last_seen"] = old_time
        redis_client.hset(agent_manager.AGENTS_KEY, "machine/offline-agent", json.dumps(data))

        response = await client.get("/admin/api/agents?status=offline")

        assert response.status_code == 200
        result = response.json()
        assert result["count"] == 1
        assert result["agents"][0]["id"] == "machine/offline-agent"

    @pytest.mark.asyncio
    async def test_filter_by_pattern(self, client, agent_manager):
        """Should filter agents by fnmatch pattern."""
        agent_manager.register_agent("stress/sender-0")
        agent_manager.register_agent("stress/sender-1")
        agent_manager.register_agent("other/agent")

        response = await client.get("/admin/api/agents?pattern=stress/*")

        assert response.status_code == 200
        result = response.json()
        assert result["count"] == 2
        agent_ids = {a["id"] for a in result["agents"]}
        assert agent_ids == {"stress/sender-0", "stress/sender-1"}

    @pytest.mark.asyncio
    async def test_filter_by_status_and_pattern(self, client, agent_manager, redis_client):
        """Should combine status and pattern filters."""
        import json
        from datetime import datetime, timedelta, timezone

        agent_manager.register_agent("stress/online")
        agent_manager.register_agent("stress/offline")
        agent_manager.register_agent("other/offline")

        # Make some agents offline
        old_time = (
            datetime.now(timezone.utc) - timedelta(seconds=agent_manager.AGENT_TIMEOUT_SECONDS + 10)
        ).isoformat()
        for aid in ["stress/offline", "other/offline"]:
            data = json.loads(redis_client.hget(agent_manager.AGENTS_KEY, aid))
            data["last_seen"] = old_time
            redis_client.hset(agent_manager.AGENTS_KEY, aid, json.dumps(data))

        response = await client.get("/admin/api/agents?status=offline&pattern=stress/*")

        assert response.status_code == 200
        result = response.json()
        assert result["count"] == 1
        assert result["agents"][0]["id"] == "stress/offline"

    @pytest.mark.asyncio
    async def test_invalid_status_returns_400(self, client):
        """Should reject invalid status values."""
        response = await client.get("/admin/api/agents?status=invalid")

        assert response.status_code == 400
        assert "Invalid status" in response.json()["error"]


class TestAdminBulkRemoveEndpoint:
    """Tests for DELETE /admin/api/agents endpoint."""

    @pytest.mark.asyncio
    async def test_requires_pattern_parameter(self, client):
        """Should return 400 when pattern query param is missing."""
        response = await client.delete("/admin/api/agents")

        assert response.status_code == 400
        assert "pattern" in response.json()["error"].lower()

    @pytest.mark.asyncio
    async def test_rejects_wildcard_star(self, client):
        """Should reject bare * pattern as safety guard."""
        response = await client.delete("/admin/api/agents?pattern=*")

        assert response.status_code == 400
        assert "Refusing" in response.json()["error"]

    @pytest.mark.asyncio
    async def test_removes_matching_agents(self, client, agent_manager):
        """Should remove agents matching the pattern and return count."""
        agent_manager.register_agent("stress/sender-0")
        agent_manager.register_agent("stress/sender-1")
        agent_manager.register_agent("other/agent")

        response = await client.delete("/admin/api/agents?pattern=stress/*")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["pattern"] == "stress/*"
        assert data["removed"] == 2
        assert sorted(data["agent_ids"]) == ["stress/sender-0", "stress/sender-1"]

        # Verify agents are actually gone
        assert agent_manager.get_agent("stress/sender-0") is None
        assert agent_manager.get_agent("other/agent") is not None

    @pytest.mark.asyncio
    async def test_no_matches_returns_zero(self, client, agent_manager):
        """Should return removed: 0 when no agents match."""
        agent_manager.register_agent("other/agent")

        response = await client.delete("/admin/api/agents?pattern=stress/*")

        assert response.status_code == 200
        data = response.json()
        assert data["removed"] == 0
        assert data["agent_ids"] == []

    @pytest.mark.asyncio
    async def test_empty_pattern_rejected(self, client):
        """Should reject empty pattern."""
        response = await client.delete("/admin/api/agents?pattern=")

        assert response.status_code == 400
        assert "pattern" in response.json()["error"].lower()

    @pytest.mark.asyncio
    async def test_delete_with_status_offline_only_removes_offline(
        self, client, agent_manager, redis_client
    ):
        """DELETE with status=offline should only remove offline agents."""
        import json
        from datetime import datetime, timedelta, timezone

        agent_manager.register_agent("test/online-agent")
        agent_manager.register_agent("test/offline-agent")

        # Make one agent offline
        old_time = (
            datetime.now(timezone.utc) - timedelta(seconds=agent_manager.AGENT_TIMEOUT_SECONDS + 10)
        ).isoformat()
        data = json.loads(redis_client.hget(agent_manager.AGENTS_KEY, "test/offline-agent"))
        data["last_seen"] = old_time
        redis_client.hset(agent_manager.AGENTS_KEY, "test/offline-agent", json.dumps(data))

        response = await client.delete("/admin/api/agents?pattern=test/*&status=offline")

        assert response.status_code == 200
        result = response.json()
        assert result["removed"] == 1
        assert result["agent_ids"] == ["test/offline-agent"]

        # Online agent should still exist
        assert agent_manager.get_agent("test/online-agent") is not None
        assert agent_manager.get_agent("test/offline-agent") is None

    @pytest.mark.asyncio
    async def test_delete_wildcard_with_status_is_allowed(
        self, client, agent_manager, redis_client
    ):
        """DELETE with pattern=*&status=offline should be allowed (bypass * guard)."""
        import json
        from datetime import datetime, timedelta, timezone

        agent_manager.register_agent("a/online")
        agent_manager.register_agent("b/offline")

        # Make one agent offline
        old_time = (
            datetime.now(timezone.utc) - timedelta(seconds=agent_manager.AGENT_TIMEOUT_SECONDS + 10)
        ).isoformat()
        data = json.loads(redis_client.hget(agent_manager.AGENTS_KEY, "b/offline"))
        data["last_seen"] = old_time
        redis_client.hset(agent_manager.AGENTS_KEY, "b/offline", json.dumps(data))

        response = await client.delete("/admin/api/agents?pattern=*&status=offline")

        assert response.status_code == 200
        result = response.json()
        assert result["removed"] == 1
        assert result["agent_ids"] == ["b/offline"]
        assert agent_manager.get_agent("a/online") is not None

    @pytest.mark.asyncio
    async def test_delete_wildcard_without_status_still_rejected(self, client):
        """DELETE with pattern=* without status should still be rejected."""
        response = await client.delete("/admin/api/agents?pattern=*")

        assert response.status_code == 400
        assert "Refusing" in response.json()["error"]

    @pytest.mark.asyncio
    async def test_delete_with_status_only_no_pattern(
        self, client, agent_manager, redis_client
    ):
        """DELETE with status=offline but no pattern should remove all offline."""
        import json
        from datetime import datetime, timedelta, timezone

        agent_manager.register_agent("a/online")
        agent_manager.register_agent("b/offline")

        old_time = (
            datetime.now(timezone.utc) - timedelta(seconds=agent_manager.AGENT_TIMEOUT_SECONDS + 10)
        ).isoformat()
        data = json.loads(redis_client.hget(agent_manager.AGENTS_KEY, "b/offline"))
        data["last_seen"] = old_time
        redis_client.hset(agent_manager.AGENTS_KEY, "b/offline", json.dumps(data))

        response = await client.delete("/admin/api/agents?status=offline")

        assert response.status_code == 200
        result = response.json()
        assert result["removed"] == 1
        assert result["agent_ids"] == ["b/offline"]


class TestValidateEndpoint:
    """Tests for GET /agent/api/validate endpoint (dev mode, no auth)."""

    @pytest.mark.asyncio
    async def test_validate_returns_200(self, client):
        """Validate endpoint should return 200 with valid/key_id/agent_pattern."""
        response = await client.get("/agent/api/validate")

        assert response.status_code == 200
        data = response.json()
        assert data["valid"] is True
        assert "key_id" in data
        assert "agent_pattern" in data

    @pytest.mark.asyncio
    async def test_validate_with_machine_name(self, client):
        """Validate with machine_name should pass (wildcard pattern in dev mode)."""
        response = await client.get("/agent/api/validate?machine_name=docker")

        assert response.status_code == 200
        data = response.json()
        assert data["valid"] is True
