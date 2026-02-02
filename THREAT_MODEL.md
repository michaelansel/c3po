# C3PO Threat Model

This document describes the security architecture, trust boundaries, and threat mitigations for the C3PO multi-agent coordination system.

## Architecture Overview

```
Claude Desktop/Mobile ──┐
                        ├──> nginx (TLS) ──> mcp-auth-proxy:8421 ──> coordinator:8420
Claude Code (OAuth)  ───┘         │              (OAuth flow)           /oauth/mcp
                                  │
Claude Code (API key) ──> nginx ──┼─────────────────────────────────> coordinator:8420
                           (TLS)  │                                    /agent/mcp
                                  │
Hook scripts (REST) ──> nginx ────┘─────────────────────────────────> coordinator:8420
                         (TLS)                                         /agent/api/*

Admin tools ──> nginx ──────────────────────────────────────────────> coordinator:8420
                 (TLS)                                                 /admin/api/*
```

## Trust Boundaries

### Boundary 1: Internet → nginx (TLS termination)
- **What crosses**: All external traffic
- **Protection**: TLS 1.2+, rate limiting, request size limits
- **Trust level**: Untrusted

### Boundary 2: nginx → mcp-auth-proxy (OAuth MCP traffic)
- **What crosses**: OAuth-authenticated MCP requests on `/oauth/*`
- **Protection**: mcp-auth-proxy validates GitHub OAuth tokens, restricts allowed users
- **Trust level**: Authenticated user identity (but not forwarded to coordinator)

### Boundary 3: nginx → coordinator (API key traffic)
- **What crosses**: MCP and REST requests on `/agent/*` with `Authorization: Bearer <server_secret>.<api_key>`
- **Protection**: Coordinator validates server_secret, hashes api_key, looks up in Redis. Agent pattern enforcement via fnmatch restricts which agent IDs a key can use.
- **Trust level**: Holder of a valid API key (scoped to agent_pattern)

