# C-3PO for pi

A [pi](https://pi.dev) extension that puts a pi session on the C-3PO agent mesh, the way the Claude Code
plugin hooks do for Claude Code. **Experimental** (the coordinator itself is unchanged).

What it does:

- Registers with the coordinator at session start (`/agent/api/register`) and unregisters with `keep=true`
  at shutdown, so queued messages survive.
- Long-polls `/agent/api/wait` and injects each message as a user turn. If pi is busy it goes in as a
  follow-up. Delivery is once per message even though `/wait` keeps returning unacked ones.
- Acks a message only after the turn that handled it has *settled* (`agent_settled`). A crash mid-turn
  means redelivery, not loss.
- Tools: `c3po_reply` (answer a message by id), `c3po_send`, `c3po_agents`. Plain reply text is **not**
  sent to the other agent, and the injected turn says so.
- Keeps the agent `online` with a 5-minute MCP ping (REST waiting alone shows as `watching`).
- `C3PO_STATUS_FILE`: JSON `{state: idle|busy|starting|stopped, pending, dropped, ...}` for a launcher to read.
- `/c3po` command: connection status.

## Install

```bash
pi install git:github.com/michaelansel/c3po   # then enable pi-extension/ (see pi docs on package subpaths)
# or, from a checkout:
pi -e /path/to/c3po/pi-extension/extensions/c3po.ts
```

Credentials come from `~/.claude/c3po-credentials.json` (written by `python3 setup.py --enroll ...`, the
same file the Claude hooks use) or `C3PO_COORDINATOR_URL` + `C3PO_API_TOKEN`.

## Configuration (env)

| Variable | Default | |
|---|---|---|
| `C3PO_AGENT_ID` | `<machine>/<project>` | Full id. Otherwise `C3PO_MACHINE_NAME` (hostname) + `C3PO_PROJECT_NAME` (cwd name) |
| `C3PO_MCP_PATH` | `/agent/mcp` | `/mcp` against a bare local coordinator |
| `C3PO_ALLOW_FROM` | accept all | Comma-separated sender globs, e.g. `Michaels-Mac-mini/*`. Others are acked unseen |
| `C3PO_STATUS_FILE` | off | Status JSON path |
| `C3PO_CREDENTIALS_FILE` | `~/.claude/c3po-credentials.json` | |
| `C3PO_DISABLE=1` | | Load and do nothing |
| `C3PO_DEBUG=1` | | Log to stderr |

## Safety

pi has no permission prompts, and a C-3PO message becomes a user turn with the session's full tool access.
Run it in a container, give it only what the job needs, and use `C3PO_ALLOW_FROM`.
Agents on the mesh are mutually trusting today (no sender allowlist exists on the Claude side either).

## Tests

```bash
cd pi-extension
node --test test/inbox.test.ts          # unit, no network (Node >= 22.18)

# end to end: real pi (RPC) + local coordinator + mock model
python -m coordinator.server            # in another shell, with Redis up, dev mode
npm i @earendil-works/pi-coding-agent   # anywhere
C3PO_TEST_LIVE=1 C3PO_TEST_URL=http://127.0.0.1:8420 PI_CLI=<path>/dist/cli.js node --test test/e2e.test.ts
```

Not covered yet: real-model tool calling (GLM via OpenRouter), the TUI under tmux, pi 1.x upgrades.

## Container image

`container/Dockerfile` builds pi (pinned, `PI_VERSION`) plus this extension. Nothing secret or project-specific is baked in.

```bash
docker build -f pi-extension/container/Dockerfile -t c3po-pi pi-extension

docker run -it --rm --name pi-lookup \
  -e PI_MODEL=fireworks/glm-5p3-flash \
  -e C3PO_AGENT_ID=$(hostname -s)/pi-lookup \
  -v ~/.config/c3po/credentials.json:/run/c3po/credentials.json:ro \
  -v "$PWD":/work \
  c3po-pi
```

Environment (see `container/entrypoint.sh`): `PI_MODEL` (required), `PI_BASE_URL` (default the exe.dev gateway), `PI_API_KEY_FILE` or `PI_API_KEY`, `PI_OPENROUTER_ONLY=<provider>` (pins an OpenRouter provider with no fallbacks, `data_collection: deny`, `zdr: true`), `C3PO_AGENT_ID`, `C3PO_DISABLE=1`. Extra arguments go to pi.

The credentials file is read as the container user (`node`, uid 1000). If the host file belongs to another uid, add `--user $(id -u):$(id -g)`; the entrypoint then keeps its state in a temp dir.

Pi runs with `--approve`, so the model's shell can read anything mounted into the container, including the C-3PO credentials file. Mount only a key scoped to this agent's pattern, and only the folder it should work in.
