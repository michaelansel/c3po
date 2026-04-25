#!/bin/bash
set -euo pipefail

# C3PO Redis State Migration
# Exports all C3PO state from an existing Redis instance and imports into the new one.
#
# Usage:
#   # Step 1: Export from old deployment (run on old server or with access to old Redis)
#   bash scripts/migrate-redis.sh export <old_redis_url> > c3po-state.json
#
#   # Step 2: Import into new deployment (run on new server)
#   bash scripts/migrate-redis.sh import < c3po-state.json
#
# The old_redis_url should be in the form: redis://:password@host:port
#
# Key patterns exported:
#   c3po:agents       - Agent registrations (hash)
#   c3po:api_keys     - API key hashes (hash) 
#   c3po:key_ids      - Key ID -> hash mappings (hash)
#   c3po:audit        - Audit log (list)
#   c3po:inbox:*      - Message inboxes (lists)
#   c3po:messages:*   - Message queues (lists)
#   c3po:acked:*      - Ack sets (sets)
#   c3po:msg:*        - Archived messages (strings)
#   c3po:blob:*       - Blob storage (strings)
#   c3po:notify:*     - Notification queues (lists, typically ephemeral)
#   c3po:rate:*       - Rate limit counters (sorted sets, ephemeral)

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'
log()  { echo -e "${GREEN}[migrate]${NC} $1" >&2; }
warn() { echo -e "${YELLOW}[migrate]${NC} $1" >&2; }
err()  { echo -e "${RED}[migrate]${NC} $1" >&2; }

COMMAND="${1:-help}"

case "$COMMAND" in
  export)
    REDIS_URL="${2:?Usage: $0 export <redis_url>}"
    log "Exporting C3PO state from $REDIS_URL ..."
    
    python3 - "$REDIS_URL" <<'PYEOF'
import json, sys, base64
import redis

url = sys.argv[1]
r = redis.from_url(url, decode_responses=False)

state = {"version": 1, "keys": {}}

# Scan all c3po:* keys
cursor = 0
while True:
    cursor, keys = r.scan(cursor, match="c3po:*", count=500)
    for key in keys:
        key_str = key.decode("utf-8")
        key_type = r.type(key).decode("utf-8")
        
        if key_type == "string":
            val = r.get(key)
            state["keys"][key_str] = {
                "type": "string",
                "value": base64.b64encode(val).decode("ascii") if val else None
            }
        elif key_type == "hash":
            hdata = r.hgetall(key)
            state["keys"][key_str] = {
                "type": "hash",
                "value": {
                    k.decode("utf-8"): base64.b64encode(v).decode("ascii")
                    for k, v in hdata.items()
                }
            }
        elif key_type == "list":
            ldata = r.lrange(key, 0, -1)
            state["keys"][key_str] = {
                "type": "list",
                "value": [base64.b64encode(v).decode("ascii") for v in ldata]
            }
        elif key_type == "set":
            sdata = r.smembers(key)
            state["keys"][key_str] = {
                "type": "set",
                "value": [base64.b64encode(v).decode("ascii") for v in sdata]
            }
        elif key_type == "zset":
            zdata = r.zrange(key, 0, -1, withscores=True)
            state["keys"][key_str] = {
                "type": "zset",
                "value": [
                    {"member": base64.b64encode(m).decode("ascii"), "score": s}
                    for m, s in zdata
                ]
            }
        else:
            print(f"Skipping unknown type {key_type} for {key_str}", file=sys.stderr)
    
    if cursor == 0:
        break

print(f"Exported {len(state['keys'])} keys", file=sys.stderr)

# Critical keys summary
for important in ["c3po:agents", "c3po:api_keys", "c3po:key_ids"]:
    if important in state["keys"]:
        count = len(state["keys"][important]["value"])
        print(f"  {important}: {count} entries", file=sys.stderr)

