---
name: c3po
description: Multi-agent coordination - use /c3po setup to configure, /c3po status to check connection, /c3po send to message other agents
---

# /c3po

Multi-agent coordination for Claude Code instances.

## Usage

- `/c3po setup` - Configure C3PO coordinator connection (interactive)
- `/c3po status` - Check connection and list online agents
- `/c3po agents` - List all agents with their status
- `/c3po send <agent> <message>` - Send a quick message to another agent

## Implementation

When the user runs this skill, parse the command and use the appropriate MCP tools from the c3po server:

### `/c3po setup`

Guide the user through configuring their C3PO coordinator connection. This is an interactive process.

1. Ask for the coordinator URL (e.g., `http://nas.local:8420`)
2. Test connectivity using curl (NOT WebFetch - it has network restrictions):
   ```bash
   curl -s <url>/api/health
   ```
   Expected response: `{"status":"ok","agents_online":N}`
3. Ask for an agent ID (suggest hostname/project format as default)
4. Configure the MCP server using `claude mcp add`:
   ```bash
   claude mcp add c3po <url>/mcp -t http -s user -H "X-Agent-ID: <agent-id>"
   ```
5. Verify the configuration with `claude mcp list`

IMPORTANT: Always use Bash with curl for HTTP requests, never use WebFetch (it runs in a sandbox with different network access).

Output format on success:
```
C3PO Setup Complete!
  Coordinator: http://nas.local:8420
  Machine ID: macbook (project added automatically per-session)

Next steps:
  1. Restart Claude Code to connect
  2. Use 'list_agents' to see online agents
  3. Run '/c3po status' to check connection
```

Note: Users can also run `claude --init` to trigger the Setup hook which provides a similar interactive experience.

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
2. Display all agents with their status and last seen time

Output format:
```
Registered Agents:
  agent-1: online (last seen: just now)
  agent-2: offline (last seen: 5 minutes ago)
  agent-3: online (last seen: just now)
```

### `/c3po send <agent> <message>`

1. Call `send_request` tool with target=agent and message=message
2. Call `wait_for_response` with the returned request_id and a 60s timeout
3. Display the response or timeout message

Example:
```
User: /c3po send meshtastic "What nodes are online?"

Response:
Sent request to meshtastic. Waiting for response...
Response from meshtastic: "Nodes online: node-1234, node-5678"
```

## Environment Variables

The coordinator URL and agent ID components are configured via environment variables:

- `C3PO_COORDINATOR_URL` - Coordinator URL (default: `http://localhost:8420`)
- `C3PO_AGENT_ID` - Machine identifier, base of agent ID (default: hostname)
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
