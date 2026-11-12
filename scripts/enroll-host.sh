#!/usr/bin/env bash
# Enroll a remote host with the production coordinator and write its
# ~/.claude/c3po-credentials.json (0600), without the admin key or the new API
# token showing up in this terminal or any chat transcript.
#
#   bash scripts/enroll-host.sh <machine> [ssh-target...]
#
#   machine     the key's agent pattern becomes "<machine>/*"
#   ssh-target  command that reaches the host (default: the exe.dev relay,
#               "ssh exe.dev ssh <machine>"; e.g. "ssh user@host" otherwise)
#
# The admin token is read on the prod VM from ~/c3po/.env (the c3po.service
# EnvironmentFile). Only the key_id, pattern and a validate status are printed.
# nginx allows ~2 admin calls/min per IP; on a 429 wait 35s and rerun.
set -euo pipefail

machine=${1:?usage: enroll-host.sh <machine> [ssh-target...]}
shift
if [ $# -eq 0 ]; then set -- ssh exe.dev ssh "$machine"; fi
prod=${C3PO_PROD_SSH:-exedev@golf-pegasus.exe.xyz}
url=https://mcp.qerk.be

# 1. Mint the key on the prod VM (the admin token never leaves it).
# 2. Wrap the credentials in a script that writes them and validates.
# 3. Run that script on the host via stdin (no nested quoting through relays).
ssh "$prod" bash -s -- "$machine" "$url" <<'PROD' |
set -euo pipefail
set -a; . ~/c3po/.env; set +a
curl -sSf -X POST "$2/admin/api/keys" \
  -H "Authorization: Bearer $C3PO_SERVER_SECRET.$C3PO_ADMIN_KEY" \
  -H 'Content-Type: application/json' \
  -d "{\"agent_pattern\":\"$1/*\",\"description\":\"$1 host\"}"
PROD
python3 -c '
import json, shlex, sys
r = json.load(sys.stdin)
url, machine = sys.argv[1], sys.argv[2]
key_id, token, pattern = r["key_id"], r["api_key"], r["agent_pattern"]
creds = json.dumps({"coordinator_url": url, "api_token": token,
                    "key_id": key_id, "agent_pattern": pattern}, indent=2)
print(f"enrolled key_id={key_id} pattern={pattern}", file=sys.stderr)
print("set -eu; umask 077; mkdir -p ~/.claude")
print("cat > ~/.claude/c3po-credentials.json.new <<\"C3PO_CREDS\"")
print(creds)
print("C3PO_CREDS")
print("chmod 600 ~/.claude/c3po-credentials.json.new")
print("mv ~/.claude/c3po-credentials.json.new ~/.claude/c3po-credentials.json")
print("tok=" + shlex.quote(token))
print(f"printf \"validate: HTTP \"; curl -s -o /dev/null -w \"%{{http_code}}\\n\" -H \"Authorization: Bearer $tok\" \"{url}/agent/api/validate?machine_name={machine}\"")
' "$url" "$machine" |
"$@" bash -s
