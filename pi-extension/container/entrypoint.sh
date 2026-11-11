#!/usr/bin/env bash
# Writes pi's models.json from environment, then starts pi with the C-3PO extension.
#
# Required:  PI_MODEL                model id as the endpoint names it (e.g. fireworks/glm-5p3-flash)
# Optional:  PI_BASE_URL             OpenAI-style endpoint (default: the exe.dev gateway)
#            PI_API_KEY_FILE         file holding the key (preferred: stays out of `docker inspect`); else PI_API_KEY; else none
#            PI_OPENROUTER_ONLY      provider slug to pin on OpenRouter (e.g. fireworks); also sets
#                                    allow_fallbacks=false, data_collection=deny, zdr=true
#            C3PO_AGENT_ID           e.g. mac-mini/pi-lookup (or C3PO_MACHINE_NAME + C3PO_PROJECT_NAME)
#            C3PO_DISABLE=1          run pi without the C-3PO extension
# C-3PO credentials: mount a credentials.json at $C3PO_CREDENTIALS_FILE (default /run/c3po/credentials.json).
# Extra arguments are passed to pi.
set -euo pipefail

: "${PI_MODEL:?set PI_MODEL}"
BASE_URL="${PI_BASE_URL:-https://llm.int.exe.xyz/v1}"
KEY="${PI_API_KEY:-none}"
if [ -n "${PI_API_KEY_FILE:-}" ]; then KEY="$(tr -d '\r\n' < "$PI_API_KEY_FILE")"; fi

EXT=()
if [ "${C3PO_DISABLE:-}" != 1 ]; then
  [ -r "$C3PO_CREDENTIALS_FILE" ] || { echo "c3po-pi: cannot read $C3PO_CREDENTIALS_FILE (mount it, or set C3PO_DISABLE=1)" >&2; exit 2; }
  EXT=(-e /opt/c3po-pi/extensions/c3po.ts)
fi

mkdir -p "$PI_CODING_AGENT_DIR"
BASE_URL="$BASE_URL" KEY="$KEY" MODEL="$PI_MODEL" ONLY="${PI_OPENROUTER_ONLY:-}" node -e '
const e = process.env;
const p = { baseUrl: e.BASE_URL, api: "openai-completions", apiKey: e.KEY, models: [{ id: e.MODEL }] };
if (e.ONLY) p.compat = { openRouterRouting: { only: [e.ONLY], allow_fallbacks: false, data_collection: "deny", zdr: true } };
process.stdout.write(JSON.stringify({ providers: { c3po_pi: p } }, null, 2));
' > "$PI_CODING_AGENT_DIR/models.json"
chmod 600 "$PI_CODING_AGENT_DIR/models.json"

exec pi --provider c3po_pi --model "$PI_MODEL" --approve "${EXT[@]}" "$@"
