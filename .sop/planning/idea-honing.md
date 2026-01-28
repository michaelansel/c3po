# C3PO Requirements Clarification

This document captures the iterative Q&A process to refine the C3PO (Claude Code Control Panel and Orchestrator) requirements.

---

## Q1: MVP Scope - What's the simplest thing that would be useful?

Your rough idea describes a full-featured coordination system, but you mentioned wanting an MVP first. Looking at the acceptance criteria, AC1 (Basic Coordination Flow) seems like the core value proposition.

**For the MVP, which of these capabilities are essential vs. nice-to-have?**

Essential candidates:
- Two CC instances can send requests to each other and get responses (AC1)
- Agent discovery (knowing what domains are available)

Potentially deferrable:
- Human interrupt handling (AC2) - could rely on standard Ctrl-C behavior initially
- AI-initiated escalation (AC3) - could just let the agent fail/timeout initially
- Resume autonomous mode (AC4) - only needed if we have interrupt/escalation
- Graceful degradation (AC6) - nice but not core
- Easy enable/disable (AC7) - can be manual config initially

**Question:** For the MVP, is the core goal simply "Agent A can ask Agent B a question and get an answer back"? Or are there other capabilities that must be present from day one to make this useful to you?

**Answer:** Keep all the acceptance criteria for MVP. Key insight: AC2 (human interrupt) can be implemented simply via prompt text instructing the agent how to behave when interrupted - no special code needed. The other "deferrable" items are small enough to include.

---

## Q2: Coordinator Technology Choice

Your rough idea lists several options for the central coordinator:
- Custom lightweight service
- Redis pub/sub
- Shared SQLite database (network filesystem)
- MCP server via HTTP/SSE

**Question:** Do you have a preference or constraint here? Considerations:
- **Redis**: Proven, fast, but requires running Redis somewhere
- **SQLite on network share**: Simplest deployment, but may have locking issues with concurrent access
- **Custom HTTP service**: Most flexible, but more code to write
- **MCP-over-HTTP**: Dogfooding MCP, but adds complexity

For a home lab MVP, what's your infrastructure situation? Do you already have Redis running somewhere, or would a simpler file-based or single-binary solution be preferable?

**Answer:**
- Can easily run Docker containers on Synology NAS
- No existing Redis or similar infrastructure
- Leaning towards keeping it off the public internet (local network only)
- Flexible on technology choice - prioritize fast and easy setup
- Not pedantic about specific solutions

**Decision:** Redis in Docker on the NAS is likely the simplest proven option. Single `docker run` command, well-documented, handles pub/sub natively. We can add a thin coordinator service layer on top if needed, or use Redis directly from the MCP servers.

---

## Q3: How CC Processes Incoming Requests

This is one of the open questions from your rough idea and it's architecturally critical.

When Agent B receives a request from Agent A, how should CC become aware of it? Options:

1. **Polling via MCP tool**: CC periodically calls `get_pending_requests()` tool. Simple but requires the agent to actively check.

2. **Injected as user message**: The MCP server somehow injects the request as if the user typed it. This would require hooks or CC customization.

3. **CC Hooks integration**: Claude Code has a hooks system that can run commands on certain events. We might be able to trigger processing via hooks.

4. **Background agent pattern**: A separate headless CC instance (`claude -p`) handles incoming requests, keeping the TUI instance free for human interaction.

**Question:** Have you experimented with Claude Code hooks or headless mode? Do you have a preference for how the "receiving agent" should pick up and process incoming requests?

**Answer:**
- Not option 4 (background agent) - wants the TUI experience
- Probably not option 1 (polling) - feels clunky
- Interested in hooks if they can help
- Does NOT want to customize/fork Claude Code itself

**Open:** Need to research what CC hooks can actually do. Can they inject messages? Trigger tool calls? This will inform the architecture significantly.

---

## Q4: Programming Language Preference

The MCP servers (one per host) need to be written in something. Your rough idea mentions FastMCP (Python) and mcp-framework (TypeScript).

**Question:** Do you have a preference between Python and TypeScript/Node for the MCP server implementation? Considerations:
- **Python (FastMCP)**: Lightweight, quick to iterate, good for scripting-style code
- **TypeScript**: Better type safety, might be more familiar if you do web dev, Claude Code itself is TypeScript

Which would you prefer to work with and maintain?

**Answer:** User pushes back on "MCP server per host" assumption. Key constraints:
- Minimal installation on each host
- Some hosts are resource constrained
- Prefer a shared/centralized solution or lightweight plugin

**This changes the architecture.** Options to explore:

**Option A: Centralized Remote MCP Server**
- One MCP server runs on the NAS (alongside Redis)
- All CC instances connect to it via HTTP/SSE transport (MCP spec supports this)
- Per-host setup is just CC config pointing to `http://nas:8420/mcp`
- Each CC identifies itself via config or environment variable

**Option B: Git-synced lightweight plugin**
- MCP config/code in a shared git repo
- Each host clones repo, but still needs a runtime (Python/Node)
- Lighter than full server but still has per-host requirements

