# C3PO Troubleshooting Guide

## Connection Issues

### "Coordinator not available" on startup

**Symptoms:**
- SessionStart hook shows: `[c3po] Coordinator not available`
- Tools fail with connection errors

**Causes:**
1. Coordinator not running
2. Wrong URL in `C3PO_COORDINATOR_URL`
3. Network/firewall blocking connection

**Solutions:**

Check coordinator is running:
```bash
curl http://localhost:8420/api/health
# Should return: {"status":"ok","agents_online":N}
```

Check environment variable:
```bash
echo $C3PO_COORDINATOR_URL
# Should show your coordinator URL
```

Check network connectivity:
```bash
ping your-nas.local
curl http://your-nas.local:8420/api/health
```

### Tools work but hooks don't

**Symptoms:**
- `list_agents` works fine
- Stop hook doesn't trigger
- No startup message

**Cause:**
Plugin not installed correctly

**Solution:**

Verify plugin structure:
```bash
ls -la ~/.claude/plugins/c3po/
# Should show: .claude-plugin/ .mcp.json hooks/ skills/
```

Check hook permissions:
```bash
chmod +x ~/.claude/plugins/c3po/hooks/*.py
```

## Agent Issues

### "Agent not found" Error

**Symptoms:**
```
ToolError: Agent 'meshtastic' not found.
Available agents: homeassistant
```

**Causes:**
1. Target agent hasn't connected yet
2. Target agent is offline
3. Typo in agent ID

**Solutions:**

List available agents:
```
Use list_agents to see who's online.
```

Check agent ID spelling - it's case-sensitive.

Wait for target agent to start Claude Code.

### Agent shows as "offline"

**Symptoms:**
- Agent appears in list but status is "offline"

**Cause:**
Agent hasn't made a request recently (>5 minutes)

**Solution:**

This is normal - agents go "offline" after 5 minutes of inactivity. They'll come back "online" on next tool use.

The agent can still receive messages while "offline" - they'll be delivered when the agent checks.

### Wrong Agent ID

**Symptoms:**
- Messages going to wrong agent
- Can't find your own agent in list

**Cause:**
`C3PO_AGENT_ID` not set or incorrect

**Solution:**

Check current agent ID:
```bash
echo $C3PO_AGENT_ID
```

Set it before starting Claude Code:
```bash
export C3PO_AGENT_ID=my-project
```

## Message Issues

### "Rate limited" Error

**Symptoms:**
```
ToolError: Rate limit exceeded for agent 'homeassistant'.
Maximum 10 requests per 60 seconds.
```

**Cause:**
Sending too many requests in a short time

**Solution:**

Wait 60 seconds and try again. The limit is per-agent, per-minute.

### Timeout waiting for response

**Symptoms:**
```
{"status": "timeout", "code": "TIMEOUT", ...}
```

**Causes:**
1. Target agent is not running Claude Code
2. Target agent is busy with other tasks
3. Network issues between agents and coordinator

**Solutions:**

Check if target is online:
```
Use list_agents to check target status.
```

Increase timeout for long operations:
```
Use wait_for_response with timeout=120 for longer waits.
```

Try again later.

### Messages not being received

**Symptoms:**
- send_request succeeds
- Target agent never sees the message

**Causes:**
1. Target agent not checking inbox
2. Messages expired (after 24h)
3. Redis data lost

**Solutions:**

On target agent, check pending:
```
Use get_pending_requests to check inbox.
```

Or actively wait:
```
Use wait_for_request to listen for messages.
```

Check coordinator logs for errors:
```bash
docker-compose logs coordinator
```

## Redis Issues

### "Connection refused" to Redis

**Symptoms:**
- Coordinator fails to start
- "Connection refused" errors in logs

**Cause:**
Redis not running or wrong URL

**Solution:**

Check Redis is running:
```bash
docker-compose ps
# redis should be "Up"
```

Check Redis URL in docker-compose.yml:
```yaml
environment:
  - REDIS_URL=redis://redis:6379
```

### Messages disappearing

**Symptoms:**
- Messages sent successfully
- Messages not in inbox

**Cause:**
Redis restarted without persistence

**Solution:**

Enable Redis persistence in docker-compose.yml:
```yaml
redis:
  command: redis-server --appendonly yes
  volumes:
    - redis_data:/data
```

## Hook Issues

### Stop hook not firing

**Symptoms:**
- Have pending requests
- Claude doesn't process them on stop

**Cause:**
Stop hook not configured or failing silently

**Solution:**

Test hook manually:
```bash
export C3PO_COORDINATOR_URL=http://localhost:8420
export C3PO_AGENT_ID=test
echo '{}' | python ~/.claude/plugins/c3po/hooks/check_inbox.py
```

Check hooks.json configuration:
```bash
cat ~/.claude/plugins/c3po/hooks/hooks.json
```

### Hooks causing errors

**Symptoms:**
- Claude Code shows hook errors
- Workflow interrupted

**Cause:**
Hook script error or timeout

**Solution:**

Hooks should fail silently. Check for:
- Syntax errors in hook scripts
- Missing dependencies (urllib, json)
- Timeout issues (hooks have 10s limit)

Test hooks in isolation:
```bash
python ~/.claude/plugins/c3po/hooks/check_inbox.py <<< '{}'
```

## Performance Issues

### Slow response times

**Symptoms:**
- Requests take >10 seconds
- Coordinator seems slow

**Causes:**
1. Redis performance issues
2. Network latency
3. Coordinator overloaded

**Solutions:**

Check coordinator health:
```bash
time curl http://localhost:8420/api/health
# Should be <100ms
```

Check Redis:
```bash
docker exec c3po-redis redis-cli ping
# Should return PONG instantly
```

Consider running coordinator closer to agents (same network).

### High memory usage

**Symptoms:**
- Coordinator using lots of memory
- Redis growing continuously

**Cause:**
Messages accumulating (not being consumed)

**Solution:**

Messages expire after 24h automatically. If you need to clear manually:

```bash
docker exec c3po-redis redis-cli FLUSHDB
```

**Warning:** This deletes all messages!

## Getting Help

### Collect Diagnostic Info

When reporting issues, include:

1. Environment:
```bash
echo "Coordinator URL: $C3PO_COORDINATOR_URL"
echo "Agent ID: $C3PO_AGENT_ID"
curl -s http://localhost:8420/api/health
```

2. Coordinator logs:
```bash
docker-compose logs coordinator --tail=50
```

3. Redis status:
```bash
docker exec c3po-redis redis-cli INFO | grep -E "(used_memory|connected_clients)"
```

4. Plugin status:
```bash
ls -la ~/.claude/plugins/c3po/
```

### Error Codes

| Code | Meaning | Action |
|------|---------|--------|
| `COORD_UNAVAILABLE` | Can't reach coordinator | Check network/URL |
| `AGENT_NOT_FOUND` | Target agent unknown | Check agent ID, wait for them |
| `TIMEOUT` | Request timed out | Target may be offline, retry |
| `INVALID_REQUEST` | Bad request format | Check parameters |
| `RATE_LIMITED` | Too many requests | Wait and retry |
