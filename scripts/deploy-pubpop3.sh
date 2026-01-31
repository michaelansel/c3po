#!/bin/bash
set -euo pipefail

# C3PO Deployment to pubpop3.datadrop.biz
# This script runs locally and SSHes into the server for remote operations.
#
# Prerequisites (user must do manually):
#   sudo loginctl enable-linger mansel
#   Create GitHub OAuth App (Settings > Developer settings > OAuth Apps)
#     - Callback: https://mcp.qerk.be/.auth/github/callback
#   # (after this script): sudo commands for nginx + certbot
#
# Usage:
#   bash scripts/deploy-pubpop3.sh

SSH_AUTH_SOCK="${SSH_AUTH_SOCK:-$HOME/Library/Group Containers/2BUA8C4S2C.com.1password/t/agent.sock}"
export SSH_AUTH_SOCK

REMOTE="mansel@pubpop3.datadrop.biz"
REMOTE_DIR="/home/mansel/c3po"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'
log()  { echo -e "${GREEN}[c3po]${NC} $1"; }
warn() { echo -e "${YELLOW}[c3po]${NC} $1"; }
err()  { echo -e "${RED}[c3po]${NC} $1" >&2; }

ssh_run() { ssh "$REMOTE" "$@"; }

# -------------------------------------------------------------------
# Step 1: Verify linger is enabled
# -------------------------------------------------------------------
log "Checking systemd linger..."
if ssh_run "loginctl show-user mansel 2>/dev/null | grep -q 'Linger=yes'"; then
    log "Linger is enabled"
else
    err "Linger is NOT enabled. Run on the server:"
    err "  sudo loginctl enable-linger mansel"
    exit 1
fi

# -------------------------------------------------------------------
# Step 2: Copy source and build Docker image
# -------------------------------------------------------------------
log "Copying coordinator source to server..."
ssh_run "mkdir -p ${REMOTE_DIR}/coordinator"
(cd "$PROJECT_DIR/coordinator" && \
    tar czf - \
        --exclude='__pycache__' \
        --exclude='.venv' \
        --exclude='.pytest_cache' \
        --exclude='tests' \
        --exclude='docker-compose.yml' \
        --exclude='.agent' \
        --exclude='.ralph' \
        . \
) | ssh_run "cd ${REMOTE_DIR}/coordinator && tar xzf -"

log "Building Docker image on server (this may take a minute)..."
ssh_run "cd ${REMOTE_DIR}/coordinator && docker build -t c3po-coordinator:latest ." 2>&1 | tail -5

# -------------------------------------------------------------------
# Step 3: Ensure secrets exist
# -------------------------------------------------------------------
if ssh_run "test -f ${REMOTE_DIR}/.secrets"; then
    log "Secrets file already exists"
else
    err "Secrets file missing at ${REMOTE_DIR}/.secrets"
    err "Create it with the following variables:"
    err "  REDIS_PASSWORD=<random>"
    err "  C3PO_PROXY_BEARER_TOKEN=<random>"
    err "  C3PO_HOOK_SECRET=<random>"
    err "  GITHUB_CLIENT_ID=<from github oauth app>"
    err "  GITHUB_CLIENT_SECRET=<from github oauth app>"
    err "  GITHUB_ALLOWED_USER=<your github username>"
    exit 1
fi

# Load secrets for docker-compose template
REDIS_PASSWORD=$(ssh_run "grep REDIS_PASSWORD ${REMOTE_DIR}/.secrets | cut -d= -f2")
PROXY_BEARER_TOKEN=$(ssh_run "grep C3PO_PROXY_BEARER_TOKEN ${REMOTE_DIR}/.secrets | cut -d= -f2")
HOOK_SECRET=$(ssh_run "grep C3PO_HOOK_SECRET ${REMOTE_DIR}/.secrets | cut -d= -f2")
GITHUB_CLIENT_ID=$(ssh_run "grep GITHUB_CLIENT_ID ${REMOTE_DIR}/.secrets | cut -d= -f2")
GITHUB_CLIENT_SECRET=$(ssh_run "grep GITHUB_CLIENT_SECRET ${REMOTE_DIR}/.secrets | cut -d= -f2")
GITHUB_ALLOWED_USER=$(ssh_run "grep GITHUB_ALLOWED_USER ${REMOTE_DIR}/.secrets | cut -d= -f2")