**Option C: Claude Code hooks + simple HTTP calls**
- No MCP server at all on most hosts
- Use CC hooks to make HTTP calls to a central coordinator
- Depends on what hooks can actually do

---

## Q5: Centralized MCP Server - Viable Architecture?

Based on your constraint, **Option A (Centralized Remote MCP Server)** seems most promising:

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│   Host A    │     │   Host B    │     │   Host C    │
│   (CC)      │     │   (CC)      │     │   (CC)      │
└──────┬──────┘     └──────┬──────┘     └──────┬──────┘
       │                   │                   │
       │ HTTP/SSE          │ HTTP/SSE          │ HTTP/SSE
       │                   │                   │
       └───────────────────┼───────────────────┘
                           │
                    ┌──────▼──────┐
                    │    NAS      │
                    │ ┌─────────┐ │
                    │ │MCP Srvr │ │
                    │ └────┬────┘ │
                    │      │      │
                    │ ┌────▼────┐ │
                    │ │  Redis  │ │
                    │ └─────────┘ │
                    └─────────────┘
```

Per-host setup becomes:
1. Install Claude Code (already done)
2. Add MCP server config: `{"url": "http://nas:8420/mcp", "headers": {"X-Domain": "homeassistant"}}`

**Question:** Does this centralized approach work for you? The tradeoff is that coordination is unavailable if the NAS is down, but that's probably acceptable for a home lab.

**Answer/Follow-up:** User asks about easier CC setup options. They've installed something from a "plugin marketplace" before and found it easy. Need to research:
- What is CC's plugin/MCP marketplace?
- Can we distribute an MCP server config via marketplace for easy install?
- What's the simplest per-host onboarding path?

---

## Q6: Clarifying CC Plugin Experience

I want to make sure I understand what you've used before.

**Question:** When you say "plugin marketplace" - are you referring to:
1. Claude Code's `/mcp` command to add MCP servers?
2. Something in Claude Desktop (the GUI app)?
3. A VS Code / IDE extension marketplace?
4. Something else?

And can you describe what "easy to install" looked like? (e.g., "I typed a command", "I clicked a button", "I copied a JSON snippet")

**Answer/Research Results:**

Claude Code has a full plugin marketplace system (since v2.0.13, Oct 2025):

**Plugin Installation:**
- Users run `/plugin install plugin-name@marketplace`
- Or use `/plugin` for an interactive UI to browse and install
- Plugins can be scoped to user, project, or managed by admins

**What Plugins Can Include:**
- MCP servers (local or remote via HTTP/SSE)
- Slash commands/skills (like `/review`)
- Agents (specialized subagents)
- Hooks (triggers at key workflow points)
- LSP servers

**Distribution Options:**
1. Official Anthropic marketplace (submit for review)
2. Create your own marketplace (any git repo with `.claude-plugin/marketplace.json`)
3. Share via: `/plugin marketplace add owner/repo`

**Remote MCP Servers Supported:**
```json
{
  "mcpServers": {
    "c3po": {
      "type": "http",
      "url": "https://nas.local:8420/mcp"
    }
  }
}
```

**This is exactly what we need.** A c3po plugin could:
- Point to the central coordinator MCP server
- Include skills like `/coordinate`, `/link-agent`
- Include prompts/hooks for coordination behavior
- Be installed with one command: `/plugin install c3po@your-marketplace`

---

## Q7: Plugin-Based Architecture Confirmation

Given the plugin system, here's the revised architecture:

```
┌─────────────────────────────────────────────────────────────┐
│                     c3po Plugin                              │
│  (Git repo: github.com/you/c3po)                            │
│  ┌─────────────────────────────────────────────────────────┐│
│  │ .claude-plugin/plugin.json                              ││
│  │ .mcp.json → points to http://nas:8420/mcp               ││
│  │ skills/coordinate/SKILL.md                              ││
│  │ skills/link-agent/SKILL.md                              ││
│  └─────────────────────────────────────────────────────────┘│
└─────────────────────────────────────────────────────────────┘
                              │
            ┌─────────────────┼─────────────────┐
            │ /plugin install │                 │
            ▼                 ▼                 ▼
      ┌──────────┐     ┌──────────┐      ┌──────────┐
      │  Host A  │     │  Host B  │      │  Host C  │
      │   (CC)   │     │   (CC)   │      │   (CC)   │
      └────┬─────┘     └────┬─────┘      └────┬─────┘
           │                │                 │
           └────────────────┼─────────────────┘
                            │ HTTP
                     ┌──────▼──────┐
                     │    NAS      │
                     │ ┌─────────┐ │
                     │ │Coord.   │ │
                     │ │MCP Srvr │ │
                     │ └────┬────┘ │
                     │ ┌────▼────┐ │
                     │ │  Redis  │ │
                     │ └─────────┘ │
                     └─────────────┘