json.dump(state, sys.stdout, indent=2)
PYEOF
    log "Export complete."
    ;;
  
  import)
    log "Importing C3PO state into local Redis..."
    
    # Get Redis password from .env
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
    source "$PROJECT_DIR/.env"
    
    REDIS_URL="redis://:${REDIS_PASSWORD}@localhost:$(docker compose -f "$PROJECT_DIR/docker-compose.prod.yml" port redis 6379 | cut -d: -f2)"
    
    # Actually, Redis is only accessible within the Docker network. Use docker exec.
    log "Reading state from stdin..."
    python3 - <<'PYEOF'
import json, sys, base64, subprocess

state = json.load(sys.stdin)
assert state["version"] == 1, f"Unknown version: {state['version']}"

print(f"Importing {len(state['keys'])} keys...", file=sys.stderr)

# Build redis-cli commands
cmds = []
for key, info in state["keys"].items():
    ktype = info["type"]
    
    # Skip ephemeral keys
    if key.startswith("c3po:rate:") or key.startswith("c3po:notify:"):
        print(f"  Skipping ephemeral: {key}", file=sys.stderr)
        continue
    
    if ktype == "string":
        val = base64.b64decode(info["value"]) if info["value"] else b""
        cmds.append(("SET", key, val))
    elif ktype == "hash":
        for hk, hv in info["value"].items():
            cmds.append(("HSET", key, hk, base64.b64decode(hv)))
    elif ktype == "list":
        for item in info["value"]:
            cmds.append(("RPUSH", key, base64.b64decode(item)))
    elif ktype == "set":
        for item in info["value"]:
            cmds.append(("SADD", key, base64.b64decode(item)))
    elif ktype == "zset":
        for item in info["value"]:
            member = base64.b64decode(item["member"])
            score = item["score"]
            cmds.append(("ZADD", key, str(score), member))

print(f"Generated {len(cmds)} Redis commands", file=sys.stderr)

# Write commands as Redis protocol
import io
buf = io.BytesIO()
for cmd in cmds:
    parts = []
    for p in cmd:
        if isinstance(p, str):
            p = p.encode("utf-8")
        parts.append(p)
    buf.write(f"*{len(parts)}\r\n".encode())
    for p in parts:
        buf.write(f"${len(p)}\r\n".encode())
        buf.write(p)
        buf.write(b"\r\n")

# Pipe to redis-cli via docker exec
result = subprocess.run(
    ["docker", "exec", "-i", "c3po-redis-1", "redis-cli", "--pipe"],
    input=buf.getvalue(),
    capture_output=True
)
print(result.stdout.decode(), file=sys.stderr)
if result.returncode != 0:
    print(f"Error: {result.stderr.decode()}", file=sys.stderr)
    sys.exit(1)

print("Import complete!", file=sys.stderr)
PYEOF
    log "Import complete."
    ;;
  
  help|*)
    echo "C3PO Redis State Migration Tool"
    echo ""
    echo "Usage:"
    echo "  $0 export <redis_url>   Export state to stdout (JSON)"
    echo "  $0 import               Import state from stdin (JSON)"
    echo ""
    echo "Migration workflow:"
    echo "  1. On old server: bash scripts/migrate-redis.sh export redis://:password@localhost:6379 > state.json"
    echo "  2. Copy state.json to this server"
    echo "  3. On this server: bash scripts/migrate-redis.sh import < state.json"
    echo ""
    echo "Critical state that will be migrated:"
    echo "  - Agent registrations (c3po:agents)"
    echo "  - API keys and key IDs (c3po:api_keys, c3po:key_ids)"
    echo "  - Message queues and inboxes"
    echo "  - Audit logs"
    echo "  - Archived messages"
    echo "  - Blob storage"
    echo ""
    echo "Ephemeral state (skipped during import):"
    echo "  - Rate limit counters (c3po:rate:*)"
    echo "  - Notification queues (c3po:notify:*)"
    ;;
esac
