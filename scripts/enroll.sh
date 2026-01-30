#!/bin/bash
# C3PO Enrollment Script
# Join a Claude Code instance to the C3PO coordination network
#
# Usage:
#   curl -sSL .../enroll.sh | bash -s -- <coordinator-url> [agent-id]
#   ./scripts/enroll.sh <coordinator-url> [agent-id]
#
# Examples:
#   ./scripts/enroll.sh http://nas.local:8420
#   ./scripts/enroll.sh http://nas.local:8420 my-project

set -euo pipefail

# Configuration
COORDINATOR_URL="${1:-}"
AGENT_ID="${2:-$(basename "$PWD")}"
ADMIN_KEY="${C3PO_ADMIN_KEY:-}"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log()   { echo -e "${GREEN}[c3po]${NC} $1"; }
warn()  { echo -e "${YELLOW}[c3po]${NC} $1"; }
error() { echo -e "${RED}[c3po]${NC} $1" >&2; }
info()  { echo -e "${BLUE}[c3po]${NC} $1"; }

usage() {
    cat <<EOF
C3PO Enrollment Script

Usage: $0 <coordinator-url> [agent-id]

Arguments:
    coordinator-url    URL of the C3PO coordinator (required)
                       Example: http://nas.local:8420
    agent-id           Identifier for this agent (default: current folder name)

Environment:
    C3PO_ADMIN_KEY     Admin bearer token for API key generation (optional)
                       Format: <server_secret>.<admin_key>
                       When set, enrollment generates a per-agent API key.

Examples:
    $0 http://localhost:8420
    $0 http://nas.local:8420 my-project
    C3PO_ADMIN_KEY=secret.adminkey $0 http://nas:8420 my-agent
    curl -sSL .../enroll.sh | bash -s -- http://nas:8420 my-agent

EOF
    exit 1
}

# Check requirements
check_requirements() {
    # Check for curl
    if ! command -v curl &> /dev/null; then
        error "curl is required but not installed"
        exit 1
    fi

    # Check for Claude Code
    if ! command -v claude &> /dev/null; then
        error "Claude Code is not installed or not in PATH"
        error "Install from: https://github.com/anthropics/claude-code"
        exit 1
    fi
}