# -------------------------------------------------------------------
# Step 4: Create docker-compose.yml
# -------------------------------------------------------------------
log "Creating docker-compose.yml..."
ssh_run "cat > ${REMOTE_DIR}/docker-compose.yml" << 'COMPOSE_EOF'
services:
  coordinator:
    image: c3po-coordinator:latest
    ports:
      - "127.0.0.1:8420:8420"
    environment:
      - REDIS_URL=redis://:${REDIS_PASSWORD}@redis:6379
      - C3PO_PROXY_BEARER_TOKEN=${C3PO_PROXY_BEARER_TOKEN}
      - C3PO_BEHIND_PROXY=true
    depends_on:
      redis:
        condition: service_started
    restart: unless-stopped
    read_only: true
    tmpfs:
      - /tmp:size=10M
    cap_drop:
      - ALL
    security_opt:
      - no-new-privileges:true
    deploy:
      resources:
        limits:
          cpus: "1.0"
          memory: 128M
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8420/api/health')"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 10s

  auth-proxy:
    image: ghcr.io/sigbit/mcp-auth-proxy:latest
    command: >-
      --external-url https://mcp.qerk.be
      --github-client-id ${GITHUB_CLIENT_ID}
      --github-client-secret ${GITHUB_CLIENT_SECRET}
      --github-allowed-users ${GITHUB_ALLOWED_USER}
      --proxy-bearer-token ${C3PO_PROXY_BEARER_TOKEN}
      --listen :8421
      --no-auto-tls
      --trusted-proxies 172.16.0.0/12
      --http-streaming-only
      -- http://coordinator:8420
    ports:
      - "127.0.0.1:8421:8421"
    volumes:
      - auth_proxy_data:/data
    depends_on:
      - coordinator
    restart: unless-stopped
    deploy:
      resources:
        limits:
          cpus: "0.5"
          memory: 64M

  redis:
    image: redis:7-alpine
    volumes:
      - redis_data:/data
    command: >-
      redis-server
      --appendonly yes
      --requirepass ${REDIS_PASSWORD}
    restart: unless-stopped
    read_only: true
    tmpfs:
      - /tmp:size=10M
    cap_drop:
      - ALL
    cap_add:
      - SETUID
      - SETGID
    security_opt:
      - no-new-privileges:true
    deploy:
      resources:
        limits:
          cpus: "0.5"
          memory: 64M
    healthcheck:
      test: ["CMD", "redis-cli", "-a", "${REDIS_PASSWORD}", "ping"]
      interval: 10s
      timeout: 5s
      retries: 3

volumes:
  redis_data:
  auth_proxy_data:
COMPOSE_EOF

# Create .env file for docker-compose variable substitution
ssh_run "cat > ${REMOTE_DIR}/.env << EOF
REDIS_PASSWORD=${REDIS_PASSWORD}
C3PO_PROXY_BEARER_TOKEN=${PROXY_BEARER_TOKEN}
C3PO_HOOK_SECRET=${HOOK_SECRET}
GITHUB_CLIENT_ID=${GITHUB_CLIENT_ID}
GITHUB_CLIENT_SECRET=${GITHUB_CLIENT_SECRET}
GITHUB_ALLOWED_USER=${GITHUB_ALLOWED_USER}
EOF
chmod 600 ${REMOTE_DIR}/.env"

