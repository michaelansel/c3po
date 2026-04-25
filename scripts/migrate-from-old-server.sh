#!/bin/bash
set -euo pipefail

# =============================================================================
# C3PO Migration Script — run on the OLD server (a10.lambda.qerk.be)
#
# This script:
#   1. Reads secrets from /home/ubuntu/c3po/.secrets and .env
#   2. Dumps all c3po:* keys from the running Redis container
#   3. Rsyncs the secrets + dump to the new exe.dev VM
#   4. SSHes into the new VM and imports everything
#
# Prerequisites:
#   - SSH access from old server to new VM.  Add this to the old server's
#     ~/.ssh/authorized_keys on the NEW VM, or copy the old server's
#     public key there.  The new VM's SSH is reachable via exe.dev proxy:
#
#       ssh-copy-id -p 2222 exedev@golf-pegasus.ssh.exe.dev
#
#     Or just paste the old server's ~/.ssh/id_*.pub into
#     /home/exedev/.ssh/authorized_keys on the new VM.
#
# Usage (on old server):
#   curl -fsSL https://raw.githubusercontent.com/michaelansel/c3po/main/scripts/migrate-from-old-server.sh | bash
#   # or:
#   bash scripts/migrate-from-old-server.sh
#
# =============================================================================

NEW_HOST="golf-pegasus.exe.xyz"
NEW_PORT=22
NEW_USER="exedev"
NEW_DIR="/home/exedev/c3po"

OLD_DIR="/home/ubuntu/c3po"
TMPDIR=$(mktemp -d /tmp/c3po-migrate.XXXXXX)

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BOLD='\033[1m'
NC='\033[0m'
log()  { echo -e "${GREEN}[migrate]${NC} $1"; }
warn() { echo -e "${YELLOW}[migrate]${NC} $1"; }
err()  { echo -e "${RED}[migrate]${NC} $1" >&2; }
step() { echo -e "\n${BOLD}=== $1 ===${NC}"; }

cleanup() { rm -rf "$TMPDIR"; }
trap cleanup EXIT

new_ssh() { ssh -o StrictHostKeyChecking=accept-new "${NEW_USER}@${NEW_HOST}" "$@"; }

# ---- Preflight checks -------------------------------------------------------
step "Step 0: Preflight checks"

if [ ! -f "${OLD_DIR}/.secrets" ] && [ ! -f "${OLD_DIR}/.env" ]; then
    err "Cannot find ${OLD_DIR}/.secrets or ${OLD_DIR}/.env"
    err "Are you running this on the old server (a10.lambda.qerk.be)?"
    exit 1
fi
log "Old deployment found at ${OLD_DIR}"

# Find the redis container
REDIS_CONTAINER=$(docker ps --format '{{.Names}}' | grep -E 'redis' | grep -E 'c3po' | head -1)
if [ -z "$REDIS_CONTAINER" ]; then
    # Try broader match
    REDIS_CONTAINER=$(docker ps --format '{{.Names}}' | grep 'redis' | head -1)
fi
if [ -z "$REDIS_CONTAINER" ]; then
    err "No running Redis container found. Is c3po running?"
    err "Containers: $(docker ps --format '{{.Names}}' | tr '\n' ' ')"
    exit 1
fi
log "Found Redis container: ${REDIS_CONTAINER}"

# Get Redis password
REDIS_PW=""
if [ -f "${OLD_DIR}/.env" ]; then
    REDIS_PW=$(grep -m1 '^REDIS_PASSWORD=' "${OLD_DIR}/.env" | cut -d= -f2- || true)
fi
if [ -z "$REDIS_PW" ] && [ -f "${OLD_DIR}/.secrets" ]; then
    REDIS_PW=$(grep -m1 'REDIS_PASSWORD' "${OLD_DIR}/.secrets" | cut -d= -f2- || true)
fi
if [ -z "$REDIS_PW" ]; then
    warn "No REDIS_PASSWORD found — assuming Redis has no password"
fi

# Test SSH to new server
log "Testing SSH to new server..."
if ! new_ssh "echo ok" >/dev/null 2>&1; then
    err "Cannot SSH to ${NEW_USER}@${NEW_HOST}:${NEW_PORT}"
    err ""
    err "Fix: copy your public key to the new VM.  On THIS machine, run:"
    err "  ssh-copy-id ${NEW_USER}@${NEW_HOST}"
    err ""
    err "Or paste this into ${NEW_DIR}/../.ssh/authorized_keys on the new VM:"
    err "  $(cat ~/.ssh/id_*.pub 2>/dev/null | head -1)"
    exit 1
