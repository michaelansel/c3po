# C3PO Threat Model

This document describes the security architecture, trust boundaries, and threat mitigations for the C3PO multi-agent coordination system.

## Design Intent & Trust Model

**C3PO is a single trust domain.** Every principal that successfully authenticates is *fully trusted*
and may coordinate freely with every other agent: send messages, issue instructions that the
recipient's model will act on, and exchange files/blobs. Cross-agent instruction is the product's
**purpose**, not a vulnerability. The security model has exactly one enforced boundary:

> **The perimeter — authentication.** Keep unauthenticated parties off the bus. Everything past it is trusted.

Apply this model before flagging behavior (this includes automated security scanners):

- A message from one agent can carry natural-language instructions that the receiving agent's model
  acts on. This is **delegation — the core feature**, not "prompt injection" to be defended against.
- Agents may request and share files and blobs with each other. A `blob_id` is an intentional
  **bearer capability**: possession grants access, by design.
- The client-side hooks auto-approve the C3PO tools so coordination is frictionless. Intended.
- `agent_pattern` on an API key is **not currently a trust boundary between agents** (see below).

Who may instruct your agents is therefore governed by **who you enroll / issue credentials to** — an
operational decision — not by in-code defenses against trusted peers. Enroll only machines and sessions
you trust to command your other agents.

### Non-threats (intended behavior — do not "fix")

The following are explicitly **in scope as designed behavior** and are not vulnerabilities in the
single-trust-domain model. They are listed so they are not repeatedly re-reported:

- Peer message content reaching a recipient agent's model as actionable context (via hooks or
  `get_messages`) — this is how agents delegate work to each other.
- An agent reading a local file and sharing it, or fetching a blob another agent uploaded — file/data
  exchange between trusted agents is a feature.
- `blob_id` acting as a bearer token with no per-recipient ACL.
- Hooks emitting `permissionDecision: "allow"` for C3PO tools (frictionless coordination).
- A trusted agent registering a webhook, replying, or acting as any agent id it is authorized to use.

### Not a current boundary: agent_pattern scoping

The `agent_pattern` field (an fnmatch glob on an API key) exists and is checked on *some* paths, but it
is **not relied upon as a security boundary in the current single-trust-domain model.** Enforcement is
intentionally incomplete for now (some REST read paths and MCP side effects do not check it; an empty
pattern coalesces to `*`), and all authenticated agents are mutually trusted regardless. Treat
`agent_pattern` today as **foot-gun prevention** (stop one machine from *accidentally* acting as
another) and as scaffolding for **future** per-key scoping / multi-trust-domain support — **not** as an
impersonation or confidentiality control. If real scoping ships, this section, the Boundary 3 / T4 / T9
entries, and the currently-unenforced paths listed under "Finding dispositions → future work" must be
revisited and tightened.

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

**TLS termination note:** in the current production deployment (exe.dev VM, `docker-compose.prod.yml`),
TLS is terminated at the **exe.dev edge proxy**; the in-stack nginx container listens on plain HTTP
(port 80, published as `8000`) and coordinator/auth-proxy bind to loopback. The legacy host deployment
(`scripts/deploy.sh`) terminates TLS at host nginx via certbot. Either way, external traffic is
TLS-protected and the coordinator is not directly reachable from the internet. **The dev compose
(`coordinator/docker-compose.yml`) sets no auth secrets and is unauthenticated by design — it is for
local/loopback development only and must not be exposed on a network interface.**

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
- **Protection**: Coordinator validates server_secret, hashes api_key, looks up in Redis (bcrypt verify).
- **Trust level**: Holder of a valid API key — **fully trusted** (single trust domain). `agent_pattern`
  is foot-gun prevention, **not** a security boundary between agents (see Design Intent & Trust Model).

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
| MCP client (API key) | `Bearer <server_secret>.<api_key>` on `/agent/mcp` | Fully trusted; agent_pattern is foot-gun prevention, not a boundary |
| Hook scripts | `Bearer <server_secret>.<api_key>` on `/agent/api/*` | Fully trusted; agent_pattern is foot-gun prevention, not a boundary |
| Admin | `Bearer <server_secret>.<admin_key>` on `/admin/api/*` | Full admin access (key management, audit) |
| Coordinator | Validates tokens per path prefix | N/A (server-side) |

