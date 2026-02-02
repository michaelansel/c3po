"""Tests that REST endpoints reject unauthenticated requests.

These tests run with authentication ENABLED (unlike test_rest_api.py which
disables auth to test endpoint logic). Every protected endpoint must return
401 without valid credentials.
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


SERVER_SECRET = "test-server-secret"
ADMIN_KEY = "test-admin-key"
PROXY_TOKEN = "test-proxy-token"


@pytest.fixture
def redis_client():
    return fakeredis.FakeRedis()


@pytest.fixture
def auth_app(redis_client, monkeypatch):
    """Create the MCP app with authentication ENABLED."""
    monkeypatch.setenv("C3PO_SERVER_SECRET", SERVER_SECRET)
    monkeypatch.setenv("C3PO_ADMIN_KEY", ADMIN_KEY)
    monkeypatch.setenv("C3PO_PROXY_BEARER_TOKEN", PROXY_TOKEN)

    import coordinator.server as server_module

    auth_mgr = AuthManager(redis_client)
    # Create an API key for agent auth tests
    key_data = auth_mgr.create_api_key(agent_pattern="*", description="test")

    monkeypatch.setattr(server_module, "redis_client", redis_client)
    monkeypatch.setattr(server_module, "agent_manager", AgentManager(redis_client))
    monkeypatch.setattr(server_module, "message_manager", MessageManager(redis_client))
    monkeypatch.setattr(server_module, "auth_manager", auth_mgr)
    monkeypatch.setattr(server_module, "rate_limiter", RateLimiter(redis_client))
    monkeypatch.setattr(server_module, "audit_logger", AuditLogger(redis_client))

    # Store key data for use in tests (setattr directly since attribute doesn't exist yet)
    server_module._test_api_key = key_data["api_key"]

    return server_module.mcp.http_app()


@pytest_asyncio.fixture
async def client(auth_app):
    transport = ASGITransport(app=auth_app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


def _agent_auth(api_key=None):
    """Return valid agent Authorization header (server_secret.api_key)."""
    # Default key for basic tests
    return f"Bearer {SERVER_SECRET}.{api_key}" if api_key else f"Bearer {SERVER_SECRET}.dummy"


def _admin_auth():
    """Return valid admin Authorization header."""
    return f"Bearer {ADMIN_KEY}"


class TestHealthEndpointNoAuth:
    """Health endpoint should work WITHOUT authentication."""

    @pytest.mark.asyncio
    async def test_health_works_without_token(self, client):
        response = await client.get("/api/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"


class TestRegisterRejectsUnauthenticated:
    """POST /agent/api/register requires valid API key."""

    @pytest.mark.asyncio
    async def test_no_auth_header(self, client):
        response = await client.post(
            "/agent/api/register",
            headers={"X-Machine-Name": "machine", "X-Project-Name": "proj"},
        )
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_invalid_bearer_token(self, client):
        response = await client.post(
            "/agent/api/register",
            headers={
                "X-Machine-Name": "machine",
                "X-Project-Name": "proj",
                "Authorization": "Bearer wrong-secret.wrong-key",
            },
        )
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_valid_api_key_succeeds(self, client, auth_app):
        """Valid server_secret.api_key should authenticate on /agent/* paths."""
        # Get the test API key from the fixture
        import coordinator.server as server_module
        api_key = server_module._test_api_key

        response = await client.post(
            "/agent/api/register",
            headers={
                "X-Machine-Name": "machine",
                "X-Project-Name": "proj",
                "Authorization": f"Bearer {SERVER_SECRET}.{api_key}",
            },
        )
        assert response.status_code == 200


class TestPendingRejectsUnauthenticated:
    """GET /agent/api/pending requires valid API key."""

    @pytest.mark.asyncio
    async def test_no_auth_header(self, client):
        response = await client.get(
            "/agent/api/pending",
            headers={"X-Machine-Name": "machine/proj"},
        )
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_invalid_bearer_token(self, client):
        response = await client.get(
            "/agent/api/pending",
            headers={
                "X-Machine-Name": "machine/proj",
                "Authorization": "Bearer bad-secret.bad-key",
            },
        )
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_valid_api_key_succeeds(self, client):
        import coordinator.server as server_module
        api_key = server_module._test_api_key

        response = await client.get(
            "/agent/api/pending",
            headers={
                "X-Machine-Name": "machine/proj",
                "Authorization": f"Bearer {SERVER_SECRET}.{api_key}",
            },
        )
        assert response.status_code == 200


class TestUnregisterRejectsUnauthenticated:
    """POST /agent/api/unregister requires valid API key."""

    @pytest.mark.asyncio
    async def test_no_auth_header(self, client):
        response = await client.post(
            "/agent/api/unregister",
            headers={"X-Machine-Name": "machine/proj"},
        )
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_invalid_bearer_token(self, client):
        response = await client.post(
            "/agent/api/unregister",
            headers={
                "X-Machine-Name": "machine/proj",
                "Authorization": "Bearer bad-secret.bad-key",
            },
        )
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_valid_api_key_succeeds(self, client):
        import coordinator.server as server_module
        api_key = server_module._test_api_key

        response = await client.post(
            "/agent/api/unregister",
            headers={
                "X-Machine-Name": "machine/proj",
                "Authorization": f"Bearer {SERVER_SECRET}.{api_key}",
            },
        )
        assert response.status_code == 200


class TestAdminEndpointsRequireAdminKey:
    """Admin endpoints require admin key, not agent API key."""

    @pytest.mark.asyncio
    async def test_admin_audit_no_auth(self, client):
        response = await client.get("/admin/api/audit")
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_admin_audit_wrong_key(self, client):
        response = await client.get(
            "/admin/api/audit",
            headers={"Authorization": "Bearer wrong-admin-key"},
        )
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_admin_audit_valid_key(self, client):
        response = await client.get(
            "/admin/api/audit",
            headers={"Authorization": _admin_auth()},
        )
        assert response.status_code == 200
        assert "entries" in response.json()

    @pytest.mark.asyncio
    async def test_admin_create_key_requires_admin(self, client):
        """POST /admin/api/keys requires admin key."""
        response = await client.post(
            "/admin/api/keys",
            json={"agent_pattern": "test/*"},
        )
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_admin_create_key_valid(self, client):
        response = await client.post(
            "/admin/api/keys",
            json={"agent_pattern": "test/*"},
            headers={"Authorization": _admin_auth()},
        )
        assert response.status_code == 201
        assert "key_id" in response.json()

    @pytest.mark.asyncio
    async def test_admin_list_keys_requires_admin(self, client):
        """GET /admin/api/keys requires admin key."""
        response = await client.get("/admin/api/keys")
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_admin_list_keys_valid(self, client):
        response = await client.get(
            "/admin/api/keys",
            headers={"Authorization": _admin_auth()},
        )
        assert response.status_code == 200
        assert "keys" in response.json()