### Boundary 4: nginx → coordinator (admin traffic)
- **What crosses**: Admin REST requests on `/admin/*` with `Authorization: Bearer <server_secret>.<admin_key>`
- **Protection**: nginx validates server_secret prefix (same as /agent/*); coordinator validates admin_key portion against `C3PO_ADMIN_KEY` env var
- **Trust level**: Holder of admin key (full admin access)

### Boundary 5: mcp-auth-proxy → coordinator (proxied MCP)
- **What crosses**: MCP tool calls with proxy bearer token injected
- **Protection**: Coordinator validates `C3PO_PROXY_BEARER_TOKEN`
- **Trust level**: Fully trusted (proxy has already authenticated the user)

### Boundary 6: coordinator → Redis
- **What crosses**: All state (agents, messages, rate limits, audit, API keys)
- **Protection**: Redis password, Docker network isolation
- **Trust level**: Internal, trusted

## Principals

| Principal | Authentication | Authorization |
|-----------|---------------|---------------|
| MCP client (OAuth) | GitHub OAuth via mcp-auth-proxy | `--github-allowed-users` whitelist |
| MCP client (API key) | `Bearer <server_secret>.<api_key>` on `/agent/mcp` | agent_pattern restricts usable agent IDs |
| Hook scripts | `Bearer <server_secret>.<api_key>` on `/agent/api/*` | agent_pattern restricts usable agent IDs |
| Admin | `Bearer <server_secret>.<admin_key>` on `/admin/api/*` | Full admin access (key management, audit) |
| Coordinator | Validates tokens per path prefix | N/A (server-side) |

## Single-Tenant Limitation

mcp-auth-proxy does not forward per-user identity to the coordinator. All OAuth-authenticated requests appear the same to the coordinator. For API key auth, agent_pattern provides some scoping but multiple keys can have overlapping patterns.

This means:
- Any OAuth-authenticated user can act as any agent ID
- API key users are restricted to agent IDs matching their key's agent_pattern
- The `--github-allowed-users` restriction at the proxy is the sole OAuth access control

This is acceptable for single-user deployments. Multi-tenancy would require upstream changes to mcp-auth-proxy to forward identity headers.

## Threat Analysis

### T1: Unauthenticated MCP access
- **Attack**: Attacker sends MCP requests without credentials
- **Mitigation**: `/oauth/mcp` requires OAuth via mcp-auth-proxy; `/agent/mcp` requires valid API key; coordinator validates tokens as defense-in-depth
- **Residual risk**: None if auth is configured

### T2: Unauthenticated REST access
- **Attack**: Attacker sends REST requests without credentials
- **Mitigation**: `/agent/api/*` requires valid API key; `/admin/api/*` requires admin key; coordinator validates on every request
- **Residual risk**: None if auth is configured

### T3: API key brute force
- **Attack**: Attacker tries to guess API keys
- **Mitigation**: nginx rate limiting; coordinator rate limiting (rest_register: 5/60s); API keys are random UUIDs (128 bits of entropy); server_secret adds another layer
- **Residual risk**: Low. Combined server_secret + API key makes brute force infeasible.

### T4: API key leak from client
- **Attack**: Composite API token stored in `~/.claude/c3po-credentials.json` is compromised
- **Mitigation**: File permissions (0o600). Keys are scoped by agent_pattern (limits which agent IDs can be used). Individual keys can be revoked via admin API without affecting other agents. The composite token includes the server_secret prefix, so a leaked token cannot be used to derive the admin key.
- **Residual risk**: Local account compromise would expose the token. The server_secret is embedded in the composite token, so leaking one client's token reveals the server_secret. However, the attacker still needs a valid API key to authenticate — the server_secret alone is insufficient without the per-key portion.

### T5: Server secret leak
- **Attack**: Server secret (`C3PO_SERVER_SECRET`) is compromised (e.g., via leaked composite token)
- **Mitigation**: Server secret is the nginx perimeter check. Even with the server_secret, the attacker needs a valid API key (verified by bcrypt in Redis) or the admin key to authenticate. Server secret alone allows bypassing nginx but not the coordinator.
- **Residual risk**: Attacker with server_secret can reach the coordinator directly (bypassing nginx rate limits) but still needs a valid API key. Rotation requires redeploying coordinator, updating nginx config, and re-enrolling all clients.

### T6: Admin key leak
- **Attack**: Admin key is compromised
- **Mitigation**: Only used during enrollment (setup.py). Not stored in credentials file after enrollment. Rate limiting on admin endpoints.
- **Residual risk**: Leaked admin key allows creating new API keys and viewing audit logs. Can be rotated by changing `C3PO_ADMIN_KEY` env var and redeploying.

### T7: OAuth token theft
- **Attack**: Attacker steals a GitHub OAuth token
- **Mitigation**: Tokens are managed by mcp-auth-proxy with standard OAuth flows (PKCE, short-lived tokens). GitHub account security applies.
- **Residual risk**: Standard OAuth risks. GitHub's own security measures apply.

### T8: Message interception between agents
- **Attack**: Attacker reads messages between coordinated agents
- **Mitigation**: All traffic is TLS-encrypted. Messages stored in Redis (Docker-internal, password-protected).
- **Residual risk**: Server compromise would expose stored messages. Messages expire after 24h.

### T9: Agent impersonation
- **Attack**: Authenticated user registers as another agent ID
- **Mitigation**: API key auth enforces agent_pattern via fnmatch. A key with pattern `macbook/*` cannot register as `other-machine/project`.
- **Residual risk**: OAuth users are not restricted by agent_pattern (single-tenant assumption). Keys with wildcard patterns (`*`) can impersonate any agent.

### T10: Denial of service
- **Attack**: Attacker floods the coordinator with requests
- **Mitigation**: nginx rate limiting, coordinator-level per-operation rate limiting, Docker resource limits (CPU, memory), Redis memory limits
- **Residual risk**: Determined attacker with valid credentials could exhaust rate limits

### T11: Health endpoint information disclosure
- **Attack**: Unauthenticated access to `/api/health` reveals agent count
- **Mitigation**: Intentionally unauthenticated for monitoring. Only reveals agent count, no sensitive data.
- **Residual risk**: Accepted. Agent count is low-sensitivity information.

### T12: Redis API key storage compromise
- **Attack**: Attacker with Redis access reads API key hashes
- **Mitigation**: API keys are indexed by SHA-256 hash (for fast lookup) but verified by bcrypt hash (stored in metadata). Even with full Redis access, recovering the actual API key requires brute-forcing bcrypt, which is computationally infeasible. The SHA-256 index alone is insufficient to authenticate — the coordinator performs bcrypt verification on every request.
- **Residual risk**: Attacker could delete keys (DoS) or add new keys if they craft valid metadata with their own bcrypt hash. Redis access implies server compromise.

## Secret Inventory

| Secret | Stored In | Rotated By | Scope |
|--------|-----------|------------|-------|
| `C3PO_SERVER_SECRET` | Server `.env`, coordinator env, embedded in client composite tokens | Manual (redeploy + re-enroll all clients) | nginx perimeter check (Bearer token prefix) |
| `C3PO_ADMIN_KEY` | Server `.env`, coordinator env | Manual (redeploy) | Admin API access (after server_secret prefix) |
| `C3PO_PROXY_BEARER_TOKEN` | Server `.env`, coordinator env, mcp-auth-proxy config | Manual (redeploy) | OAuth proxy → coordinator trust |
| Per-agent composite tokens | Client `~/.claude/c3po-credentials.json` (as `api_token`), Redis (SHA-256 index + bcrypt hash) | `DELETE /admin/api/keys/{id}` | Per-agent MCP and REST access |
| `GITHUB_CLIENT_SECRET` | Server `.env`, mcp-auth-proxy config | Via GitHub OAuth App settings | OAuth authentication |
| `REDIS_PASSWORD` | Server `.env`, coordinator env, Redis config | Manual (redeploy) | Redis access |

## Deployment Checklist

- [ ] Generate strong random values for all secrets (32+ bytes)
- [ ] Set `--github-allowed-users` to restrict OAuth access
- [ ] Set `C3PO_SERVER_SECRET`, `C3PO_ADMIN_KEY`, `C3PO_PROXY_BEARER_TOKEN` in coordinator env
- [ ] Configure nginx path-based routing: `/oauth/` → proxy, `/agent/` + `/admin/` → coordinator
- [ ] Enable TLS via certbot
- [ ] Verify health endpoint works without auth: `curl https://mcp.qerk.be/api/health`
- [ ] Verify agent endpoint rejects without credentials: `curl https://mcp.qerk.be/agent/api/register` (should 401)
- [ ] Verify admin endpoint rejects without credentials: `curl https://mcp.qerk.be/admin/api/keys` (should 401)
- [ ] Verify OAuth MCP endpoint requires OAuth: `curl https://mcp.qerk.be/oauth/mcp` (should 401/redirect)
- [ ] Verify API key MCP endpoint works with valid key: `curl -H "Authorization: Bearer <secret>.<key>" https://mcp.qerk.be/agent/mcp`
- [ ] Verify OAuth discovery: `curl https://mcp.qerk.be/.well-known/oauth-authorization-server`
- [ ] Enroll at least one agent via `setup.py --enroll` and verify connectivity
