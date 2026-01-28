# C3PO - Claude Code Control Panel and Orchestrator

## Project Codename: Hive

### Overview

A coordination layer that enables multiple Claude Code instances running on different hosts to collaborate on cross-cutting problems while preserving the ability for a human operator to directly interact with any individual instance at any time.

Each CC instance maintains deep context about its domain (e.g., Home Assistant automation, Meshtastic IoT, media library management, mobile app development). The system enables these specialized instances to request help from each other, share findings, and coordinate on problems that span multiple domains—without requiring the human to relay messages between them.

---

## Goals

### Primary Goals

1. **Peer-to-peer agent coordination**: Any CC instance can request assistance from any other CC instance by domain name, without human relay.

2. **Human-in-the-loop when needed**: The human can interrupt and take over any instance at any time using normal CC interaction patterns (Esc, Ctrl-C, or natural language like "stop, let me take over").

3. **AI-initiated escalation**: Each CC instance has a tool to pause automated coordination and request human input when it encounters uncertainty, needs credentials, or hits failure loops.

4. **Preserve standard CC workflow**: Instances run in normal TUI mode. No special terminal requirements (works in plain terminal, tmux, whatever).

5. **Simple host onboarding**: Adding a new host to the coordination network should require minimal setup—ideally installing one package and providing a configuration file.

6. **Opt-in/opt-out**: Coordination mode can be enabled or disabled per-instance without affecting normal CC functionality.

### Non-Goals

- Replacing CC's existing capabilities with custom tooling
- Building a web UI or dashboard (CLI/TUI only)
- Supporting non-CC agents (this is CC-specific)
- Automatic credential sharing between instances
- Real-time streaming of agent outputs to other agents

---

## Acceptance Criteria

### AC1: Basic Coordination Flow

```
Given: CC instances running on Host A (domain: "homeassistant") and Host B (domain: "meshtastic")
When: The user asks the HA instance "debug why Meshtastic sensor data isn't appearing in HA"
Then:
  - HA instance can use a tool to ask the Meshtastic instance about MQTT topic structure
  - Meshtastic instance receives the request, processes it, and returns a response
  - HA instance receives the response and continues its work
  - Total latency for a single request/response < 10 seconds under normal conditions
```

### AC2: Human Interrupt

```
Given: A CC instance is in autonomous coordination mode, processing a request from another agent
When: The human presses Esc, Ctrl-C, or types a message like "stop" or "let me take over"
Then:
  - The instance immediately stops processing the automated request
  - The instance acknowledges the interrupt to the human
  - The instance is now in manual mode and awaits human input
  - The interrupted task is marked as incomplete in the coordination system
  - The requesting agent is notified that the task was interrupted
```

### AC3: AI-Initiated Escalation

```
Given: A CC instance is processing a coordination request
When: The instance determines it needs human input (uncertainty, missing credentials, repeated failures, potentially destructive action)
Then:
  - The instance calls a tool (e.g., `request_human_input`) with a description of what it needs
  - The instance pauses automated processing
  - The coordination system marks this instance as "awaiting human"
  - The instance displays a clear prompt to the human explaining what it needs
  - Other agents requesting help from this instance receive a "busy/unavailable" response
```

### AC4: Resume Autonomous Mode

```
Given: A CC instance is in manual mode (either from human interrupt or AI escalation)
When: The human indicates they want to resume autonomous mode (e.g., "/auto" or "resume coordination mode")
Then:
  - The instance re-enters autonomous coordination mode
  - Pending requests from other agents begin processing
  - The instance confirms the mode change to the human
```

### AC5: Host Registration and Discovery

```
Given: A new host with CC installed wants to join the coordination network
When: The operator installs the coordination package and provides a config file with:
  - Domain name (e.g., "mediaserver")
  - Coordinator address (e.g., "redis://192.168.1.10:6379" or "http://coordinator.local:8080")
  - (Optional) Capabilities list
Then:
  - The instance registers itself with the coordination network on startup
  - Other instances can discover and request help from the new instance
  - The new instance can discover and request help from existing instances
```

### AC6: Graceful Degradation

```
Given: The coordination network is unavailable (coordinator down, network issues)
When: A CC instance attempts to use coordination tools
Then:
  - The tools return clear error messages (not hangs or cryptic failures)
  - The instance continues to function normally for local tasks
  - The instance can be used in fully manual mode without issues
```

### AC7: Easy Enable/Disable

```
Given: An instance with coordination configured
When: The operator wants to disable coordination temporarily
Then:
  - A simple command or config change disables coordination
  - The instance functions as normal CC with no coordination overhead
  - Re-enabling coordination does not require re-registration
```