# Validate coordinator URL format
validate_url() {
    local url="$1"

    # Basic URL validation
    if [[ ! "$url" =~ ^https?:// ]]; then
        error "Invalid URL format: $url"
        error "URL must start with http:// or https://"
        exit 1
    fi

    # Strip trailing slash
    echo "${url%/}"
}

# Check if coordinator is reachable
check_coordinator() {
    local url="$1"

    log "Checking coordinator at $url..."

    local health_response
    health_response=$(curl -sf --connect-timeout 5 "$url/api/health" 2>/dev/null) || {
        error "Cannot reach coordinator at $url"
        error "Make sure the coordinator is running and accessible"
        exit 1
    }

    # Verify response contains expected fields
    if ! echo "$health_response" | grep -q '"status"'; then
        error "Invalid response from coordinator"
        error "Response: $health_response"
        exit 1
    fi

    log "Coordinator is online"
    echo "$health_response"
}

# Validate agent ID
validate_agent_id() {
    local id="$1"

    # Check length
    if [[ ${#id} -lt 1 || ${#id} -gt 64 ]]; then
        error "Agent ID must be 1-64 characters"
        exit 1
    fi

    # Check allowed characters
    if [[ ! "$id" =~ ^[a-zA-Z0-9][a-zA-Z0-9._-]*$ ]]; then
        error "Agent ID must start with alphanumeric and contain only: a-z A-Z 0-9 . _ -"
        exit 1
    fi

    echo "$id"
}

# Check if c3po is already configured
check_existing() {
    local existing
    existing=$(claude mcp list 2>/dev/null | grep -E "^\s*c3po\s+" || true)

    if [[ -n "$existing" ]]; then
        warn "C3PO MCP server is already configured:"
        echo "  $existing"
        echo ""
        read -p "Replace existing configuration? [y/N] " -n 1 -r
        echo
        if [[ ! $REPLY =~ ^[Yy]$ ]]; then
            log "Keeping existing configuration"
            exit 0
        fi

        # Remove existing
        log "Removing existing c3po configuration..."
        claude mcp remove c3po 2>/dev/null || true
    fi
}

# Generate API key using admin credentials
generate_api_key() {
    local url="$1"
    local machine_name="$2"

    if [[ -z "$ADMIN_KEY" ]]; then
        return 1  # No admin key, skip API key generation
    fi

    log "Generating API key for ${machine_name}/*..." >&2

    local response
    response=$(curl -sf --connect-timeout 5 \
        -X POST "$url/api/admin/keys" \
        -H "Authorization: Bearer $ADMIN_KEY" \
        -H "Content-Type: application/json" \
        -d "{\"agent_pattern\": \"${machine_name}/*\", \"description\": \"Enrolled via enroll.sh\"}" \
        2>/dev/null) || {
        warn "Could not generate API key (admin endpoint may not be available)"
        return 1
    }

    # Extract bearer_token from JSON response
    local bearer_token
    bearer_token=$(echo "$response" | python3 -c "import sys,json; print(json.load(sys.stdin)['bearer_token'])" 2>/dev/null) || {
        warn "Could not parse API key response"
        return 1
    }

    echo "$bearer_token"
}

# Add MCP server to Claude Code
add_mcp_server() {
    local url="$1"
    local agent_id="$2"
    local api_key="${3:-}"

    log "Configuring Claude Code MCP server..."

    # Build header arguments
    local header_args=(-H "X-Agent-ID: $agent_id")
    if [[ -n "$api_key" ]]; then
        header_args+=(-H "Authorization: Bearer $api_key")
    fi

    # Add with user scope so it works from any directory
    if claude mcp add c3po "$url/mcp" \
        -t http \
        -s user \
        "${header_args[@]}"; then
        log "MCP server added successfully"
    else
        error "Failed to add MCP server"
        exit 1
    fi
}

# Verify the configuration works
verify_config() {
    local url="$1"
    local agent_id="$2"

    log "Verifying configuration..."

    # Check the MCP server is listed
    if ! claude mcp list 2>/dev/null | grep -q "c3po"; then
        error "Configuration verification failed - c3po not in mcp list"
        exit 1
    fi

    # Make a test request to the coordinator
    local test_response
    test_response=$(curl -sf "$url/api/health" -H "X-Agent-ID: $agent_id" 2>/dev/null) || {
        warn "Could not verify connection to coordinator"
        warn "This may be normal if the coordinator requires specific headers"
    }

    log "Configuration verified"
}

# Print success message
print_success() {
    local url="$1"
    local agent_id="$2"

    echo ""
    echo -e "${GREEN}═══════════════════════════════════════════════════════════════${NC}"
    echo -e "${GREEN}  C3PO Enrollment Complete!${NC}"
    echo -e "${GREEN}═══════════════════════════════════════════════════════════════${NC}"
    echo ""
    echo "  Coordinator: $url"
    echo "  Agent ID:    $agent_id"
    echo ""
    echo "  Next steps:"
    echo "    1. Start (or restart) Claude Code"
    echo "    2. Use 'list_agents' tool to see online agents"
    echo "    3. Use 'send_request' to message other agents"
    echo ""
    echo "  Quick test:"
    echo "    claude -p \"Use the list_agents tool from c3po\""
    echo ""
    echo -e "${GREEN}═══════════════════════════════════════════════════════════════${NC}"
}

# Main
main() {
    # Check for help or missing args
    if [[ "$COORDINATOR_URL" == "-h" || "$COORDINATOR_URL" == "--help" || -z "$COORDINATOR_URL" ]]; then
        usage
    fi

    check_requirements

    # Validate and normalize inputs
    COORDINATOR_URL=$(validate_url "$COORDINATOR_URL")
    AGENT_ID=$(validate_agent_id "$AGENT_ID")

    info "Enrolling agent '$AGENT_ID' with coordinator at $COORDINATOR_URL"
    echo ""

    # Check coordinator first
    check_coordinator "$COORDINATOR_URL"

    # Check for existing config (interactive)
    # Skip if running in pipe mode (no TTY)
    if [[ -t 0 ]]; then
        check_existing
    else
        # Non-interactive: just remove existing silently
        claude mcp remove c3po 2>/dev/null || true
    fi

    # Generate API key if admin credentials are available
    API_KEY=""
    if [[ -n "$ADMIN_KEY" ]]; then
        API_KEY=$(generate_api_key "$COORDINATOR_URL" "$AGENT_ID")
        if [[ -n "$API_KEY" ]]; then
            log "API key generated successfully"
        else
            warn "Continuing without API key (auth may not be enforced)"
        fi
    fi

    # Add MCP server
    add_mcp_server "$COORDINATOR_URL" "$AGENT_ID" "$API_KEY"

    # API key info (hooks read it automatically from ~/.claude.json)
    if [[ -n "$API_KEY" ]]; then
        log "API key stored in ~/.claude.json (hooks read it automatically)"
    fi

    # Verify
    verify_config "$COORDINATOR_URL" "$AGENT_ID"

    # Success!
    print_success "$COORDINATOR_URL" "$AGENT_ID"
}

main "$@"
