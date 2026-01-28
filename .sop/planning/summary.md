# C3PO Planning Summary

## Project Overview

**C3PO (Claude Code Control Panel and Orchestrator)** is a coordination layer enabling multiple Claude Code instances on different hosts to collaborate without human relay.

---

## Artifacts Created

```
.sop/planning/
├── rough-idea.md                    # Original concept and requirements
├── idea-honing.md                   # Requirements clarification Q&A
├── research/
│   ├── cc-hooks.md                  # Claude Code hooks capabilities
│   ├── fastmcp-implementation.md    # FastMCP HTTP server patterns
│   └── notification-patterns.md     # MCP notification research
├── design/
│   └── detailed-design.md           # Complete system design
├── implementation/
│   └── plan.md                      # 13-step implementation plan
└── summary.md                       # This document
```

---

## Architecture Summary

```
┌─────────────────────────────────────────────────────────────┐
│                     c3po Plugin (GitHub)                    │
│  - MCP config pointing to coordinator                       │
│  - Stop hook to check for pending requests                  │
│  - SessionStart hook for registration                       │
│  - /coordinate skill                                        │
└─────────────────────────────────────────────────────────────┘
                              │
                    /plugin install c3po
                              │
         ┌────────────────────┼────────────────────┐
         ▼                    ▼                    ▼
   ┌──────────┐        ┌──────────┐        ┌──────────┐
   │  Host A  │        │  Host B  │        │  Host C  │
   │   (CC)   │        │   (CC)   │        │   (CC)   │
   └────┬─────┘        └────┬─────┘        └────┬─────┘
        │ HTTP              │                   │
        └───────────────────┼───────────────────┘
                            │
                     ┌──────▼──────┐
                     │    NAS      │
                     │ ┌─────────┐ │
                     │ │FastMCP  │ │
                     │ │  :8420  │ │
                     │ └────┬────┘ │
                     │ ┌────▼────┐ │
                     │ │  Redis  │ │
                     │ └─────────┘ │
                     └─────────────┘
```

---

## Key Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Distribution | CC Plugin | One-command install, includes hooks |
| Coordinator | FastMCP (Python) | Best docs, quick to develop |
| Message store | Redis | Native BLPOP for blocking, simple |
| Agent identity | X-Agent-ID header | Simple, folder name default |
| Incoming requests | Stop hook + blocking tool | Works with CC's limitations |
| Collaboration | Human-initiated only | Avoids cognitive overhead |

---

## Implementation Plan Overview

**13 steps**, each producing a working, demoable increment:

| Phase | Steps | Outcome |
|-------|-------|---------|
| **Foundation** | 1-2 | Coordinator skeleton, Redis, agent registration |
| **Messaging** | 3-5 | Request/response, blocking waits |
| **Infrastructure** | 6-7 | REST API for hooks, Docker packaging |
| **Plugin** | 8-10 | Plugin structure, Stop hook, SessionStart hook |
| **Validation** | 11 | End-to-end two-agent test |
| **Polish** | 12-13 | Error handling, /coordinate skill, docs |

---

## Next Steps

1. **Review the detailed design** at `.sop/planning/design/detailed-design.md`
2. **Review the implementation plan** at `.sop/planning/implementation/plan.md`
3. **Begin implementation** with Step 1 (project scaffolding)
4. **Demo each step** before proceeding to the next

---

## Open Items for Future

- Authentication for non-home-lab deployments
- TLS encryption for coordinator connections
- Agent suggestions ("I notice meshtastic might help...")
- Web dashboard for visibility
- Persistent multi-turn context

---

## Quick Reference

**Environment Variables:**
- `C3PO_COORDINATOR_URL` - Coordinator URL (default: `http://localhost:8420`)
- `C3PO_AGENT_ID` - Agent name (default: folder name)

**MCP Tools:**
- `list_agents` - See online agents
- `send_request` - Send request to another agent
- `get_pending_requests` - Get incoming requests
- `respond_to_request` - Reply to a request
- `wait_for_response` - Block until response arrives
- `wait_for_request` - Block until request arrives

**Skill:**
- `/coordinate status` - Check connection
- `/coordinate agents` - List agents
- `/coordinate send <agent> <msg>` - Quick message