fi
log "SSH to new server: OK"

# ---- Step 1: Extract secrets ------------------------------------------------
step "Step 1: Extract secrets from old deployment"

# Collect all secrets into a unified format
get_secret() {
    local key="$1"
    local val=""
    if [ -f "${OLD_DIR}/.env" ]; then
        val=$(grep -m1 "^${key}=" "${OLD_DIR}/.env" 2>/dev/null | cut -d= -f2- || true)
    fi
    if [ -z "$val" ] && [ -f "${OLD_DIR}/.secrets" ]; then
        val=$(grep -m1 "${key}" "${OLD_DIR}/.secrets" 2>/dev/null | cut -d= -f2- || true)
    fi
    echo "$val"
}

SERVER_SECRET=$(get_secret C3PO_SERVER_SECRET)
ADMIN_KEY=$(get_secret C3PO_ADMIN_KEY)
PROXY_TOKEN=$(get_secret C3PO_PROXY_BEARER_TOKEN)

if [ -z "$SERVER_SECRET" ]; then
    err "C3PO_SERVER_SECRET not found in old deployment!"
    exit 1
fi
if [ -z "$ADMIN_KEY" ]; then
    err "C3PO_ADMIN_KEY not found in old deployment!"
    exit 1
fi

log "C3PO_SERVER_SECRET: ${SERVER_SECRET:0:8}..."
log "C3PO_ADMIN_KEY:     ${ADMIN_KEY:0:8}..."
log "PROXY_BEARER_TOKEN: ${PROXY_TOKEN:+${PROXY_TOKEN:0:8}...}${PROXY_TOKEN:-<not set>}"

# ---- Step 2: Dump Redis state -----------------------------------------------
step "Step 2: Dump Redis state"

REDIS_AUTH=""
if [ -n "$REDIS_PW" ]; then
    REDIS_AUTH="-a ${REDIS_PW}"
fi

# Count keys first
KEY_COUNT=$(docker exec "$REDIS_CONTAINER" redis-cli $REDIS_AUTH --no-auth-warning KEYS 'c3po:*' 2>/dev/null | wc -l)
log "Found ${KEY_COUNT} c3po:* keys in Redis"

# Export using inline Python (avoid needing the migrate script on old server)
log "Exporting state to JSON..."

python3 - "$REDIS_CONTAINER" "$REDIS_PW" "$TMPDIR/state.json" <<'PYEOF'
import json, sys, base64, subprocess

container = sys.argv[1]
redis_pw = sys.argv[2]
outpath = sys.argv[3]

def redis_cmd(*args):
    """Run a redis-cli command inside the container, return raw bytes."""
    cmd = ["docker", "exec", container, "redis-cli", "--no-auth-warning"]
    if redis_pw:
        cmd += ["-a", redis_pw]
    cmd += list(args)
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode != 0:
        print(f"redis-cli error: {r.stderr.decode()}", file=sys.stderr)
    return r.stdout

def redis_lines(*args):
    """Run redis-cli and return non-empty decoded lines."""
    raw = redis_cmd(*args)
    return [l for l in raw.decode("utf-8", errors="replace").strip().split("\n") if l]

# Get all c3po:* keys
keys = redis_lines("KEYS", "c3po:*")
print(f"Exporting {len(keys)} keys...", file=sys.stderr)

state = {"version": 1, "keys": {}}