```

**Per-host setup becomes:**
1. `/plugin marketplace add you/c3po`
2. `/plugin install c3po`
3. Configure domain name (env var or plugin config)

**Question:** Does this plugin-based distribution approach work for you? The main thing we'd need to figure out is how each host identifies itself (its domain name like "homeassistant" or "meshtastic").

**Answer:** Yes, this works. For agent naming:
- Default: Use the project/folder name as the agent name
- Allow explicit renaming via config
- Server handles collisions automatically (e.g., suffix with "-2", "-3")

---

## Q8: Interactive Troubleshooting & Agent Linking

You mentioned two features beyond basic request/response:

1. **Interactive troubleshooting for complex issues** - agents can have a back-and-forth dialogue, not just single request/response

2. **Explicit relationship building** - "I'm on agent A, please link up with agent B and go work on this task together"

**Question:** Can you describe what "interactive troubleshooting" looks like in practice?

For example, if Agent A asks Agent B to debug an MQTT issue:
- **Option 1 (Async rounds):** A sends question → B responds → A sends follow-up → B responds → etc. Each round is a separate request/response.
- **Option 2 (Synchronous session):** A and B open a "session" and have a real-time back-and-forth until the issue is resolved.
- **Option 3 (Shared context):** B pulls A into a shared conversation where both can see what's happening.

Which of these (or something else) matches your mental model?

**Answer:** Option 1 - Async rounds with explicit handoff. Each handoff includes clear directions (could be a simple question or a complex activity). This keeps things simple and fits the request/response model.

Example flow:
```
A → B: "What MQTT topic does node 0x1234 use?"
B → A: "It uses mesh/node/1234/sensors. But I see it's not publishing. Want me to check the node status?"
A → B: "Yes, please check node status and restart if needed"
B → A: "Node was in sleep mode. I woke it and it's now publishing to mesh/node/1234/sensors every 30s"
A: "Got it, I'll configure the HA sensor now"
```

---

## Q9: "Link Up Agents" Feature

You mentioned: *"I'm on agent A, please link up with agent B and go work on this task together"*

**Question:** What does "link up" mean to you? Some interpretations:

1. **Delegation with monitoring:** You tell A to collaborate with B on a task. A coordinates with B, and you can watch/interrupt either. When done, A reports back to you.

2. **Introduction:** You tell A to introduce itself to B and explain a task. Then B takes over and you interact with B directly while A stays available.

3. **Parallel work:** Both A and B work on related aspects of a problem simultaneously, checking in with each other as needed. You monitor overall progress.

4. **Something else?**

Also: Should this be human-initiated only (you tell agents to link up), or should agents be able to suggest it? ("I notice the meshtastic agent might be helpful here. Should I reach out?")

**Answer:** "Link up" is just the async handoff mechanism - nothing special. Key points:
- **Human-initiated only** for MVP - the human decides when agents should collaborate
- No automatic discovery or "should I reach out?" suggestions - avoids cognitive overhead
- Agents don't need to know about all other agents all the time
- Human provides explicit direction: "Go link up with home-assistant and help get MQTT sensors set up"

This is simpler: the `request_domain_help` tool is only used when the human has directed the agent to collaborate with another specific agent.

---

## Q10: Central MCP Server - Language Choice

The coordinator MCP server (running on your NAS) needs to be written in something. Since it's centralized, there's only one thing to maintain.

**Question:** Do you have a preference between:
- **Python (FastMCP):** Quick to develop, easy to read/modify, good library ecosystem
- **TypeScript/Node:** Strong typing, might be more performant, matches CC's codebase

Either works fine for this use case. What's your preference for maintaining the server code?

**Answer:** Python (FastMCP) - good library ecosystem, and no strong preference otherwise.

---

## Q11: Requirements Summary - Ready for Research?

We've clarified the key requirements. Here's where we stand:

**Architecture:**
- Central MCP server (Python/FastMCP) + Redis on NAS in Docker
- CC plugin for easy distribution (`/plugin install c3po`)
- Agent naming: folder name by default, server handles collisions

**MVP Scope (all ACs):**
- AC1: Basic request/response between agents ✓
- AC2: Human interrupt (via prompt text) ✓
- AC3: AI escalation ✓
- AC4: Resume coordination ✓
- AC5: Registration/discovery ✓
- AC6: Graceful degradation ✓
- AC7: Easy enable/disable ✓

**Interaction Model:**
- Async rounds with explicit handoff
- Human-initiated collaboration only (no auto-suggestions)
- Multi-turn back-and-forth for complex issues

**Open Technical Question:**
- **How does CC process incoming requests?** We need to research what hooks can do - can they inject messages or trigger the agent to check for pending requests?

**Question:** Before I move to research, are there any other requirements or constraints I should know about? Or should we proceed to researching the hooks/incoming request handling question?

**Answer:** Proceed with research.

---

## Requirements Clarification Complete

Moving to research phase to investigate:
1. Claude Code hooks - capabilities and limitations
2. FastMCP HTTP transport implementation
3. Incoming request handling patterns

