"""Tests that REST endpoints reject unauthenticated requests.

These tests run with authentication ENABLED (unlike test_rest_api.py which
disables auth to test endpoint logic). Every protected endpoint must return
401 without a valid proxy bearer token.
"""

import fakeredis
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from coordinator.agents import AgentManager
from coordinator.audit import AuditLogger
from coordinator.auth import ProxyAuthManager
from coordinator.messaging import MessageManager
from coordinator.rate_limit import RateLimiter


PROXY_TOKEN = "test-proxy-bearer-token"


@pytest.fixture
def redis_client():
    return fakeredis.FakeRedis()


@pytest.fixture
def auth_app(redis_client, monkeypatch):
    """Create the MCP app with authentication ENABLED."""
    monkeypatch.setenv("C3PO_PROXY_BEARER_TOKEN", PROXY_TOKEN)

    import coordinator.server as server_module

    monkeypatch.setattr(server_module, "redis_client", redis_client)
    monkeypatch.setattr(server_module, "agent_manager", AgentManager(redis_client))
    monkeypatch.setattr(server_module, "message_manager", MessageManager(redis_client))
    monkeypatch.setattr(server_module, "auth_manager", ProxyAuthManager())
    monkeypatch.setattr(server_module, "rate_limiter", RateLimiter(redis_client))
    monkeypatch.setattr(server_module, "audit_logger", AuditLogger(redis_client))

    return server_module.mcp.http_app()


@pytest_asyncio.fixture
async def client(auth_app):
    transport = ASGITransport(app=auth_app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


def _valid_auth():
    """Return valid Authorization header."""
    return f"Bearer {PROXY_TOKEN}"


class TestHealthEndpointNoAuth:
    """Health endpoint should work WITHOUT authentication."""

    @pytest.mark.asyncio
    async def test_health_works_without_token(self, client):
        response = await client.get("/api/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"


class TestRegisterRejectsUnauthenticated:
    """POST /api/register requires valid proxy token."""

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
                "X-Project-Name": "proj",
                "Authorization": "Bearer wrong-token",
            },
        )
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_valid_token_succeeds(self, client):
        response = await client.post(
            "/api/register",
            headers={
                "X-Machine-Name": "machine",
                "X-Project-Name": "proj",
                "Authorization": _valid_auth(),
            },
        )
        assert response.status_code == 200


class TestPendingRejectsUnauthenticated:
    """GET /api/pending requires valid proxy token."""

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
                "Authorization": "Bearer wrong-token",
            },
        )
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_valid_token_succeeds(self, client):
        response = await client.get(
            "/api/pending",
            headers={
                "X-Agent-ID": "machine/proj",
                "Authorization": _valid_auth(),
            },
        )
        assert response.status_code == 200


class TestUnregisterRejectsUnauthenticated:
    """POST /api/unregister requires valid proxy token."""

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
                "Authorization": "Bearer wrong-token",
            },
        )
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_valid_token_succeeds(self, client):
        response = await client.post(
            "/api/unregister",
            headers={
                "X-Agent-ID": "machine/proj",
                "Authorization": _valid_auth(),
            },
        )
        assert response.status_code == 200


class TestAdminAuditRejectsUnauthenticated:
    """GET /api/admin/audit requires valid proxy token."""

    @pytest.mark.asyncio
    async def test_no_auth_header(self, client):
        response = await client.get("/api/admin/audit")
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_invalid_token(self, client):
        response = await client.get(
            "/api/admin/audit",
            headers={"Authorization": "Bearer wrong-token"},
        )
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_valid_token_succeeds(self, client):
        response = await client.get(
            "/api/admin/audit",
            headers={"Authorization": _valid_auth()},
        )
        assert response.status_code == 200
        assert "entries" in response.json()