# -------------------------------------------------------------------
# Step 5: Create user-level systemd service
# -------------------------------------------------------------------
log "Creating systemd user service..."
ssh_run "mkdir -p ~/.config/systemd/user"
ssh_run "cat > ~/.config/systemd/user/c3po.service" << 'SERVICE_EOF'
[Unit]
Description=C3PO Multi-Agent Coordinator
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/home/mansel/c3po
ExecStartPre=/usr/bin/docker-compose pull redis
ExecStart=/usr/bin/docker-compose up --force-recreate --remove-orphans
ExecStop=/usr/bin/docker-compose down
Restart=on-failure
RestartSec=10
TimeoutStartSec=120
TimeoutStopSec=30

[Install]
WantedBy=default.target
SERVICE_EOF

log "Enabling and starting c3po service..."
ssh_run "systemctl --user daemon-reload"
ssh_run "systemctl --user enable c3po.service"
ssh_run "systemctl --user restart c3po.service"

# Wait for containers to come up
log "Waiting for containers to start..."
sleep 10

# -------------------------------------------------------------------
# Step 6: Create nginx config
# -------------------------------------------------------------------
log "Creating nginx config for mcp.qerk.be..."

# Check if TLS cert already exists (from previous certbot run)
HAS_TLS=false
if ssh_run "grep -q 'ssl_certificate' /etc/nginx/sites-available/mcp.qerk.be 2>/dev/null"; then
    log "TLS certificate found — generating config with HTTPS"
    HAS_TLS=true
else
    warn "No TLS certificate found — generating HTTP-only config"
    warn "After first deploy, run: sudo certbot --nginx -d mcp.qerk.be"
fi

ssh_run "cat > ${REMOTE_DIR}/nginx-mcp-qerk-be.conf" << NGINX_EOF
# C3PO Nginx config for mcp.qerk.be (OAuth + mcp-auth-proxy)
# Install with:
#   sudo cp ~/c3po/nginx-mcp-qerk-be.conf /etc/nginx/sites-available/mcp.qerk.be
#   sudo ln -sf /etc/nginx/sites-available/mcp.qerk.be /etc/nginx/sites-enabled/
#   sudo nginx -t && sudo systemctl reload nginx

upstream c3po_proxy {
    server 127.0.0.1:8421;
    keepalive 8;
}

upstream c3po_coordinator {
    server 127.0.0.1:8420;
    keepalive 8;
}

# Hook secret validation for REST endpoints
map \$http_x_c3po_hook_secret \$hook_auth_valid {
    "${HOOK_SECRET}" 1;
    default 0;
}

# Rate limit zones
limit_req_zone \$binary_remote_addr zone=c3po_api:10m rate=30r/s;
limit_req_zone \$binary_remote_addr zone=c3po_admin:1m rate=5r/m;