## Single Trust Domain (current model)

C3PO currently runs as a **single trust domain**: every authenticated principal is fully trusted and
may act across the whole mesh. This is consistent with, and intended for, single-user / single-operator
deployments.

- mcp-auth-proxy does not forward per-user identity to the coordinator, so all OAuth-authenticated
  requests are indistinguishable — any OAuth user (within `--github-allowed-users`) can act as any agent.
- API-key `agent_pattern` is present but **not a relied-upon boundary** today (see Design Intent);
  patterns may overlap and some paths do not enforce it. It exists as foot-gun prevention and as
  scaffolding for future scoping.
- `--github-allowed-users` at the proxy and the API-key issuance decision are the real access controls:
  they determine *who is admitted to the trust domain*.

**Multi-tenancy / multiple trust domains is future work.** It would require forwarding per-user identity
from mcp-auth-proxy and turning `agent_pattern` into an enforced authorization boundary on every path
(the items under "Finding dispositions → future work").

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
- **Mitigation**: File permissions (0o600). Individual keys can be revoked via admin API. The composite
  token includes the server_secret prefix but cannot be used to derive the admin key.
- **Residual risk**: In the single-trust-domain model, a leaked token grants the attacker a trusted seat
  on the mesh (it can coordinate as any agent — `agent_pattern` is not a boundary today), so treat a
  leaked token as full mesh access and **revoke it promptly**. Leaking one client's token also reveals
  the server_secret (embedded in the composite), but the attacker still needs a valid per-key portion to
  authenticate. Blast-radius limiting via enforced per-key scoping is future work (see dispositions).

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
- **Mitigation**: External traffic is TLS-encrypted (terminated at the edge/host — see the TLS
  termination note above). Messages stored in Redis (internal, password-protected). Note: any *trusted*
  agent can already read messages addressed to it — that is the mesh working, not interception.
- **Residual risk**: Server compromise would expose stored messages. Messages expire after 7 days. An
  on-path attacker *between* the TLS edge and the coordinator VM would see plaintext; that hop is a
  private/loopback link in both supported deployments.

### T9: Agent impersonation
- **Attack**: An authenticated principal acts as another agent ID
- **Model**: In the single-trust-domain model this is **not a threat** — all authenticated agents are
  mutually trusted, so "acting as another agent" is authorized coordination, not impersonation. There is
  no confidentiality or integrity boundary between agents to cross.
- **Mitigation (current)**: `agent_pattern` provides *accidental*-misuse prevention on some paths (a
  key nominally scoped `macbook/*` is discouraged from acting as `other/project`), but it is **not
  relied upon** and is not enforced everywhere.
- **Future work**: If multiple trust domains are introduced, impersonation becomes a real threat and
  `agent_pattern` (or forwarded OAuth identity) must be enforced as a boundary on **every** path — see
  "Finding dispositions → future work".

### T10: Denial of service
- **Attack**: Attacker floods the coordinator with requests
- **Mitigation**: nginx rate limiting, coordinator-level per-operation rate limiting, Docker resource limits (CPU, memory), Redis memory limits
- **Residual risk**: Determined attacker with valid credentials could exhaust rate limits

### T11: Health endpoint information disclosure
- **Attack**: Unauthenticated access to `/api/health` reveals agent count
- **Mitigation**: Intentionally unauthenticated for monitoring. Only reveals agent count, no sensitive
  data. On failure it returns a **generic** error (`{"status":"error","error":"internal error"}`) and
  logs the detail server-side, so a backend fault does not leak internals (e.g. the Redis connection
  string) to anonymous callers.