for key in keys:
    key = key.strip()
    if not key:
        continue
    
    ktype = redis_lines("TYPE", key)[0].replace("type:", "").strip()
    # TYPE returns e.g. "string" or sometimes "type: string" depending on version
    for prefix in ["type: ", "type:"]:
        ktype = ktype.removeprefix(prefix).strip()
    
    try:
        if ktype == "string":
            val = redis_cmd("GET", key)
            # redis-cli adds a trailing newline
            if val.endswith(b"\n"):
                val = val[:-1]
            state["keys"][key] = {
                "type": "string",
                "value": base64.b64encode(val).decode("ascii")
            }
        elif ktype == "hash":
            lines = redis_lines("HGETALL", key)
            pairs = {}
            for i in range(0, len(lines), 2):
                hk = lines[i]
                hv = lines[i+1] if i+1 < len(lines) else ""
                pairs[hk] = base64.b64encode(hv.encode("utf-8")).decode("ascii")
            state["keys"][key] = {"type": "hash", "value": pairs}
        elif ktype == "list":
            lines = redis_lines("LRANGE", key, "0", "-1")
            state["keys"][key] = {
                "type": "list",
                "value": [base64.b64encode(l.encode("utf-8")).decode("ascii") for l in lines]
            }
        elif ktype == "set":
            lines = redis_lines("SMEMBERS", key)
            state["keys"][key] = {
                "type": "set",
                "value": [base64.b64encode(l.encode("utf-8")).decode("ascii") for l in lines]
            }
        elif ktype == "zset":
            lines = redis_lines("ZRANGE", key, "0", "-1", "WITHSCORES")
            items = []
            for i in range(0, len(lines), 2):
                member = lines[i]
                score = float(lines[i+1]) if i+1 < len(lines) else 0.0
                items.append({
                    "member": base64.b64encode(member.encode("utf-8")).decode("ascii"),
                    "score": score
                })
            state["keys"][key] = {"type": "zset", "value": items}
        else:
            print(f"  Skipping {key} (unknown type: {ktype})", file=sys.stderr)
    except Exception as e:
        print(f"  Error exporting {key}: {e}", file=sys.stderr)

# Summary
print(f"\nExported {len(state['keys'])} keys", file=sys.stderr)
for important in ["c3po:agents", "c3po:api_keys", "c3po:key_ids"]:
    if important in state["keys"]:
        count = len(state["keys"][important].get("value", {}))
        print(f"  {important}: {count} entries", file=sys.stderr)

with open(outpath, "w") as f:
    json.dump(state, f, indent=2)

print(f"\nState written to {outpath}", file=sys.stderr)
PYEOF

SIZE=$(du -h "$TMPDIR/state.json" | cut -f1)
log "State dump: ${SIZE}"

# ---- Step 3: Transfer to new server ----------------------------------------
step "Step 3: Transfer to new server"

rsync -avz -e "ssh -o StrictHostKeyChecking=accept-new" \
    "$TMPDIR/state.json" \
    "${NEW_USER}@${NEW_HOST}:${NEW_DIR}/state.json"

log "State file transferred"

# ---- Step 4: Update secrets on new server -----------------------------------
step "Step 4: Update secrets on new server"

# Read the current REDIS_PASSWORD from the new server (keep it — it's for local Redis)
NEW_REDIS_PW=$(new_ssh "grep '^REDIS_PASSWORD=' ${NEW_DIR}/.env | cut -d= -f2-")

log "Writing updated .env on new server..."
new_ssh "cat > ${NEW_DIR}/.env" <<EOF
REDIS_PASSWORD=${NEW_REDIS_PW}
C3PO_SERVER_SECRET=${SERVER_SECRET}
C3PO_ADMIN_KEY=${ADMIN_KEY}
C3PO_PROXY_BEARER_TOKEN=${PROXY_TOKEN}
EOF

new_ssh "chmod 600 ${NEW_DIR}/.env"
log "Secrets updated (kept new Redis password, applied old auth secrets)"

# ---- Step 5: Import state on new server -------------------------------------
step "Step 5: Import state into new server's Redis"

new_ssh "bash -s" <<'REMOTE_IMPORT'
set -euo pipefail

NEW_DIR="/home/exedev/c3po"
STATE_FILE="${NEW_DIR}/state.json"

if [ ! -f "$STATE_FILE" ]; then
    echo "ERROR: state.json not found at $STATE_FILE" >&2
    exit 1
fi

# Find the redis container
REDIS_CONTAINER=$(docker ps --format '{{.Names}}' | grep redis | grep c3po | head -1)
if [ -z "$REDIS_CONTAINER" ]; then
    echo "ERROR: c3po Redis container not running" >&2
    exit 1
fi

REDIS_PW=$(grep '^REDIS_PASSWORD=' "${NEW_DIR}/.env" | cut -d= -f2-)

python3 - "$STATE_FILE" "$REDIS_CONTAINER" "$REDIS_PW" <<'PYEOF'
import json, sys, base64, subprocess, io

state_file = sys.argv[1]
container = sys.argv[2]
redis_pw = sys.argv[3]

with open(state_file) as f:
    state = json.load(f)

assert state["version"] == 1, f"Unknown version: {state['version']}"

print(f"Importing {len(state['keys'])} keys...", file=sys.stderr)

# Build Redis protocol commands
buf = io.BytesIO()
cmd_count = 0

