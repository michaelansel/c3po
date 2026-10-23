#!/usr/bin/env bash
# Runs the PRODUCTION nginx.conf.template for real, in a container, and asks it
# for every kind of path with and without credentials, from inside and outside
# Anthropic's IP range. Run this before deploying any change to the template.
# (tests/acceptance uses its own minimal nginx-test.conf, so it never exercises
# this file.)
#
#   bash scripts/check-nginx.sh                 # default runtime: finch
#   CONTAINER=docker bash scripts/check-nginx.sh
#
# Requests are made from inside the container, so they arrive from 127.0.0.1 —
# a trusted hop, like the Docker bridge in production — which lets an
# X-Forwarded-For header stand in for the real client address.
set -euo pipefail
cd "$(dirname "$0")/.."

runtime="${CONTAINER:-finch}"
image="$(sed -n 's/^ *image: \(nginx:.*\)$/\1/p' docker-compose.prod.yml)"
tmp="$(mktemp -d)"
containers=()
cleanup() {
    for c in "${containers[@]}"; do "$runtime" rm -f "$c" >/dev/null 2>&1 || true; done
    rm -rf "$tmp"
}
trap cleanup EXIT

# Stand-ins for the coordinator (:8420) and mcp-auth-proxy (:8421). Each echoes
# what nginx forwarded, so checks can see the client IP and stripped headers.
cat >"$tmp/stubs.conf" <<'EOF'
server {
    listen 127.0.0.1:8420;
    location / { return 200 "coordinator uri=$request_uri real_ip=$http_x_real_ip xff=$http_x_forwarded_for auth_path=$http_x_c3po_auth_path end\n"; }
}
server {
    listen 127.0.0.1:8421;
    location / { return 200 "proxy uri=$request_uri real_ip=$http_x_real_ip xff=$http_x_forwarded_for auth_path=$http_x_c3po_auth_path end\n"; }
}
EOF

# start <name> <server_secret>
start() {
    local name="c3po-nginx-check-$1-$$"
    containers+=("$name")
    "$runtime" run -d --name "$name" \
        -e C3PO_SERVER_SECRET="$2" \
        -e NGINX_ENVSUBST_FILTER=C3PO_SERVER_SECRET \
        --add-host auth-proxy:127.0.0.1 \
        --add-host coordinator:127.0.0.1 \
        -v "$PWD/nginx.conf.template:/etc/nginx/templates/default.conf.template:ro" \
        -v "$tmp/stubs.conf:/etc/nginx/conf.d/stubs.conf:ro" \
        "$image" >/dev/null
    for _ in $(seq 30); do
        "$runtime" exec "$name" curl -s -o /dev/null http://127.0.0.1:80/api/health && { current="$name"; return; }
        sleep 0.5
    done
    echo "nginx did not start:"; "$runtime" logs "$name"; exit 1
}

fail=0

# check <expected-status> <description> <path> [curl args...]
check() {
    local want="$1" what="$2" path="$3" got
    shift 3
    got="$("$runtime" exec "$current" curl -s -o /dev/null -w '%{http_code}' --path-as-is "$@" "http://127.0.0.1:80$path")"
    if [ "$got" = "$want" ]; then
        printf 'ok    %s %-46s %s\n' "$got" "$path" "$what"
    else
        printf 'FAIL  %s %-46s %s (wanted %s)\n' "$got" "$path" "$what" "$want"
        fail=1
    fi
}

# body_has <substring> <description> <path> [curl args...]
body_has() {
    local want="$1" what="$2" path="$3" body
    shift 3
    body="$("$runtime" exec "$current" curl -s --path-as-is "$@" "http://127.0.0.1:80$path")"
    case "$body" in
        *"$want"*) printf 'ok    %-50s %s\n' "$path" "$what" ;;
        *) printf 'FAIL  %-50s %s\n      got: %s\n' "$path" "$what" "$body"; fail=1 ;;
    esac
}

SECRET="0123456789abcdef"
AGENT="Authorization: Bearer $SECRET.some-api-key"
WRONG="Authorization: Bearer fedcba9876543210.some-api-key"
ANTHROPIC="X-Forwarded-For: 160.79.104.10"
ELSEWHERE="X-Forwarded-For: 203.0.113.9"

start main "$SECRET"

