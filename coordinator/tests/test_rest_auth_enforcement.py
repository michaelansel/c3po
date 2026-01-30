"""Tests that REST endpoints reject unauthenticated/unauthorized requests.

These tests run with authentication ENABLED (unlike test_rest_api.py which
disables auth to test endpoint logic). Every protected endpoint must return
401 without a valid token and 403 for unauthorized access patterns.
"""

import fakeredis
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from coordinator.agents import AgentManager
from coordinator.audit import AuditLogger
from coordinator.auth import AuthManager
from coordinator.messaging import MessageManager
from coordinator.rate_limit import RateLimiter


SERVER_SECRET = "test-secret"
ADMIN_KEY = "test-admin-key"


@pytest.fixture
def redis_client():
    return fakeredis.FakeRedis()


@pytest.fixture
def auth_app(redis_client, monkeypatch):
    """Create the MCP app with authentication ENABLED."""
    monkeypatch.setenv("C3PO_SERVER_SECRET", SERVER_SECRET)
    monkeypatch.setenv("C3PO_ADMIN_KEY", ADMIN_KEY)

    import coordinator.server as server_module

    # Create a single AuthManager shared between tests and server
    shared_auth = AuthManager(redis_client)

    monkeypatch.setattr(server_module, "redis_client", redis_client)
    monkeypatch.setattr(server_module, "agent_manager", AgentManager(redis_client))
    monkeypatch.setattr(server_module, "message_manager", MessageManager(redis_client))
    monkeypatch.setattr(server_module, "auth_manager", shared_auth)
    monkeypatch.setattr(server_module, "rate_limiter", RateLimiter(redis_client))
    monkeypatch.setattr(server_module, "audit_logger", AuditLogger(redis_client))

    return server_module.mcp.http_app()


@pytest.fixture
def auth_manager(auth_app, monkeypatch):
    """Return the same AuthManager instance the server uses."""
    import coordinator.server as server_module
    return server_module.auth_manager


