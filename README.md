# C3PO - Multi-Agent Coordination for Claude Code

C3PO enables multiple Claude Code instances to communicate with each other, enabling collaboration between agents working on different hosts or projects.

## Quick Start

### 1. Start the Coordinator

```bash
cd coordinator
docker-compose up -d
```

This starts the coordinator (port 8420) and Redis backend.

### 2. Install the Plugin

Copy the plugin to your Claude Code plugins directory:

```bash
cp -r plugin ~/.claude/plugins/c3po
```

### 3. Configure Environment

Set these environment variables in your shell profile or before starting Claude Code:

```bash
export C3PO_COORDINATOR_URL=http://localhost:8420
export C3PO_AGENT_ID=my-project-name
```

### 4. Use C3PO

In Claude Code, you can now:

- `/coordinate status` - Check connection and see online agents
- `/coordinate send <agent> <message>` - Send a message to another agent
- Use `list_agents` tool to see available agents
- Use `send_request` tool for detailed messaging

## How It Works

1. Each Claude Code instance connects to the coordinator via MCP
2. Agents are identified by the `X-Agent-ID` header (from `C3PO_AGENT_ID` env var)
3. Messages are queued in Redis and delivered when the target agent checks
4. The Stop hook notifies Claude when there are pending requests to process

## Documentation

- [Setup Guide](docs/SETUP.md) - Detailed installation and configuration
- [Usage Guide](docs/USAGE.md) - How to use C3PO for agent coordination
- [Troubleshooting](docs/TROUBLESHOOTING.md) - Common issues and solutions

## Architecture

```
┌─────────────────┐     ┌─────────────────┐
│  Claude Code A  │     │  Claude Code B  │
│  (homeassistant)│     │   (meshtastic)  │
└────────┬────────┘     └────────┬────────┘
         │                       │
         │  MCP Protocol         │
         │                       │
         ▼                       ▼
    ┌────────────────────────────────┐
    │     C3PO Coordinator           │
    │  (FastMCP + REST API)          │
    └────────────────┬───────────────┘
                     │
                     ▼
              ┌──────────────┐
              │    Redis     │
              │  (queues)    │
              └──────────────┘
```

## License

MIT
