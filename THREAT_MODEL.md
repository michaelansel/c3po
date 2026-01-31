# C3PO Threat Model

This document describes the security architecture, trust boundaries, and threat mitigations for the C3PO multi-agent coordination system using the OAuth + mcp-auth-proxy gateway.

## Architecture Overview

```
Claude Desktop/Mobile ──┐
                        ├──> nginx (TLS) ──> mcp-auth-proxy:8421 ──> coordinator:8420
Claude Code (MCP)  ─────┘         │                                        ↑
                                  │         (OAuth flow + proxy-bearer-token)
Hook scripts (REST) ──> nginx ────┘──────────────────────────────> coordinator:8420
                        (hook secret → injects proxy-bearer-token)
```

## Trust Boundaries

### Boundary 1: Internet → nginx (TLS termination)
- **What crosses**: All external traffic
- **Protection**: TLS 1.2+, rate limiting, request size limits
- **Trust level**: Untrusted

### Boundary 2: nginx → mcp-auth-proxy (MCP traffic)
- **What crosses**: OAuth-authenticated MCP requests
- **Protection**: mcp-auth-proxy validates GitHub OAuth tokens, restricts allowed users
- **Trust level**: Authenticated user identity (but not forwarded to coordinator)

### Boundary 3: nginx → coordinator (hook REST traffic)
- **What crosses**: Hook REST requests (`/api/*`)
- **Protection**: nginx validates `X-C3PO-Hook-Secret` header, injects `Authorization: Bearer <proxy-token>`
- **Trust level**: Holder of hook secret (assumed to be the enrolled machine)

### Boundary 4: mcp-auth-proxy → coordinator (proxied MCP)
- **What crosses**: MCP tool calls with proxy bearer token injected
- **Protection**: Coordinator validates `C3PO_PROXY_BEARER_TOKEN`
- **Trust level**: Fully trusted (proxy has already authenticated the user)

### Boundary 5: coordinator → Redis
- **What crosses**: All state (agents, messages, rate limits, audit)
- **Protection**: Redis password, Docker network isolation
- **Trust level**: Internal, trusted

## Principals

| Principal | Authentication | Authorization |
|-----------|---------------|---------------|
| MCP client (Claude Desktop/Code) | GitHub OAuth via mcp-auth-proxy | `--github-allowed-users` whitelist |
| Hook scripts | `X-C3PO-Hook-Secret` header (validated by nginx) | Allowed to call `/api/*` endpoints |
| Admin (audit access) | Hook secret → proxy bearer token | Any holder of hook secret can access audit |
| Coordinator | Trusts proxy bearer token | N/A (server-side) |

## Single-Tenant Limitation

mcp-auth-proxy does not forward per-user identity to the coordinator. All authenticated requests appear the same to the coordinator. This means:

- Any authenticated user can act as any agent ID
- Any authenticated user can read any agent's messages
- The `--github-allowed-users` restriction at the proxy is the sole access control

This is acceptable for single-user deployments. Multi-tenancy would require upstream changes to mcp-auth-proxy to forward identity headers.

## Threat Analysis

### T1: Unauthenticated MCP access
- **Attack**: Attacker sends MCP requests without OAuth
- **Mitigation**: mcp-auth-proxy rejects unauthenticated requests; coordinator validates proxy bearer token as defense-in-depth
- **Residual risk**: None if both layers are configured

### T2: Unauthenticated REST access
- **Attack**: Attacker sends REST requests without hook secret
- **Mitigation**: nginx validates `X-C3PO-Hook-Secret` and rejects requests with invalid/missing secret
- **Residual risk**: None if nginx config is correct

### T3: Hook secret brute force
- **Attack**: Attacker tries to guess the hook secret
- **Mitigation**: nginx rate limiting (30r/s for API, 5r/m for admin); secret is a random value
- **Residual risk**: Low. Secrets should be 32+ bytes of random data.

