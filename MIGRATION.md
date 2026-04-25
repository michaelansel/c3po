# C3PO Migration Guide: Old Server → exe.dev VM

This deployment is running on `golf-pegasus.exe.xyz:8000`.

## Architecture

- **Coordinator**: Docker container on port 8000 (mapped from internal 8420)
- **Redis**: Docker container with AOF persistence
- **Managed by**: systemd (`c3po.service`)
- **Proxy**: exe.dev handles external access

## Migration Steps

### Step 1: Copy Secrets from Old Deployment

**This is critical.** Existing enrolled agents authenticate using the old server's
`C3PO_SERVER_SECRET`. If you generate new secrets, all agents must re-enroll.

On the **old server**, grab the secrets:
```bash
cat /home/ubuntu/c3po/.secrets
# or
cat /home/ubuntu/c3po/.env
```

Then on **this server**, update `/home/exedev/c3po/.env`:
```bash
# Replace with values from old server
REDIS_PASSWORD=<keep-new-is-fine>
C3PO_SERVER_SECRET=<COPY FROM OLD SERVER>
C3PO_ADMIN_KEY=<COPY FROM OLD SERVER>
C3PO_PROXY_BEARER_TOKEN=<COPY FROM OLD SERVER if using OAuth proxy>
```

Then restart:
```bash
sudo systemctl restart c3po
```

### Step 2: Export Redis State from Old Server

On the **old server** (or anywhere with access to the old Redis):

```bash
# If Redis is in Docker on the old server:
ssh ubuntu@a10.lambda.qerk.be
cd /home/ubuntu/c3po

# Get the Redis password
REDIS_PW=$(grep REDIS_PASSWORD .env | cut -d= -f2)

# Export using redis-cli (if accessible)
# Or copy the migrate script there and run:
bash scripts/migrate-redis.sh export "redis://:${REDIS_PW}@localhost:6379" > /tmp/c3po-state.json
```

Alternatively, if the old Redis is in Docker:
```bash
# Dump directly from the container
docker exec c3po-redis-1 redis-cli -a "$REDIS_PW" --rdb /data/dump.rdb
docker cp c3po-redis-1:/data/dump.rdb ./dump.rdb
```

### Step 3: Transfer State to This Server

```bash
scp ubuntu@a10.lambda.qerk.be:/tmp/c3po-state.json /home/exedev/c3po/
# or for RDB dump:
scp ubuntu@a10.lambda.qerk.be:~/c3po/dump.rdb /home/exedev/c3po/
```

### Step 4: Import State

**Option A: JSON import (recommended)**
```bash
cd /home/exedev/c3po
bash scripts/migrate-redis.sh import < c3po-state.json
```

**Option B: RDB restore**
```bash
# Stop the service
sudo systemctl stop c3po

# Copy RDB into Redis volume
docker run --rm -v c3po_redis_data:/data -v $(pwd):/backup alpine \
  cp /backup/dump.rdb /data/dump.rdb

# Restart
sudo systemctl start c3po
```

### Step 5: Update DNS / Client Configuration

Clients enrolled with `https://mcp.qerk.be` need to be re-pointed to the new URL.

The new endpoint is:
- **Health**: `http://golf-pegasus.exe.xyz:8000/api/health`
- **Agent API**: `http://golf-pegasus.exe.xyz:8000/agent/...`
- **Admin API**: `http://golf-pegasus.exe.xyz:8000/admin/...`
- **MCP**: `http://golf-pegasus.exe.xyz:8000/agent/mcp`

To re-enroll clients:
```bash
python3 setup.py --enroll http://golf-pegasus.exe.xyz:8000 '<server_secret>.<admin_key>'
```

Or update existing MCP configs to point to the new URL.

**Alternative**: Update the DNS CNAME for `mcp.qerk.be` to point to this server,
then clients don't need reconfiguration. (You'd need to set up TLS separately.)

### Step 6: Verify

```bash
# Check health
curl http://localhost:8000/api/health

# Check agents were imported
curl -H "Authorization: Bearer <server_secret>.<admin_key>" \
  http://localhost:8000/admin/agents

# Check API keys exist
curl -H "Authorization: Bearer <server_secret>.<admin_key>" \
  http://localhost:8000/admin/keys
```

## Current Secrets

Stored in `/home/exedev/c3po/.env` (chmod 600).

Admin token for enrollment: `<C3PO_SERVER_SECRET>.<C3PO_ADMIN_KEY>`

To view:
```bash
cat /home/exedev/c3po/.env
```

## Service Management

```bash
# Status
sudo systemctl status c3po

# Logs
sudo journalctl -u c3po -f

# Restart
sudo systemctl restart c3po

# Docker containers
docker compose -f /home/exedev/c3po/docker-compose.prod.yml ps
```

## Architecture

Same stack as the old server: nginx → coordinator + auth-proxy + redis, all in
Docker Compose. The only difference is exe.dev terminates TLS instead of certbot,
and nginx listens on port 80 inside the container (mapped to 8000 on the host).
