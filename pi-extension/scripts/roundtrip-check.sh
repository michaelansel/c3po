#!/usr/bin/env bash
# Round trip through a real C-3PO coordinator with a real model:
#   sender -> pi + this extension -> model replies with c3po_reply -> sender receives it.
# Uses a THROWAWAY api key (agent_pattern must cover $PI_AGENT_ID and $SENDER_ID) kept in a 0600 file.
#
#   CREDS=/path/credentials.json bash roundtrip-check.sh [model-substring]
#
# Same model/endpoint knobs, scratch dir and container sandbox as model-check.sh.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EXT_DIR="$(dirname "$HERE")"

WANT="${1:-glm-5p3-flash}"
CREDS="${CREDS:?set CREDS to a credentials.json holding a throwaway key}"
BASE_URL="${BASE_URL:-https://llm.int.exe.xyz/v1}"
API_KEY="${API_KEY:-none}"
PI_VERSION="${PI_VERSION:-1.5.2}"
PI_AGENT_ID="${PI_AGENT_ID:-$(hostname -s)/pi-roundtrip}"
SENDER_ID="${SENDER_ID:-$(hostname -s)/pi-sender}"
SANDBOX="${SANDBOX:-$(command -v docker >/dev/null 2>&1 && echo docker || echo none)}"
IMAGE="${IMAGE:-node:22-slim}"
MCP_PATH="${C3PO_MCP_PATH:-/agent/mcp}"
[ -r "$CREDS" ] || { echo "cannot read $CREDS"; exit 2; }
CREDS="$(cd "$(dirname "$CREDS")" && pwd)/$(basename "$CREDS")"

ROOT="$(mktemp -d /tmp/pi-roundtrip.XXXXXX)"; echo "scratch: $ROOT"; cd "$ROOT"
MODEL="$(curl -fsS -H "Authorization: Bearer $API_KEY" "$BASE_URL/models" \
  | node -e 'let s="";process.stdin.on("data",d=>s+=d).on("end",()=>{const w=process.argv[1];const ids=JSON.parse(s).data.map(m=>m.id).filter(i=>i.includes(w));console.log(ids.find(i=>i.startsWith("fireworks/"))||ids[0]||"")})' "$WANT")"
[ -n "$MODEL" ] || { echo "no model matching '$WANT'"; exit 2; }
echo "model: $MODEL  pi: $PI_AGENT_ID  sender: $SENDER_ID  sandbox: $SANDBOX"

mkdir pi && (cd pi && npm init -y >/dev/null && npm i --silent "@earendil-works/pi-coding-agent@$PI_VERSION")
CLI="$ROOT/pi/node_modules/@earendil-works/pi-coding-agent/dist/cli.js"
mkdir -p agent work home
cat > agent/models.json <<JSON
{ "providers": { "check": { "baseUrl": "$BASE_URL", "api": "openai-completions", "apiKey": "$API_KEY", "models": [ { "id": "$MODEL" } ] } } }
JSON

export C3PO_CREDENTIALS_FILE="$CREDS" C3PO_MCP_PATH="$MCP_PATH" PI_AGENT_ID SENDER_ID
PIARGS=(--mode rpc --provider check --model "$MODEL" --approve --no-session -e "$EXT_DIR/extensions/c3po.ts")
if [ "$SANDBOX" = docker ]; then
  CNAME="pi-roundtrip-$$"
  trap 'docker kill "$CNAME" >/dev/null 2>&1 || true' EXIT
  # Mounted: scratch (rw), the extension dir (ro), the one credentials file (ro). Network stays on (model + coordinator).
  PI_CMD="$(node -e 'console.log(JSON.stringify(process.argv.slice(1)))' docker run -i --rm --name "$CNAME" --user "$(id -u):$(id -g)" \
    -e HOME="$ROOT/home" -e PI_CODING_AGENT_DIR="$ROOT/agent" -e C3PO_CREDENTIALS_FILE="$CREDS" -e C3PO_MCP_PATH="$MCP_PATH" -e C3PO_AGENT_ID="$PI_AGENT_ID" \
    -v "$ROOT:$ROOT" -v "$EXT_DIR:$EXT_DIR:ro" -v "$CREDS:$CREDS:ro" -w "$ROOT/work" "$IMAGE" node "$CLI" "${PIARGS[@]}")"
else
  PI_CMD="$(PI_CODING_AGENT_DIR="$ROOT/agent" node -e 'console.log(JSON.stringify(process.argv.slice(1)))' env "PI_CODING_AGENT_DIR=$ROOT/agent" "C3PO_AGENT_ID=$PI_AGENT_ID" node "$CLI" "${PIARGS[@]}")"
fi
export PI_CMD
cd work
EXPECT="${EXPECT:-391}" timeout 400 node "$EXT_DIR/test/roundtrip.ts" && echo "RESULT: PASS ($MODEL)" || { echo "RESULT: FAIL ($MODEL); scratch $ROOT"; exit 1; }
