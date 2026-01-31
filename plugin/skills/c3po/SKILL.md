---
name: c3po
description: Multi-agent coordination - use /c3po setup to configure, /c3po status to check connection, /c3po send to message other agents, /c3po auto for low-token listening
---

# /c3po

Multi-agent coordination for Claude Code instances.

## Usage

- `/c3po setup` - Configure C3PO coordinator connection (interactive)
- `/c3po status` - Check connection and list online agents
- `/c3po agents` - List all agents with their status
- `/c3po send <agent> <message>` - Send a quick message to another agent
- `/c3po auto` - Listen for incoming messages in a loop (low token cost)

## Implementation

When the user runs this skill, parse the command and use the appropriate MCP tools from the c3po server:

### `/c3po setup`

Guide the user through configuring their C3PO coordinator connection. This is an interactive process.

IMPORTANT: Always use Bash with curl for HTTP requests, never use WebFetch (it runs in a sandbox with different network access).

**Step 1: Coordinator URL**
Ask for the coordinator URL (e.g., `https://mcp.qerk.be` or `http://nas.local:8420`) and test connectivity:
```bash
curl -s <url>/api/health
```
Expected response: `{"status":"ok","agents_online":N}`

**Step 2: Machine name**
Ask the user what machine name to use. The default should be the hostname. This becomes the first part of the agent ID (`machine/project`). The project part is added automatically per-session from the working directory.

**Step 3: Authentication mode**
There are two authentication paths depending on the coordinator setup:

**A) OAuth (public coordinators with HTTPS, e.g., `https://mcp.qerk.be`):**
MCP authentication uses OAuth 2.1 via GitHub, handled automatically by the auth proxy. When Claude Code first connects, it will open a browser for GitHub login. No manual token configuration needed for MCP.

However, hooks (SessionStart/End/Stop) use a separate **hook secret** for REST API calls. Ask the user for the hook secret (displayed during coordinator deployment). If they don't have it, warn that hooks won't be able to register/unregister the agent.

**B) Headless (no browser available, e.g., SSH servers, `claude -p` pipelines):**
If the user is on a headless system with no browser, use the `/mcp-headless` endpoint instead. This authenticates via hook secret (no OAuth/browser needed). The hook secret is **required** for headless mode.

**C) Direct (local/private coordinators with HTTP, e.g., `http://nas.local:8420`):**
No OAuth proxy involved. MCP connects directly to the coordinator. Ask for the hook secret if the coordinator requires authentication.

**Step 4: Configure MCP server**
Remove any existing config first, then add with all required headers:

For **OAuth (interactive)** mode:
```bash
claude mcp remove c3po 2>/dev/null
claude mcp add c3po <url>/mcp -t http -s user \
  -H "X-Machine-Name: <machine_name>" \
  -H "X-Project-Name: \${C3PO_PROJECT_NAME:-\${PWD##*/}}" \
  -H "X-Session-ID: \${C3PO_SESSION_ID:-\$\$}" \
  -H "X-C3PO-Hook-Secret: <hook_secret>"
```

For **headless** mode (hook secret required):
```bash
claude mcp remove c3po 2>/dev/null
claude mcp add c3po <url>/mcp-headless -t http -s user \
  -H "X-Machine-Name: <machine_name>" \
  -H "X-Project-Name: \${C3PO_PROJECT_NAME:-\${PWD##*/}}" \
  -H "X-Session-ID: \${C3PO_SESSION_ID:-\$\$}" \
  -H "X-C3PO-Hook-Secret: <hook_secret>"
```

For **direct** mode (local coordinator, no OAuth):
```bash
claude mcp remove c3po 2>/dev/null
claude mcp add c3po <url>/mcp -t http -s user \
  -H "X-Machine-Name: <machine_name>" \
  -H "X-Project-Name: \${C3PO_PROJECT_NAME:-\${PWD##*/}}" \
  -H "X-Session-ID: \${C3PO_SESSION_ID:-\$\$}" \
  -H "X-C3PO-Hook-Secret: <hook_secret>"
```