---

## User Experience

### Entering Coordination Mode

When the coordination MCP server is connected, the user can:
- Type `/coordinate` or similar to explicitly enable coordination mode
- Coordination can also be enabled by default via configuration

The instance should display a brief status indicator that coordination is active.

### During Coordination Mode

The instance periodically checks for incoming requests from other agents. When a request arrives:
- If the human is not actively typing/interacting, process the request automatically
- If the human is mid-interaction, queue the request until the current interaction completes
- Display a subtle indicator that a coordination task is being processed

### Receiving Requests

When another agent sends a request, the receiving instance should:
1. Acknowledge receipt
2. Process the request using its local context and tools
3. Return a structured response with:
   - Success/failure status
   - Result content (text, data, whatever is appropriate)
   - (Optional) Suggested follow-up actions

### Sending Requests

A CC instance can request help from another domain using a tool like:
```
request_domain_help(
  target_domain: "meshtastic",
  question: "What MQTT topic does node 0x1234 publish sensor data to?",
  context: "I'm trying to configure an HA sensor for this node",
  urgency: "normal" | "high"
)
```

The response includes the result or an error explaining why the request couldn't be fulfilled.

### Escalation UX

When the AI decides to escalate:
```
═══════════════════════════════════════════════════════════════
🖐️  HUMAN INPUT NEEDED
───────────────────────────────────────────────────────────────
I'm working on a request from the homeassistant agent to debug
MQTT connectivity, but I need your help:

  I found that the Meshtastic node is publishing to a topic that
  requires authentication. I don't have the MQTT credentials.

  Options:
  1. Provide the credentials
  2. Tell me to skip this and report back to HA
  3. Take over and handle this yourself

Coordination mode paused. Type your response or /auto to resume.
═══════════════════════════════════════════════════════════════
```

---

## Architecture Constraints

### Central Coordinator Required

The system requires a central coordination service that all instances connect to. This can be:
- A lightweight custom service (simplest)
- Redis pub/sub
- A shared SQLite database (if all hosts can access a network filesystem)
- An MCP server running on one host that others connect to via HTTP/SSE

The coordinator handles:
- Agent registration and discovery
- Message routing between agents
- Request queue management
- Status tracking (which agents are available, busy, awaiting human)

### MCP Server Per Host

Each host runs a local MCP server that:
- Connects to the central coordinator
- Exposes coordination tools to the local CC instance
- Manages the local instance's state (autonomous, manual, busy)
- Handles incoming requests by queuing them for CC to process

### Communication Protocol

Inter-agent messages should be structured and include:
```
{
  "message_id": "uuid",
  "from_domain": "homeassistant",
  "to_domain": "meshtastic",
  "type": "request" | "response" | "notification",
  "timestamp": "ISO8601",
  "payload": {
    "question": "...",
    "context": "...",
    "urgency": "normal"
  },
  "correlation_id": "uuid of original request, for responses"
}
```

---

## Relevant Libraries and Patterns

### From Research

1. **FastMCP** (Python) or **mcp-framework** (TypeScript): For building the local MCP server on each host. FastMCP is particularly lightweight and well-suited for this.

2. **Streamable HTTP / SSE transport**: For connecting to remote MCP servers across the network. The MCP spec supports this natively as of early 2025, and Claude Code can connect to remote MCP servers via SSE.

3. **mcp-agent's patterns** (lastmile-ai): The "orchestrator-workers" and "router" patterns from this library are conceptually useful, though we're not using mcp-agent directly. The pattern of having a coordinator route requests to specialized workers is exactly what we're building.

4. **Claude Code headless mode** (`claude -p`): Useful for the coordinator or for testing, but the primary mode is TUI with coordination happening via MCP tools.

5. **Redis pub/sub**: A proven, simple option for the message bus. Lightweight, fast, handles the coordination use case well. Alternative: NATS for more sophisticated routing.

6. **Temporal** (optional, for durability): If we want requests to survive instance restarts, Temporal provides durable execution. Probably overkill for v1.

### Design Patterns

1. **Actor model**: Each CC instance is an actor with its own state and mailbox (queue of incoming requests). Messages are processed one at a time.

2. **Supervisor pattern**: The coordination system should detect when an instance becomes unavailable and handle it gracefully (timeout requests, notify requestors).

3. **Circuit breaker**: If an instance repeatedly fails to respond, temporarily stop routing requests to it.

4. **Back-pressure**: If an instance's queue gets too long, reject new requests with a "busy" status rather than unbounded queuing.

---

## Configuration

### Per-Host Configuration