server {
    server_name mcp.qerk.be;

    # Security headers
    add_header X-Content-Type-Options "nosniff" always;
    add_header X-Frame-Options "DENY" always;
    add_header Referrer-Policy "strict-origin-when-cross-origin" always;

    # Request body size limit
    client_max_body_size 64k;

    # Health endpoint - no auth required
    location = /api/health {
        limit_req zone=c3po_api burst=10 nodelay;
        proxy_pass http://c3po_coordinator;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }

    # Admin endpoints - stricter rate limit
    location /api/admin/ {
        if (\$hook_auth_valid = 0) {
            return 401 '{"error": "Unauthorized"}';
        }
        limit_req zone=c3po_admin burst=3 nodelay;
        proxy_set_header X-C3PO-Hook-Secret "";
        proxy_set_header Authorization "Bearer ${PROXY_BEARER_TOKEN}";
        proxy_pass http://c3po_coordinator;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }

    # Hook REST endpoints - secret header auth
    location /api/ {
        if (\$hook_auth_valid = 0) {
            return 401 '{"error": "Unauthorized"}';
        }
        limit_req zone=c3po_api burst=10 nodelay;
        # Strip hook secret, inject proxy bearer token
        proxy_set_header X-C3PO-Hook-Secret "";
        proxy_set_header Authorization "Bearer ${PROXY_BEARER_TOKEN}";
        proxy_pass http://c3po_coordinator;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }

    # OAuth discovery endpoints
    location /.well-known/ {
        proxy_pass http://c3po_proxy;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }

    # OAuth IdP endpoints
    location /.idp/ {
        proxy_pass http://c3po_proxy;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }

    # OAuth callback endpoints
    location /.auth/ {
        proxy_pass http://c3po_proxy;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }

    # MCP endpoint - supports SSE for long-poll (OAuth via mcp-auth-proxy)
    location /mcp {
        limit_req zone=c3po_api burst=20 nodelay;
        proxy_pass http://c3po_proxy;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_set_header Connection "";
        proxy_http_version 1.1;
        # SSE/long-poll support
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 3700s;
    }

    # Headless MCP endpoint - hook secret auth (no browser/OAuth needed)
    location /mcp-headless {
        if (\$hook_auth_valid = 0) {
            return 401 '{"error": "Unauthorized: invalid hook secret"}';
        }
        limit_req zone=c3po_api burst=20 nodelay;
        # Strip hook secret, inject proxy bearer token, rewrite path to /mcp
        proxy_set_header X-C3PO-Hook-Secret "";
        proxy_set_header Authorization "Bearer ${PROXY_BEARER_TOKEN}";
        rewrite ^/mcp-headless(.*)$ /mcp\$1 break;
        proxy_pass http://c3po_coordinator;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_set_header Connection "";
        proxy_http_version 1.1;
        # SSE/long-poll support
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 3700s;
    }

    # Deny everything else
    location / {
        return 404;
    }
NGINX_EOF

# Append TLS or HTTP listen directives
if [ "$HAS_TLS" = true ]; then
ssh_run "cat >> ${REMOTE_DIR}/nginx-mcp-qerk-be.conf" << 'TLS_EOF'

    listen 443 ssl;
    ssl_certificate /etc/letsencrypt/live/mcp.qerk.be/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/mcp.qerk.be/privkey.pem;
    include /etc/letsencrypt/options-ssl-nginx.conf;
    ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem;
}

# HTTP -> HTTPS redirect
server {
    listen 80;
    server_name mcp.qerk.be;
    return 301 https://$host$request_uri;
}
TLS_EOF
else
ssh_run "cat >> ${REMOTE_DIR}/nginx-mcp-qerk-be.conf" << 'HTTP_EOF'

    listen 80;
}
HTTP_EOF
fi

# -------------------------------------------------------------------
# Step 7: Verify deployment
# -------------------------------------------------------------------
log "Checking container status..."
ssh_run "docker ps --filter name=c3po --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'" || true

log "Checking systemd service..."
ssh_run "systemctl --user status c3po.service --no-pager" 2>&1 | head -10 || true

log "Health check..."
if ssh_run "curl -sf http://127.0.0.1:8420/api/health 2>/dev/null"; then
    echo ""
    log "Coordinator is UP and healthy!"
else
    warn "Coordinator not responding yet (may still be starting)"
    warn "Check logs with: ssh ${REMOTE} 'docker-compose -f ~/c3po/docker-compose.yml logs'"
fi

# -------------------------------------------------------------------
# Done — print setup info
# -------------------------------------------------------------------
echo ""
log "========================================="
log "Deployment complete (containers running)"
log "========================================="
echo ""
warn "NOW RUN THESE SUDO COMMANDS on the server:"
echo ""
echo "  # 1. Install nginx config"
echo "  sudo cp ~/c3po/nginx-mcp-qerk-be.conf /etc/nginx/sites-available/mcp.qerk.be"
echo "  sudo ln -sf /etc/nginx/sites-available/mcp.qerk.be /etc/nginx/sites-enabled/"
echo "  sudo nginx -t"
echo "  sudo systemctl restart nginx"
echo ""
echo "  # 2. Get TLS certificate (after CNAME is in place)"
echo "  sudo certbot --nginx -d mcp.qerk.be"
echo ""
warn "Hook secret for client setup:"
echo "  ${HOOK_SECRET}"
echo ""
warn "To enroll a client:"
echo "  C3PO_HOOK_SECRET=${HOOK_SECRET} claude /c3po setup"
echo ""
