#!/usr/bin/env bash
# Real-model smoke test for pi: can the model drive pi's tools through a multi-step fix?
# Runs in a scratch dir, touches nothing else (own PI_CODING_AGENT_DIR, own pi install).
#
# Intended for an exe.dev VM, where https://llm.int.exe.xyz needs no API key.
# Anywhere else, set BASE_URL and API_KEY (an OpenAI-shaped endpoint) and MODEL.
#
#   bash model-check.sh [model-id-substring]      default: glm-5p3-flash
set -euo pipefail

WANT="${1:-glm-5p3-flash}"
BASE_URL="${BASE_URL:-https://llm.int.exe.xyz/v1}"
API_KEY="${API_KEY:-none}"
PI_VERSION="${PI_VERSION:-1.5.2}"

ROOT="$(mktemp -d /tmp/pi-model-check.XXXXXX)"
echo "scratch: $ROOT"
cd "$ROOT"

# 1. Resolve the model id the endpoint actually lists (ids are provider-namespaced and change).
MODEL="$(curl -fsS -H "Authorization: Bearer $API_KEY" "$BASE_URL/models" \
  | node -e 'let s="";process.stdin.on("data",d=>s+=d).on("end",()=>{const w=process.argv[1];const ids=JSON.parse(s).data.map(m=>m.id).filter(i=>i.includes(w));console.log(ids.find(i=>i.startsWith("fireworks/"))||ids[0]||"")})' "$WANT")"
[ -n "$MODEL" ] || { echo "no model matching '$WANT' at $BASE_URL"; exit 2; }
echo "model: $MODEL"

# 2. Own pi install and config, pinned.
mkdir pi && (cd pi && npm init -y >/dev/null && npm i --silent "@earendil-works/pi-coding-agent@$PI_VERSION")
PI="node $ROOT/pi/node_modules/@earendil-works/pi-coding-agent/dist/cli.js"
export PI_CODING_AGENT_DIR="$ROOT/agent"
mkdir -p "$PI_CODING_AGENT_DIR"
cat > "$PI_CODING_AGENT_DIR/models.json" <<JSON
{ "providers": { "check": { "baseUrl": "$BASE_URL", "api": "openai-completions", "apiKey": "$API_KEY",
  "models": [ { "id": "$MODEL" } ] } } }
JSON

# 3. A tiny repo with a failing test.
mkdir work && cd work
cat > sum.js <<'JS'
export function sumTo(n) { let t = 0; for (let i = 1; i < n; i++) t += i; return t; }
JS
cat > sum.test.js <<'JS'
import test from "node:test"; import assert from "node:assert/strict"; import { sumTo } from "./sum.js";
test("sumTo includes n", () => { assert.equal(sumTo(4), 10); });
test("sumTo(0) is 0", () => { assert.equal(sumTo(0), 0); });
JS
echo '{"type":"module"}' > package.json
git init -q && git add -A && git -c user.name=t -c user.email=t@t commit -qm init
! node --test >/dev/null 2>&1 || { echo "setup bug: tests should fail first"; exit 3; }

# 4. Ask for a multi-step job: run tests, read code, fix, rerun, then add a regression test.
START=$(date +%s)
set +e
timeout 300 $PI -p --provider check --model "$MODEL" --approve --no-session \
  "node --test fails in this repo. Run it, find the bug in sum.js, fix it, rerun the tests until they pass, then add a test for sumTo(1) === 1 to sum.test.js and run them once more. Reply with one sentence." \
  > "$ROOT/pi.out" 2> "$ROOT/pi.err"
RC=$?
set -e
END=$(date +%s)

echo "--- pi exit $RC after $((END-START))s; final output:"; tail -c 600 "$ROOT/pi.out"
echo "--- diff:"; git diff --stat; git diff -- sum.js | head -20
if node --test >/dev/null 2>&1 && grep -q "sumTo(1)" sum.test.js; then
  echo "RESULT: PASS ($MODEL)"; exit 0
fi
echo "RESULT: FAIL ($MODEL); see $ROOT/pi.out and $ROOT/pi.err"; exit 1