If no hook secret was provided, omit the `X-C3PO-Hook-Secret` header.

**Step 5: Verify**
```bash
claude mcp list
```

Output format on success:
```
C3PO Setup Complete!
  Coordinator: https://mcp.qerk.be
  Machine name: macbook (project added automatically per-session)
  Auth mode: OAuth (interactive) / Headless (hook secret) / Direct

Restart Claude Code to connect.
```

### `/c3po status`

1. Call `ping` tool to verify coordinator connection
2. Call `list_agents` tool to get online agents
3. Display connection status and agent count

Output format:
```
C3PO Status:
  Coordinator: [URL] (connected/unavailable)
  Agent ID: [machine/project]
  Online agents: [count]
    - macbook/project-a (online)
    - server/homeassistant (offline)
```

### `/c3po agents`

1. Call `list_agents` tool
2. Display all agents with their status, description, and last seen time

Output format:
```
Registered Agents:
  agent-1: online - "Home automation controller" (last seen: just now)
  agent-2: offline - "Media server manager" (last seen: 5 minutes ago)
  agent-3: online (last seen: just now)
```

Show the description in quotes after the status if the agent has one. Omit it if the description is empty.

### `/c3po send <agent> <message>`

1. Call `send_request` tool with target_agent=agent and message=message
2. Call `wait_for_message` with type="response" and `timeout=3600` (user will Ctrl+C if needed)
3. Display the response or timeout message

Example:
```
User: /c3po send meshtastic "What nodes are online?"

Response:
Sent request to meshtastic. Waiting for response...
Response from meshtastic: "Nodes online: node-1234, node-5678"
```

### `/c3po auto`

Enter auto-listen mode: a tight loop that waits for incoming messages with minimal token usage.

1. Call `set_description` with a brief description of what this agent/project does (infer from the project context — e.g., repo name, README, or working directory)
2. Print: `Auto-listen mode active. Waiting for messages... (Ctrl+C to exit)`
3. Call `wait_for_message` with `timeout=3600`
4. If messages received: process each message fully:
   - For requests: read the request, use any tools needed to research an answer, then call `respond_to_request` with your response
   - For responses: display the response content to the user
   - After processing all messages, go back to step 3
5. If timeout (no messages): print ONLY `Still listening...` and go back to step 3

**Critical rules for auto-listen mode:**
- ALWAYS loop back to step 3. Never exit the loop unless the user interrupts with Ctrl+C.
- On timeout, print ONLY "Still listening..." — no extra commentary, no suggestions, no questions.
- Do NOT ask the user for input during the loop. Process everything autonomously.
- When processing requests, use your full tool access to research thorough answers before responding.
- Always use `timeout=3600` (1 hour — maximum token savings; the user will Ctrl+C if needed).

## Environment Variables

The coordinator URL and agent ID components are configured via environment variables:

- `C3PO_COORDINATOR_URL` - Coordinator URL (default: `http://localhost:8420`)
- `C3PO_MACHINE_NAME` - Machine identifier, base of agent ID (default: hostname)
- `C3PO_PROJECT_NAME` - Project name override (default: current directory name)
- `C3PO_SESSION_ID` - Session identifier for same-session detection

The full agent ID is constructed as `{machine}/{project}` (e.g., `macbook/myproject`).

## Error Handling

- If coordinator is unavailable, display a friendly message suggesting to check the URL
- If target agent not found, list available agents as suggestions
- If request times out, suggest checking if the target agent is online

## Examples

Check who's online:
```
User: /c3po status

C3PO Status:
  Coordinator: http://localhost:8420 (connected)
  Agent ID: raspi/homeassistant
  Online agents: 3
    - raspi/homeassistant (online)
    - raspi/meshtastic (online)
    - server/mediaserver (offline)
```

Ask another agent for help:
```
User: /c3po send meshtastic "What MQTT topics do you publish to?"

Sent request to meshtastic. Waiting for response...
Response from meshtastic: "I publish to mesh/node/# for node status and mesh/msg/# for messages."
```
