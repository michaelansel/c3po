# C3PO - Multi-Agent Coordination for Claude Code

C3PO enables multiple Claude Code instances to communicate with each other, enabling collaboration between agents working on different hosts or projects.

## Quick Start

### 1. Deploy the Coordinator (one-time)

```bash
git clone https://github.com/USER/c3po.git
cd c3po
./scripts/deploy.sh full
```

Or for local testing:
```bash
cd coordinator
docker-compose up -d
```

### 2. Enroll Any Claude Code Instance (one command!)

```bash
./scripts/enroll.sh http://your-coordinator:8420 my-agent-name
```

That's it! The script:
- Verifies the coordinator is reachable
- Configures Claude Code's MCP settings
- Sets up the agent identity

### 3. Start Collaborating

In Claude Code, you now have access to:

- `list_agents` - See all online agents
- `send_request` - Send a message to another agent
- `wait_for_response` - Wait for a reply
- `get_pending_requests` - Check your inbox
- `respond_to_request` - Reply to requests

## How It Works

1. Each Claude Code instance connects to the coordinator via MCP
2. Agents are identified by the `X-Agent-ID` header (from `C3PO_AGENT_ID` env var)
3. Messages are queued in Redis and delivered when the target agent checks
4. The Stop hook notifies Claude when there are pending requests to process

## Documentation

- [Setup Guide](docs/SETUP.md) - Detailed installation and configuration
- [Usage Guide](docs/USAGE.md) - How to use C3PO for agent coordination
- [API Reference](docs/API_REFERENCE.md) - MCP tools and REST endpoints
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