def write_cmd(*parts):
    global cmd_count
    encoded = []
    for p in parts:
        if isinstance(p, str):
            p = p.encode("utf-8")
        encoded.append(p)
    buf.write(f"*{len(encoded)}\r\n".encode())
    for p in encoded:
        buf.write(f"${len(p)}\r\n".encode())
        buf.write(p)
        buf.write(b"\r\n")
    cmd_count += 1

# AUTH first
if redis_pw:
    write_cmd("AUTH", redis_pw)

skipped = 0
for key, info in state["keys"].items():
    ktype = info["type"]
    
    # Skip ephemeral keys
    if key.startswith("c3po:rate:") or key.startswith("c3po:notify:"):
        skipped += 1
        continue
    
    if ktype == "string":
        val = base64.b64decode(info["value"]) if info["value"] else b""
        write_cmd("SET", key, val)
    elif ktype == "hash":
        for hk, hv in info["value"].items():
            write_cmd("HSET", key, hk, base64.b64decode(hv))
    elif ktype == "list":
        for item in info["value"]:
            write_cmd("RPUSH", key, base64.b64decode(item))
    elif ktype == "set":
        for item in info["value"]:
            write_cmd("SADD", key, base64.b64decode(item))
    elif ktype == "zset":
        for item in info["value"]:
            member = base64.b64decode(item["member"])
            score = str(item["score"])
            write_cmd("ZADD", key, score, member)

print(f"Sending {cmd_count} commands (skipped {skipped} ephemeral keys)...", file=sys.stderr)

# Pipe into redis-cli via docker exec
result = subprocess.run(
    ["docker", "exec", "-i", container, "redis-cli", "--pipe"],
    input=buf.getvalue(),
    capture_output=True
)

stdout = result.stdout.decode()
stderr = result.stderr.decode()
if stdout.strip():
    print(f"  {stdout.strip()}", file=sys.stderr)
if result.returncode != 0:
    print(f"  ERROR: {stderr}", file=sys.stderr)
    sys.exit(1)

print("Import complete!", file=sys.stderr)
PYEOF

echo "Cleaning up state file..."
rm -f "$STATE_FILE"
REMOTE_IMPORT

log "State imported successfully"

# ---- Step 6: Restart coordinator to pick up new secrets ---------------------
step "Step 6: Restart coordinator with migrated secrets"

new_ssh "sudo systemctl restart c3po"
log "Waiting for coordinator to come up..."
sleep 15

HEALTH=$(new_ssh "curl -sf http://localhost:8000/api/health" || echo "FAILED")
log "Health check: $HEALTH"

# ---- Step 7: Verify ---------------------------------------------------------
step "Step 7: Verify migration"

log "Checking agent list..."
new_ssh "curl -sf -H 'Authorization: Bearer ${SERVER_SECRET}.${ADMIN_KEY}' http://localhost:8000/admin/agents" | python3 -m json.tool 2>/dev/null || warn "Could not list agents (admin endpoint may need /api prefix)"

log "Checking API keys..."
KEY_COUNT=$(new_ssh "docker exec \$(docker ps --format '{{.Names}}' | grep redis | grep c3po | head -1) redis-cli -a \$(grep REDIS_PASSWORD /home/exedev/c3po/.env | cut -d= -f2-) --no-auth-warning HLEN c3po:api_keys" 2>/dev/null || echo "?")
log "API keys in Redis: ${KEY_COUNT}"

AGENT_COUNT=$(new_ssh "docker exec \$(docker ps --format '{{.Names}}' | grep redis | grep c3po | head -1) redis-cli -a \$(grep REDIS_PASSWORD /home/exedev/c3po/.env | cut -d= -f2-) --no-auth-warning HLEN c3po:agents" 2>/dev/null || echo "?")
log "Agents in Redis: ${AGENT_COUNT}"

# ---- Done -------------------------------------------------------------------
step "Migration complete!"
echo ""
log "New coordinator: http://golf-pegasus.exe.xyz:8000"
log "Admin token:     ${SERVER_SECRET}.${ADMIN_KEY}"
log "Health:          $HEALTH"
echo ""
warn "Next steps:"
echo "  1. Verify agents can connect to the new URL"
echo "  2. Update DNS or re-enroll agents:"
echo "     python3 setup.py --enroll http://golf-pegasus.exe.xyz:8000 '${SERVER_SECRET}.${ADMIN_KEY}'"
echo "  3. When satisfied, decommission old server"
echo ""
