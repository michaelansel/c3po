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

Run the setup script. Find the plugin root directory (where this skill file is located, two directories up from SKILL.md) and execute:

```bash
python3 <plugin_root>/setup.py
```

The script handles all setup interactively (coordinator URL, machine name, API key enrollment). No AI interpretation needed.

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

1. Call `send_message` tool with to=agent and message=message
2. Call `wait_for_message` with type="reply" and `timeout=3600` (user will Ctrl+C if needed)
3. Display the response or timeout message

Example:
```
User: /c3po send meshtastic "What nodes are online?"

Response:
Sent message to meshtastic. Waiting for reply...
Reply from meshtastic: "Nodes online: node-1234, node-5678"
```

### `/c3po auto`

Enter auto-listen mode: a tight loop that waits for incoming messages with minimal token usage.

1. Call `set_description` with a brief description of what this agent/project does (infer from the project context — e.g., repo name, README, or working directory)
2. Print: `Auto-listen mode active. Waiting for messages... (Ctrl+C to exit)`
3. Call `wait_for_message` with `timeout=3600`
4. If messages received: process each message fully:
   - For incoming messages (type="message"): read the message, use any tools needed to research an answer, then call `reply` with your response
   - For replies (type="reply"): display the reply content to the user
   - After processing all messages, go back to step 3
5. If timeout (no messages): print ONLY `Still listening...` and go back to step 3

**Critical rules for auto-listen mode:**
- ALWAYS loop back to step 3. Never exit the loop unless the user interrupts with Ctrl+C.
- On timeout, print ONLY "Still listening..." — no extra commentary, no suggestions, no questions.
- Do NOT ask the user for input during the loop. Process everything autonomously.
- When processing messages, use your full tool access to research thorough answers before responding.
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
- If message times out, suggest checking if the target agent is online

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

Sent message to meshtastic. Waiting for reply...
Reply from meshtastic: "I publish to mesh/node/# for node status and mesh/msg/# for messages."
```