```yaml
# ~/.config/claude-hive/config.yaml (or similar)

domain: "homeassistant"
description: "Home Assistant automation and integration"
capabilities:
  - "home-automation"
  - "mqtt"
  - "zigbee"

coordinator:
  url: "http://192.168.1.10:8420"
  # or: redis://192.168.1.10:6379
  # or: sqlite:///mnt/shared/hive.db

behavior:
  auto_coordination: true  # Start in coordination mode
  poll_interval_seconds: 5
  request_timeout_seconds: 60
  max_queue_size: 10

escalation:
  # When to automatically escalate to human
  on_repeated_failures: 3
  on_destructive_actions: true  # e.g., deleting files, restarting services
  on_credential_requests: true
```

### Central Coordinator Configuration

```yaml
# On the coordinator host

listen: "0.0.0.0:8420"
storage: "redis://localhost:6379"  # or "sqlite:///var/lib/hive/coord.db"

timeouts:
  agent_heartbeat_seconds: 30
  request_default_seconds: 60
  agent_unavailable_after_seconds: 90

logging:
  level: "info"
  file: "/var/log/hive/coordinator.log"
```

---

## Tools Exposed to CC

The local MCP server should expose these tools to Claude Code:

### `register_domain`
Register this instance with the coordination network. Called automatically on startup if auto_coordination is enabled.

### `list_available_domains`
Returns a list of currently available domains and their capabilities.

### `request_domain_help`
Send a request to another domain and wait for a response.
- `target_domain`: Which domain to ask
- `question`: What you need help with
- `context`: Background information to help the other agent
- `timeout_seconds`: How long to wait (default: 60)

### `get_pending_requests`
Check if there are incoming requests from other agents waiting to be processed.

### `respond_to_request`
Send a response to a previously received request.
- `request_id`: Which request this responds to
- `status`: "success" | "failed" | "escalated"
- `response`: The actual response content

### `request_human_input`
Pause coordination mode and escalate to the human.
- `reason`: Why human input is needed
- `options`: Suggested options for the human (optional)

### `set_coordination_mode`
Enable or disable coordination mode.
- `enabled`: true/false

### `broadcast_notification`
Send a notification to all other domains (or specific domains) without expecting a response. Useful for "FYI" messages like "I just restarted the MQTT broker."

---

## Open Questions for Implementation

1. **Coordinator implementation**: Custom service vs. Redis vs. something else? Trade-offs are complexity vs. reliability vs. ease of deployment.

2. **How does CC process incoming requests?**: Does the MCP server inject them as tool calls? As system messages? Does CC poll explicitly? Need to figure out the cleanest integration with CC's existing patterns.

3. **State persistence**: If CC restarts, should it remember it was in the middle of a coordination task? Probably not for v1—just mark the task as abandoned.

4. **Authentication**: Should domains authenticate to each other? For a home lab, probably not necessary. For anything more exposed, yes.

5. **Request priority**: Should "urgent" requests interrupt the current task? Probably not—just queue them—but worth considering.

---

## Success Metrics

1. **Setup time**: A new host can join the coordination network in < 15 minutes.

2. **Coordination latency**: A simple request/response round-trip completes in < 10 seconds.

3. **Human interrupt latency**: From pressing Esc to being in manual mode < 2 seconds.

4. **Reliability**: 99% of coordination requests complete successfully (don't hang or error silently).

5. **Cognitive overhead**: A user familiar with CC can understand and use the coordination features within 10 minutes of reading docs.

---

## Out of Scope for V1

- Web dashboard or monitoring UI
- Multi-tenant / multi-user support
- Encryption of inter-agent messages (assume trusted home network)
- Complex workflow orchestration (DAGs, dependencies, etc.)
- Persistent agent memory sharing (each agent keeps its own context)
- Support for non-Claude-Code agents

---

## References

- Anthropic's "Building Effective Agents" guide: https://www.anthropic.com/engineering/building-effective-agents
- MCP Specification (transports): https://modelcontextprotocol.io/specification/2025-03-26/basic/transports
- FastMCP: https://github.com/jlowin/fastmcp
- Claude Code headless mode: https://docs.anthropic.com/en/docs/claude-code/cli-usage
- mcp-agent (for pattern inspiration): https://github.com/lastmile-ai/mcp-agent

---

## Additional User Requirements

- Multiple agents on multiple hosts able to talk to each other and handoff information
- Interactive troubleshooting for complex issues
- Explicit relationship building (e.g., "I'm on agent A, please link up with agent B and go work on this task together")
- Discovery and suggestion of linking when it might be beneficial
- Focus on MVP first, iterate from there