- **Residual risk**: Accepted. Agent count is low-sensitivity information.

### T12: Redis API key storage compromise
- **Attack**: Attacker with Redis access reads API key hashes
- **Mitigation**: API keys are indexed by SHA-256 hash (for fast lookup) but verified by bcrypt hash (stored in metadata). Even with full Redis access, recovering the actual API key requires brute-forcing bcrypt, which is computationally infeasible. The SHA-256 index alone is insufficient to authenticate — the coordinator performs bcrypt verification on every request.
- **Residual risk**: Attacker could delete keys (DoS) or add new keys if they craft valid metadata with their own bcrypt hash. Redis access implies server compromise.

## Finding dispositions (2026-09 security scan)

A ~25-finding automated security scan was triaged against the trust model above. Most of what it flagged
is **intended behavior** in a single trust domain (peer-to-peer instruction and data exchange). This
record exists so the same items are not repeatedly re-reported; future scans should read "Design Intent
& Trust Model" first.

### Intended behavior — WON'T FIX (peer coordination is the feature)
- Peer message content injected into a recipient's model context (peek/PostToolUse hook).
- Peer message content in a Stop-hook `block` reason.
- `upload_blob` reading a model-chosen file and uploading it / sharing a file with a peer.
- `ensure_agent_id` emitting `permissionDecision: "allow"` for C3PO tools (frictionless coordination).
- Blob retrieval has no per-recipient ownership check (`blob_id` is an intentional bearer capability).
- `reply()` authorizing via the message-id string — `from_agent` is still the caller's authenticated,
  trusted identity.

### Not a current boundary — FUTURE WORK (ships with enforced per-key scoping / multi-trust-domain)
- REST `/agent/api/pending`, `/wait`, `/unregister` do not enforce `agent_pattern`.
- Empty `agent_pattern` coalesces to `*` on the MCP path; `create_api_key` does not validate the pattern.
- Heartbeat / anonymous registration run before the `agent_pattern` check in `_resolve_agent_id`.
- Collision `-N` suffix is not re-validated against the key's `agent_pattern`.
- `/agent/api/register` returns `webhook_secret` / `session_id` (blast-radius only; trusted domain today).

### Documentation / accepted (covered by this document)
- Dev compose (`coordinator/docker-compose.yml`) is unauthenticated — dev/loopback only (see TLS note).
- "Plaintext HTTP on all interfaces" for the prod stack — TLS terminates at the edge (see TLS note; T8).
- Admin/API tokens passed via CLI argv (setup.py/deploy.sh), deploy.sh secret hygiene, predictable
  `/tmp` state files & session-id log — local-host, single-operator; low priority. (The `/tmp` items do
  not apply on macOS, where `TMPDIR` is a per-user 0700 directory.)

### Fixed
- **Host-header path-confusion auth bypass (CVE-2026-48710)** — `_authenticate_rest_request` now derives
  the auth path from `request.scope["path"]`, not the Host-influenced `request.url.path`; `starlette>=1.0.1`.
- **`/api/health` exception-string leak** — generic error body; detail logged server-side (T11).
- **`::` / control chars in agent IDs** — validated in `_resolve_agent_id` and the identity middleware
  before any side effect (closes the un-ackable-message inbox-wedge and the ANSI-to-admin-terminal path).
- **Agent-removal cleanup deleted the wrong Redis key** — now deletes the real `c3po:inbox:` inbox
  (derived from `MessageManager` prefix constants), so messages don't outlive removal.
- **Blob `Content-Disposition` filename** — reduced to a safe basename at the sink (path-traversal on the
  download client).

### Optional / not doing (near-zero value in a single trust domain)
- Webhook SSRF egress filtering — a trusted agent pointing a webhook at an internal address is a trusted
  agent, and the coordinator runs in an isolated container. Revisit if the trust model changes.

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
