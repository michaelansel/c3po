# /coordinate

Multi-agent coordination for Claude Code instances.

## Usage

- `/coordinate setup` - Configure C3PO coordinator connection (interactive)
- `/coordinate status` - Check connection and list online agents
- `/coordinate agents` - List all agents with their status
- `/coordinate send <agent> <message>` - Send a quick message to another agent

## Implementation

When the user runs this skill, parse the command and use the appropriate MCP tools from the c3po server:

### `/coordinate setup`

Guide the user through configuring their C3PO coordinator connection. This is an interactive process.

1. Ask for the coordinator URL (e.g., `http://nas.local:8420`)
2. Test connectivity to the coordinator using the `/api/health` endpoint
3. Ask for an agent ID (suggest the current folder name as default)
4. Configure the MCP server using `claude mcp add`:
   ```bash
   claude mcp add c3po <url>/mcp -t http -s user -H "X-Agent-ID: <agent-id>"
   ```
5. Verify the configuration works

Output format on success:
```
C3PO Setup Complete!
  Coordinator: http://nas.local:8420
  Agent ID: my-project

Next steps:
  1. Restart Claude Code to connect
  2. Use 'list_agents' to see online agents
  3. Run '/coordinate status' to check connection
```

Note: Users can also run `claude --init` to trigger the Setup hook which provides a similar interactive experience.

### `/coordinate status`

1. Call `ping` tool to verify coordinator connection
2. Call `list_agents` tool to get online agents
3. Display connection status and agent count

Output format:
```
C3PO Status:
  Coordinator: [URL] (connected/unavailable)
  Agent ID: [your-agent-id]
  Online agents: [count]
    - agent-1 (online)
    - agent-2 (offline)
```

### `/coordinate agents`

1. Call `list_agents` tool
2. Display all agents with their status and last seen time

Output format:
```
Registered Agents:
  agent-1: online (last seen: just now)
  agent-2: offline (last seen: 5 minutes ago)
  agent-3: online (last seen: just now)
```

### `/coordinate send <agent> <message>`

1. Call `send_request` tool with target=agent and message=message
2. Call `wait_for_response` with the returned request_id and a 60s timeout
3. Display the response or timeout message

Example:
```
User: /coordinate send meshtastic "What nodes are online?"

Response:
Sent request to meshtastic. Waiting for response...
Response from meshtastic: "Nodes online: node-1234, node-5678"
```

## Environment Variables

The coordinator URL and agent ID are configured via environment variables:

- `C3PO_COORDINATOR_URL` - Coordinator URL (default: `http://localhost:8420`)
- `C3PO_AGENT_ID` - Your agent identifier (default: current folder name)

## Error Handling

- If coordinator is unavailable, display a friendly message suggesting to check the URL
- If target agent not found, list available agents as suggestions
- If request times out, suggest checking if the target agent is online

## Examples

Check who's online:
```
User: /coordinate status

C3PO Status:
  Coordinator: http://localhost:8420 (connected)
  Agent ID: homeassistant
  Online agents: 3
    - homeassistant (online)
    - meshtastic (online)
    - mediaserver (offline)
```

Ask another agent for help:
```
User: /coordinate send meshtastic "What MQTT topics do you publish to?"

Sent request to meshtastic. Waiting for response...
Response from meshtastic: "I publish to mesh/node/# for node status and mesh/msg/# for messages."
```
