#!/bin/bash
set -uo pipefail

# Probe the live perimeter with unauthenticated requests. Every answer must
# match nginx.conf.template's design; exits non-zero on any mismatch.
# No secrets involved. Must run from OUTSIDE Anthropic's IP range (the
# operator's machine, or the VM itself, whose requests go out through the
# exe.dev edge like anyone else's).
#
# scripts/check-nginx.sh covers the full matrix before a deploy; this confirms
# the live edge + nginx behave the same way. Used by scripts/deploy.sh and
# scripts/refresh-images.sh.
#
# Usage: scripts/probe-perimeter.sh [base_url]   (default https://mcp.qerk.be)

BASE="${1:-https://mcp.qerk.be}"
fail=0

probe() {
    local want="$1" path="$2" got
    got=$(curl -s -o /dev/null -m 10 -w '%{http_code}' "${BASE}${path}")
    if [ "$got" = "$want" ]; then
        echo "probe ok    ${got} ${path}"
    else
        echo "probe FAIL  ${got} ${path} (wanted ${want})" >&2
        fail=1
    fi
}

probe 200 /api/health
probe 401 /agent/mcp
probe 401 /agent/api/pending
probe 401 /admin/api/keys
probe 403 /oauth/mcp
probe 403 /.idp/register
probe 403 /.idp/token
probe 403 /.well-known/oauth-authorization-server
probe 404 /.idp/introspect
probe 404 /.well-known/jwks.json
probe 404 /

# Browser-facing OAuth page must stay reachable (any answer but 403/404).
got=$(curl -s -o /dev/null -m 10 -w '%{http_code}' "${BASE}/.idp/auth")
if [ "$got" = "403" ] || [ "$got" = "404" ] || [ "$got" = "000" ]; then
    echo "probe FAIL  ${got} /.idp/auth (browser OAuth page should be reachable)" >&2
    fail=1
else
    echo "probe ok    ${got} /.idp/auth"
fi

exit "$fail"
