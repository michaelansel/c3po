#!/bin/bash
# Run on the OLD server (a10.lambda.qerk.be)
# Pulls GITHUB_* vars from .secrets/.env and appends them to the new server's .env

set -euo pipefail

OLD_DIR=/home/ubuntu/c3po
NEW=exedev@golf-pegasus.exe.xyz
NEW_ENV=/home/exedev/c3po/.env

get() {
    grep -m1 "$1" "$OLD_DIR/.secrets" "$OLD_DIR/.env" 2>/dev/null | head -1 | sed 's/^[^:]*://'
}

GITHUB_CLIENT_ID=$(get GITHUB_CLIENT_ID | cut -d= -f2-)
GITHUB_CLIENT_SECRET=$(get GITHUB_CLIENT_SECRET | cut -d= -f2-)
GITHUB_ALLOWED_USER=$(get GITHUB_ALLOWED_USER | cut -d= -f2-)

echo "GITHUB_CLIENT_ID=$GITHUB_CLIENT_ID"
echo "GITHUB_CLIENT_SECRET=${GITHUB_CLIENT_SECRET:0:8}..."
echo "GITHUB_ALLOWED_USER=$GITHUB_ALLOWED_USER"

ssh "$NEW" "grep -q GITHUB_CLIENT_ID $NEW_ENV 2>/dev/null && echo 'Already present, skipping' && exit 0; cat >> $NEW_ENV" <<EOF
GITHUB_CLIENT_ID=${GITHUB_CLIENT_ID}
GITHUB_CLIENT_SECRET=${GITHUB_CLIENT_SECRET}
GITHUB_ALLOWED_USER=${GITHUB_ALLOWED_USER}
EOF

ssh "$NEW" "sudo systemctl restart c3po"
echo "Done. Secrets pushed and service restarted."