echo "--- public"
check 200 "health"                        /api/health
check 404 "root"                          /
check 404 "unknown path"                  /anything
check 404 "traversal"                     /agent/../etc/passwd

echo "--- /agent and /admin: server_secret prefix"
check 401 "agent mcp, no auth"            /agent/mcp
check 401 "agent mcp, wrong secret"       /agent/mcp -H "$WRONG"
check 200 "agent mcp, right secret"       /agent/mcp -H "$AGENT"
check 401 "agent rest, no auth"           /agent/api/pending
check 200 "agent rest, right secret"      /agent/api/pending -H "$AGENT"
check 401 "admin, no auth"                /admin/api/keys
check 200 "admin, right secret"           /admin/api/keys -H "$AGENT"
check 401 "secret as a prefix of more"    /agent/mcp -H "Authorization: Bearer ${SECRET}x.key"
check 401 "secret without the dot"        /agent/mcp -H "Authorization: Bearer ${SECRET}"
body_has "auth_path=/agent"   "agent mcp gets the routing hint"   /agent/mcp -H "$AGENT"
body_has "uri=/mcp"           "agent mcp is rewritten to /mcp"    /agent/mcp -H "$AGENT"
body_has "auth_path= end"        "admin: client routing hint stripped" /admin/api/keys -H "$AGENT" -H "X-C3PO-Auth-Path: /agent"

echo "--- client IP: real caller from the edge's XFF, forged entries ignored"
body_has "real_ip=203.0.113.9 xff=203.0.113.9 " "edge-appended client is used" /agent/api/pending -H "$AGENT" -H "$ELSEWHERE"
body_has "real_ip=203.0.113.9 xff=203.0.113.9 " "forged left-hand entry ignored" /agent/api/pending -H "$AGENT" -H "X-Forwarded-For: 6.6.6.6, 203.0.113.9"

echo "--- mcp-auth-proxy, server-to-server routes: Anthropic only"
for path in /oauth/mcp /.well-known/oauth-authorization-server /.well-known/oauth-protected-resource \
            /.well-known/oauth-protected-resource/oauth/mcp /.idp/register /.idp/token; do
    check 200 "from Anthropic"            "$path" -H "$ANTHROPIC"
    check 403 "from elsewhere"            "$path" -H "$ELSEWHERE"
    check 403 "no XFF (bridge address)"   "$path"
done
check 403 "forged Anthropic entry first" /.idp/register -H "X-Forwarded-For: 160.79.104.10, 203.0.113.9"
body_has "uri=/mcp"           "oauth mcp is rewritten to /mcp"      /oauth/mcp -H "$ANTHROPIC"
body_has "auth_path= end"        "oauth: client routing hint stripped" /oauth/mcp -H "$ANTHROPIC" -H "X-C3PO-Auth-Path: /agent"

echo "--- mcp-auth-proxy, browser routes: open"
check 200 "authorize"                     "/.idp/auth?client_id=x" -H "$ELSEWHERE"
check 200 "consent page"                  /.idp/auth/0b8f3c2e-1d4a-4f6b-9a7e-2c5d8e1f0a3b -H "$ELSEWHERE"
check 200 "login"                         /.auth/login -H "$ELSEWHERE"
check 200 "github"                        /.auth/github -H "$ELSEWHERE"
check 200 "github callback"               "/.auth/github/callback?code=x&state=y" -H "$ELSEWHERE"

echo "--- mcp-auth-proxy, everything else: closed, even from Anthropic"
for path in /.idp/introspect /.well-known/jwks.json /.auth/logout /.auth/google /.auth/oidc/callback \
            /.idp/auth/x/y /.idp/authz /.idp/ /.auth/ /.well-known/ /.well-known/openid-configuration; do
    check 404 "closed"                    "$path" -H "$ANTHROPIC"
done

echo "--- a secret containing regex metacharacters is matched literally"
start plus "ab+c.d/e="
check 200 "exact secret"                  /agent/mcp -H "Authorization: Bearer ab+c.d/e=.k"
check 401 "'+' read as a quantifier"      /agent/mcp -H "Authorization: Bearer abbc.d/e=.k"
check 401 "'.' read as any character"     /agent/mcp -H "Authorization: Bearer ab+cXd/e=.k"

[ "$fail" -eq 0 ] && echo "all checks passed" || { echo "SOME CHECKS FAILED"; exit 1; }
