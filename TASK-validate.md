# C3PO Validation Task

## Objective

Deploy the coordinator to the NAS and validate end-to-end communication between two Claude Code agents.

## Part 1: Fix Deployment

The current `scripts/deploy.sh` doesn't work because:
1. Local machine is arm64 (Mac), NAS is x86_64
2. `finch save -o` doesn't work, need to use stdout redirect
3. scp/rsync having permission issues

**Solution:** Build the Docker image directly on the NAS.

### Steps

1. Copy coordinator source files to NAS:
   ```bash
   ssh admin@mkansel-nas.home.qerk.be "mkdir -p /volume1/enc-containers/c3po/coordinator"
   ```

2. Use tar to transfer files (avoids scp issues):
   ```bash
   cd coordinator
   tar czf - --exclude='__pycache__' --exclude='.venv' --exclude='.pytest_cache' --exclude='tests' . | \
     ssh admin@mkansel-nas.home.qerk.be "cd /volume1/enc-containers/c3po/coordinator && tar xzf -"
   ```

3. Build on NAS:
   ```bash
   ssh admin@mkansel-nas.home.qerk.be "cd /volume1/enc-containers/c3po/coordinator && docker build -t c3po-coordinator:latest ."
   ```

4. Deploy containers:
   ```bash
   ssh admin@mkansel-nas.home.qerk.be "cd /volume1/enc-containers/c3po/coordinator && docker-compose up -d"
   ```

5. Verify:
   ```bash
   curl http://mkansel-nas.home.qerk.be:8420/api/health
   # Expected: {"status":"ok","agents_online":0}
   ```

### Update deploy.sh

Fix the script to handle cross-architecture deployment properly.

## Part 2: Two-Agent Validation

Prove that two Claude Code agents can communicate through the coordinator.

### Setup

1. Install the plugin on local machine:
   ```bash
   cp -r plugin ~/.claude/plugins/c3po
   ```

2. Set coordinator URL:
   ```bash
   export C3PO_COORDINATOR_URL=http://mkansel-nas.home.qerk.be:8420
   ```

### Test Procedure

1. **Terminal 1 - Agent A:**
   ```bash
   cd /tmp/agent-a
   mkdir -p /tmp/agent-a && cd /tmp/agent-a
   export C3PO_AGENT_ID=agent-a
   export C3PO_COORDINATOR_URL=http://mkansel-nas.home.qerk.be:8420
   claude
   ```

   In Claude, run:
   ```
   /coordinate status
   ```
   Should show connected, 1 agent online.

2. **Terminal 2 - Agent B:**
   ```bash
   mkdir -p /tmp/agent-b && cd /tmp/agent-b
   export C3PO_AGENT_ID=agent-b
   export C3PO_COORDINATOR_URL=http://mkansel-nas.home.qerk.be:8420
   claude
   ```

   In Claude, run:
   ```
   /coordinate status
   ```
   Should show connected, 2 agents online.

3. **Agent A sends request to Agent B:**
   In Terminal 1 (Agent A), tell Claude:
   ```
   Send a message to agent-b asking "What is 2+2?"
   ```

   Claude should use `send_request` tool and `wait_for_response`.

4. **Agent B receives and responds:**
   In Terminal 2 (Agent B), when Claude finishes any task, the Stop hook should trigger and show the pending request. Claude should process it and respond.

5. **Agent A receives response:**
   Agent A's `wait_for_response` should return with agent-b's answer.

### Success Criteria

- [ ] Coordinator deployed and responding at http://mkansel-nas.home.qerk.be:8420
- [ ] Two agents can register and see each other via `list_agents`
- [ ] Agent A can send request to Agent B
- [ ] Agent B receives request (via Stop hook or explicit check)
- [ ] Agent B responds to request
- [ ] Agent A receives response
- [ ] Round-trip latency < 10 seconds

### Troubleshooting

If hooks don't trigger:
- Check plugin is installed: `ls ~/.claude/plugins/c3po`
- Check env vars are set: `echo $C3PO_COORDINATOR_URL`
- Check coordinator is reachable: `curl $C3PO_COORDINATOR_URL/api/health`
- Manually check pending: `curl -H "X-Agent-ID: agent-b" $C3PO_COORDINATOR_URL/api/pending`

## Deliverables

1. Working deployment on NAS
2. Updated `scripts/deploy.sh` that handles cross-architecture
3. Documented proof (screenshot or log) of successful two-agent communication