@pytest_asyncio.fixture
async def client(auth_app):
    transport = ASGITransport(app=auth_app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


def _make_valid_token(auth_manager, pattern="machine/*"):
    """Generate a valid bearer token for testing."""
    raw_key, _ = auth_manager.generate_key(pattern)
    full_token = auth_manager.get_full_bearer_token(raw_key)
    return f"Bearer {full_token}"


def _admin_token():
    """Return the admin bearer token."""
    return f"Bearer {SERVER_SECRET}.{ADMIN_KEY}"


class TestHealthEndpointNoAuth:
    """Health endpoint should work WITHOUT authentication."""

    @pytest.mark.asyncio
    async def test_health_works_without_token(self, client):
        response = await client.get("/api/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"


class TestRegisterRejectsUnauthenticated:
    """POST /api/register requires valid authentication."""

    @pytest.mark.asyncio
    async def test_no_auth_header(self, client):
        response = await client.post(
            "/api/register",
            headers={"X-Machine-Name": "machine", "X-Project-Name": "proj"},
        )
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_invalid_bearer_token(self, client):
        response = await client.post(
            "/api/register",
            headers={
                "X-Machine-Name": "machine",
                "Authorization": "Bearer garbage-token",
            },
        )
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_wrong_server_secret(self, client):
        response = await client.post(
            "/api/register",
            headers={
                "X-Machine-Name": "machine",
                "Authorization": "Bearer wrong-secret.some-key",
            },
        )
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_valid_token_succeeds(self, client, auth_manager):
        token = _make_valid_token(auth_manager, "machine/*")
        response = await client.post(
            "/api/register",
            headers={
                "X-Machine-Name": "machine",
                "X-Project-Name": "proj",
                "Authorization": token,
            },
        )
        assert response.status_code == 200


class TestPendingRejectsUnauthenticated:
    """GET /api/pending requires valid authentication."""

    @pytest.mark.asyncio
    async def test_no_auth_header(self, client):
        response = await client.get(
            "/api/pending",
            headers={"X-Agent-ID": "machine/proj"},
        )
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_invalid_bearer_token(self, client):
        response = await client.get(
            "/api/pending",
            headers={
                "X-Agent-ID": "machine/proj",
                "Authorization": "Bearer garbage-token",
            },
        )
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_wrong_server_secret(self, client):
        response = await client.get(
            "/api/pending",
            headers={
                "X-Agent-ID": "machine/proj",
                "Authorization": "Bearer wrong-secret.some-key",
            },
        )
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_wrong_agent_pattern_returns_403(self, client, auth_manager):
        """Key for other-machine/* should not access machine/proj."""
        token = _make_valid_token(auth_manager, "other-machine/*")
        response = await client.get(
            "/api/pending",
            headers={
                "X-Agent-ID": "machine/proj",
                "Authorization": token,
            },
        )
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_valid_token_succeeds(self, client, auth_manager):
        token = _make_valid_token(auth_manager, "machine/*")
        response = await client.get(
            "/api/pending",
            headers={
                "X-Agent-ID": "machine/proj",
                "Authorization": token,
            },
        )
        assert response.status_code == 200


class TestUnregisterRejectsUnauthenticated:
    """POST /api/unregister requires valid authentication."""

    @pytest.mark.asyncio
    async def test_no_auth_header(self, client):
        response = await client.post(
            "/api/unregister",
            headers={"X-Agent-ID": "machine/proj"},
        )
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_invalid_bearer_token(self, client):
        response = await client.post(
            "/api/unregister",
            headers={
                "X-Agent-ID": "machine/proj",
                "Authorization": "Bearer garbage-token",
            },
        )
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_wrong_agent_pattern_returns_403(self, client, auth_manager):
        """Key for other-machine/* should not unregister machine/proj."""
        token = _make_valid_token(auth_manager, "other-machine/*")
        response = await client.post(
            "/api/unregister",
            headers={
                "X-Agent-ID": "machine/proj",
                "Authorization": token,
            },
        )
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_valid_token_succeeds(self, client, auth_manager):
        token = _make_valid_token(auth_manager, "machine/*")
        response = await client.post(
            "/api/unregister",
            headers={
                "X-Agent-ID": "machine/proj",
                "Authorization": token,
            },
        )
        assert response.status_code == 200


class TestAdminCreateKeyRejectsUnauthenticated:
    """POST /api/admin/keys requires admin authentication."""

    @pytest.mark.asyncio
    async def test_no_auth_header(self, client):
        response = await client.post("/api/admin/keys", json={
            "agent_pattern": "machine/*",
        })
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_invalid_token(self, client):
        response = await client.post(
            "/api/admin/keys",
            headers={"Authorization": "Bearer garbage"},
            json={"agent_pattern": "machine/*"},
        )
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_non_admin_key_returns_403(self, client, auth_manager):
        """A valid but non-admin key should get 403."""
        token = _make_valid_token(auth_manager, "machine/*")
        response = await client.post(
            "/api/admin/keys",
            headers={"Authorization": token},
            json={"agent_pattern": "machine/*"},
        )
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_admin_token_succeeds(self, client):
        response = await client.post(
            "/api/admin/keys",
            headers={"Authorization": _admin_token()},
            json={"agent_pattern": "machine/*", "description": "test"},
        )
        assert response.status_code == 200
        data = response.json()
        assert "bearer_token" in data


class TestAdminListKeysRejectsUnauthenticated:
    """GET /api/admin/keys requires admin authentication."""

    @pytest.mark.asyncio
    async def test_no_auth_header(self, client):
        response = await client.get("/api/admin/keys")
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_non_admin_key_returns_403(self, client, auth_manager):
        token = _make_valid_token(auth_manager, "*")
        response = await client.get(
            "/api/admin/keys",
            headers={"Authorization": token},
        )
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_admin_token_succeeds(self, client):
        response = await client.get(
            "/api/admin/keys",
            headers={"Authorization": _admin_token()},
        )
        assert response.status_code == 200
        assert "keys" in response.json()


class TestAdminRevokeKeyRejectsUnauthenticated:
    """DELETE /api/admin/keys/{key_id} requires admin authentication."""

    @pytest.mark.asyncio
    async def test_no_auth_header(self, client):
        response = await client.delete("/api/admin/keys/some-key-id")
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_non_admin_key_returns_403(self, client, auth_manager):
        token = _make_valid_token(auth_manager, "*")
        response = await client.delete(
            "/api/admin/keys/some-key-id",
            headers={"Authorization": token},
        )
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_admin_token_succeeds(self, client):
        """Admin can call revoke (even for nonexistent key — gets 404, not 401/403)."""
        response = await client.delete(
            "/api/admin/keys/nonexistent",
            headers={"Authorization": _admin_token()},
        )
        assert response.status_code == 404  # Not found, but auth passed


class TestAdminAuditRejectsUnauthenticated:
    """GET /api/admin/audit requires admin authentication."""

    @pytest.mark.asyncio
    async def test_no_auth_header(self, client):
        response = await client.get("/api/admin/audit")
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_non_admin_key_returns_403(self, client, auth_manager):
        token = _make_valid_token(auth_manager, "*")
        response = await client.get(
            "/api/admin/audit",
            headers={"Authorization": token},
        )
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_admin_token_succeeds(self, client):
        response = await client.get(
            "/api/admin/audit",
            headers={"Authorization": _admin_token()},
        )
        assert response.status_code == 200
        assert "entries" in response.json()