### T4: Proxy bearer token leak
- **Attack**: Attacker obtains the proxy bearer token
- **Mitigation**: Token only exists in coordinator env, mcp-auth-proxy config, and nginx config (all server-side). Never sent to clients.
- **Residual risk**: Server compromise would expose this. Standard server hardening applies.

### T5: Hook secret leak from client
- **Attack**: Hook secret stored in `~/.claude.json` is compromised
- **Mitigation**: File permissions (0600 for agent ID files). Secret only grants REST API access, not MCP access. Rate limiting applies.
- **Residual risk**: Local account compromise would expose this. Hook secret scope is limited to REST endpoints.

### T6: OAuth token theft
- **Attack**: Attacker steals a GitHub OAuth token
- **Mitigation**: Tokens are managed by mcp-auth-proxy with standard OAuth flows (PKCE, short-lived tokens). GitHub account security applies.
- **Residual risk**: Standard OAuth risks. GitHub's own security measures apply.

### T7: Message interception between agents
- **Attack**: Attacker reads messages between coordinated agents
- **Mitigation**: All traffic is TLS-encrypted. Messages stored in Redis (Docker-internal, password-protected).
- **Residual risk**: Server compromise would expose stored messages. Messages expire after 24h.

### T8: Agent impersonation
- **Attack**: Authenticated user registers as another agent ID
- **Mitigation**: None at the coordinator level (single-tenant). The `--github-allowed-users` restriction limits who can authenticate at all.
- **Residual risk**: Accepted for single-user deployments. Any authenticated user can claim any agent ID.

### T9: Denial of service
- **Attack**: Attacker floods the coordinator with requests
- **Mitigation**: nginx rate limiting, coordinator-level per-operation rate limiting, Docker resource limits (CPU, memory), Redis memory limits
- **Residual risk**: Determined attacker with valid credentials could exhaust rate limits

### T10: mcp-auth-proxy compromise
- **Attack**: Vulnerability in mcp-auth-proxy allows bypass
- **Mitigation**: Defense-in-depth: coordinator still validates proxy bearer token. Docker container isolation, resource limits, restart policy.
- **Residual risk**: If proxy is bypassed AND attacker obtains proxy bearer token, full access is possible.

### T11: Health endpoint information disclosure
- **Attack**: Unauthenticated access to `/api/health` reveals agent count
- **Mitigation**: Intentionally unauthenticated for monitoring. Only reveals agent count, no sensitive data.
- **Residual risk**: Accepted. Agent count is low-sensitivity information.

## Secret Inventory

| Secret | Stored In | Rotated By | Scope |
|--------|-----------|------------|-------|
| `C3PO_PROXY_BEARER_TOKEN` | Server `.env`, coordinator env, mcp-auth-proxy config, nginx config | Manual (redeploy) | All authenticated access to coordinator |
| `C3PO_HOOK_SECRET` | Server `.env`, nginx config, client `~/.claude.json` | Manual (redeploy + re-enroll) | REST API access via nginx |
| `GITHUB_CLIENT_SECRET` | Server `.env`, mcp-auth-proxy config | Via GitHub OAuth App settings | OAuth authentication |
| `REDIS_PASSWORD` | Server `.env`, coordinator env, Redis config | Manual (redeploy) | Redis access |

## Deployment Checklist

- [ ] Generate strong random values for all secrets (32+ bytes)
- [ ] Set `--github-allowed-users` to restrict OAuth access
- [ ] Verify nginx config has correct hook secret value
- [ ] Verify nginx config has correct proxy bearer token value
- [ ] Enable TLS via certbot
- [ ] Verify health endpoint works without auth: `curl https://mcp.qerk.be/api/health`
- [ ] Verify hook endpoint rejects without secret: `curl https://mcp.qerk.be/api/register` (should 401)
- [ ] Verify MCP endpoint requires OAuth: `curl https://mcp.qerk.be/mcp` (should 401/redirect)
- [ ] Verify OAuth discovery: `curl https://mcp.qerk.be/.well-known/oauth-authorization-server`
