# C3PO Setup Guide

## Prerequisites

**For Coordinator (server):**
- Docker and docker-compose (or finch)
- Network access from Claude Code clients

**For Claude Code Clients:**
- Claude Code CLI installed
- Network access to coordinator

## Quick Start: Plugin Setup (Recommended)

The easiest way to configure C3PO is through the plugin's interactive setup:

### Option 1: Using claude --init

If you have the c3po plugin installed, run:

```bash
claude --init
```

This triggers the Setup hook which guides you through:
1. Entering your coordinator URL
2. Testing connectivity
3. Choosing an agent ID
4. Configuring the MCP server

### Option 2: Using /coordinate setup

Inside Claude Code, run:

```
/coordinate setup
```

This provides the same interactive setup experience.

### Option 3: Shell Script

For scripted or non-interactive setup:

```bash
# From the c3po repo
./scripts/enroll.sh http://your-coordinator:8420 my-agent-name

# Or via curl (from anywhere)
curl -sSL https://raw.githubusercontent.com/USER/c3po/main/scripts/enroll.sh | \
  bash -s -- http://your-coordinator:8420 my-agent-name
```

All methods:
1. Verify the coordinator is reachable
2. Configure Claude Code's MCP settings with user scope
3. Set up the agent identity

After setup, restart Claude Code to connect.

## Coordinator Setup

### Option 1: Docker (Recommended)

The simplest way to run the coordinator:

```bash
cd coordinator
docker-compose up -d
```

This starts:
- **c3po-coordinator** on port 8420
- **Redis** for message queuing

To check status:
```bash
docker-compose ps
docker-compose logs coordinator
```

To stop:
```bash
docker-compose down
```

### Option 2: Local Development

For development without Docker:

```bash
# Start Redis
docker run -d -p 6379:6379 --name c3po-redis redis:7-alpine

# Set up Python environment
cd coordinator
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Start coordinator
python server.py
```

### Option 3: Remote Server (NAS)

Deploy to a Synology NAS or other server:

```bash
# Build and deploy
./scripts/deploy.sh full

# Individual commands
./scripts/deploy.sh build    # Build image
./scripts/deploy.sh push     # Copy to NAS
./scripts/deploy.sh deploy   # Start containers
./scripts/deploy.sh status   # Check status
./scripts/deploy.sh logs     # View logs
./scripts/deploy.sh stop     # Stop containers
```

Configure in `scripts/deploy.sh`:
- `NAS_HOST` - SSH address of your server
- `DATA_DIR` - Data directory for Redis persistence

## Headless Mode Configuration

**Important:** Plugin `.mcp.json` files are NOT loaded in headless mode (`claude -p`). The enrollment script handles this by adding the MCP server with user scope, which works in both interactive and headless modes.

### Pre-approved Tools for Headless Mode

In headless mode, use `--allowedTools` to pre-approve MCP tools:

```bash
claude -p "Your prompt" --allowedTools "mcp__c3po__list_agents,mcp__c3po__send_request,mcp__c3po__wait_for_response,mcp__c3po__respond_to_request"
```

### Per-Invocation Configuration

For testing with different agent IDs, create a config file:

```bash
cat > /tmp/agent-config.json << 'EOF'
{
  "mcpServers": {
    "c3po": {
      "type": "http",
      "url": "http://your-coordinator:8420/mcp",
      "headers": { "X-Agent-ID": "test-agent" }
    }
  }
}
EOF

claude -p "Your prompt" --mcp-config /tmp/agent-config.json --strict-mcp-config
```

## Plugin Installation (Alternative)

For interactive use only (not headless mode), you can install the plugin manually.

### Manual Installation

Copy the plugin directory to your Claude Code plugins location:

```bash
cp -r plugin ~/.claude/plugins/c3po
```

### Verify Installation

Check that the plugin is recognized:
```bash
ls ~/.claude/plugins/c3po
# Should show: .claude-plugin/ .mcp.json hooks/ skills/
```

## Configuration

### Environment Variables

Set these before starting Claude Code:

| Variable | Description | Default |
|----------|-------------|---------|
| `C3PO_COORDINATOR_URL` | Coordinator URL | `http://localhost:8420` |
| `C3PO_AGENT_ID` | Your agent identifier | Current folder name |

Add to your shell profile (`~/.bashrc`, `~/.zshrc`):

```bash
export C3PO_COORDINATOR_URL=http://your-nas:8420
export C3PO_AGENT_ID=my-project
```

Or set per-project in `.envrc` (if using direnv):

```bash
export C3PO_AGENT_ID=homeassistant
```

### Agent ID Guidelines

- Use descriptive names: `homeassistant`, `meshtastic`, `web-frontend`
- Keep it short (1-64 characters)
- Allowed characters: alphanumeric, underscore, dash, dot
- Must not start with special characters

## Verification

### Test Coordinator

```bash
# Health check
curl http://localhost:8420/api/health
# Expected: {"status":"ok","agents_online":0}

# List tools
curl -X POST http://localhost:8420/mcp \
  -H "Content-Type: application/json" \
  -H "X-Agent-ID: test" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
```

### Test Plugin

Start Claude Code and check:

1. On startup, you should see:
   ```
   [c3po] Connected to coordinator. Agent ID: your-agent
   [c3po] N agent(s) online.
   ```

2. Run `/coordinate status` to verify connection

3. Use `list_agents` tool - should include your agent

## Network Configuration

### Firewall

Ensure port 8420 is accessible:
- Local: Usually no firewall issues
- LAN: Open port 8420 on server firewall
- Remote: Consider VPN or SSH tunnel

### Docker Networking

If coordinator and Claude Code are in different Docker networks:
```yaml
# docker-compose.yml
services:
  coordinator:
    ports:
      - "8420:8420"  # Expose to host
```

### Multiple Hosts

For agents on different machines:
1. Run coordinator on accessible server (NAS, cloud VM)
2. Set `C3PO_COORDINATOR_URL` to server's address
3. Ensure all hosts can reach the coordinator

Example for home network:
```bash
# On homeassistant host
export C3PO_COORDINATOR_URL=http://nas.local:8420

# On meshtastic host
export C3PO_COORDINATOR_URL=http://nas.local:8420
```

## Upgrading

### Coordinator

```bash
cd coordinator
docker-compose pull
docker-compose up -d
```

### Plugin

```bash
cp -r plugin ~/.claude/plugins/c3po
# Restart Claude Code to pick up changes
```

## Uninstallation

### Remove Plugin

```bash
rm -rf ~/.claude/plugins/c3po
```

### Stop Coordinator

```bash
cd coordinator
docker-compose down -v  # -v removes data volumes
```

### Clean Environment

Remove from shell profile:
```bash
# Remove these lines:
export C3PO_COORDINATOR_URL=...
export C3PO_AGENT_ID=...
```
