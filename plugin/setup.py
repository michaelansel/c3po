#!/usr/bin/env python3
"""
C3PO Setup Script - Plugin-based enrollment.

This script runs when the user executes `claude --init` or `claude --maintenance`
with the c3po plugin installed. It guides the user through configuring the
coordinator connection.

Exit codes:
- 0: Setup completed successfully or skipped
- 1: Setup failed (should not block Claude Code startup)
"""

import json
import os
import re
import subprocess
import sys
import urllib.request
import urllib.error


# ANSI colors
GREEN = '\033[0;32m'
YELLOW = '\033[1;33m'
RED = '\033[0;31m'
BLUE = '\033[0;34m'
NC = '\033[0m'


def log(msg: str) -> None:
    """Log an info message."""
    print(f"{GREEN}[c3po]{NC} {msg}")


def warn(msg: str) -> None:
    """Log a warning message."""
    print(f"{YELLOW}[c3po]{NC} {msg}")


def error(msg: str) -> None:
    """Log an error message."""
    print(f"{RED}[c3po]{NC} {msg}", file=sys.stderr)


def info(msg: str) -> None:
    """Log a blue info message."""
    print(f"{BLUE}[c3po]{NC} {msg}")


def prompt(msg: str, default: str = "") -> str:
    """Prompt user for input with optional default."""
    if default:
        result = input(f"{msg} [{default}]: ").strip()
        return result if result else default
    return input(f"{msg}: ").strip()


def check_existing_config() -> dict | None:
    """Check if c3po MCP is already configured."""
    try:
        result = subprocess.run(
            ["claude", "mcp", "list"],
            capture_output=True,
            text=True,
            timeout=10
        )
        if result.returncode == 0 and "c3po" in result.stdout:
            # Parse the existing config
            for line in result.stdout.splitlines():
                if "c3po" in line:
                    return {"exists": True, "line": line.strip()}
        return None
    except Exception:
        return None


def validate_url(url: str) -> str | None:
    """Validate and normalize URL format."""
    url = url.strip()
    if not url:
        return None

    # Add http:// if no scheme
    if not url.startswith(("http://", "https://")):
        url = f"http://{url}"

    # Basic URL validation
    if not re.match(r'^https?://[^\s/$.?#].[^\s]*$', url):
        return None

    # Strip trailing slash
    return url.rstrip("/")


def validate_agent_id(agent_id: str) -> str | None:
    """Validate agent ID format."""
    agent_id = agent_id.strip()
    if not agent_id:
        return None

    # Length check
    if len(agent_id) < 1 or len(agent_id) > 64:
        return None

    # Character check: must start with alphanumeric, then alphanumeric/._-
    if not re.match(r'^[a-zA-Z0-9][a-zA-Z0-9._-]*$', agent_id):
        return None

    return agent_id


def check_coordinator(url: str) -> dict | None:
    """Check if coordinator is reachable and get health info."""
    try:
        req = urllib.request.Request(
            f"{url}/api/health",
            headers={"User-Agent": "c3po-setup/1.0"}
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
            if "status" in data:
                return data
    except Exception:
        pass
    return None


def add_mcp_server(url: str, agent_id: str) -> bool:
    """Add c3po MCP server to Claude Code config."""
    try:
        result = subprocess.run(
            [
                "claude", "mcp", "add", "c3po",
                f"{url}/mcp",
                "-t", "http",
                "-s", "user",
                "-H", f"X-Agent-ID: {agent_id}"
            ],
            capture_output=True,
            text=True,
            timeout=30
        )
        return result.returncode == 0
    except Exception as e:
        error(f"Failed to add MCP server: {e}")
        return False


def remove_existing_config() -> bool:
    """Remove existing c3po MCP configuration."""
    try:
        result = subprocess.run(
            ["claude", "mcp", "remove", "c3po"],
            capture_output=True,
            text=True,
            timeout=10
        )
        return result.returncode == 0
    except Exception:
        return False


def run_setup() -> int:
    """Run the interactive setup process."""
    print()
    print(f"{GREEN}{'═' * 60}{NC}")
    print(f"{GREEN}  C3PO Setup - Multi-Agent Coordination{NC}")
    print(f"{GREEN}{'═' * 60}{NC}")
    print()

    # Check for existing configuration
    existing = check_existing_config()
    if existing:
        warn("C3PO is already configured:")
        print(f"  {existing.get('line', 'c3po MCP server')}")
        print()
        response = prompt("Reconfigure C3PO? (y/N)", "n").lower()
        if response != "y":
            log("Keeping existing configuration.")
            return 0

        log("Removing existing configuration...")
        remove_existing_config()

    # Get coordinator URL
    print()
    info("Enter the URL of your C3PO coordinator.")
    info("Example: http://nas.local:8420 or http://localhost:8420")
    print()

    coordinator_url = None
    while not coordinator_url:
        url_input = prompt("Coordinator URL")
        if not url_input:
            warn("Setup cancelled.")
            return 0

        coordinator_url = validate_url(url_input)
        if not coordinator_url:
            error("Invalid URL format. Please enter a valid HTTP/HTTPS URL.")
            continue

        # Check if coordinator is reachable
        info(f"Checking coordinator at {coordinator_url}...")
        health = check_coordinator(coordinator_url)
        if not health:
            error(f"Cannot reach coordinator at {coordinator_url}")
            retry = prompt("Try a different URL? (Y/n)", "y").lower()
            if retry != "n":
                coordinator_url = None
                continue
            else:
                warn("Continuing without verification (coordinator may be offline).")
        else:
            agents = health.get("agents_online", 0)
            log(f"Coordinator online! {agents} agent(s) currently connected.")

    # Get agent ID
    print()
    default_agent_id = os.path.basename(os.getcwd())
    info(f"Enter an identifier for this agent (default: {default_agent_id})")
    info("This should be unique and descriptive (e.g., 'homeassistant', 'meshtastic').")
    print()

    agent_id = None
    while not agent_id:
        id_input = prompt("Agent ID", default_agent_id)
        agent_id = validate_agent_id(id_input)
        if not agent_id:
            error("Invalid agent ID. Must be 1-64 chars, alphanumeric start, may contain ._-")

    # Configure MCP server
    print()
    log("Configuring Claude Code...")

    if not add_mcp_server(coordinator_url, agent_id):
        error("Failed to configure MCP server.")
        error("You can try manual setup with:")
        print(f"  claude mcp add c3po {coordinator_url}/mcp -t http -s user -H \"X-Agent-ID: {agent_id}\"")
        return 1

    # Success!
    print()
    print(f"{GREEN}{'═' * 60}{NC}")
    print(f"{GREEN}  C3PO Setup Complete!{NC}")
    print(f"{GREEN}{'═' * 60}{NC}")
    print()
    print(f"  Coordinator: {coordinator_url}")
    print(f"  Agent ID:    {agent_id}")
    print()
    print("  Next steps:")
    print("    1. Restart Claude Code to connect")
    print("    2. Use 'list_agents' tool to see online agents")
    print("    3. Use '/coordinate status' to check connection")
    print()
    print(f"{GREEN}{'═' * 60}{NC}")

    return 0


def main() -> None:
    """Entry point for setup script."""
    # Check if running interactively
    if not sys.stdin.isatty():
        # Non-interactive mode - skip setup
        log("Non-interactive mode detected. Run 'claude --init' to configure C3PO.")
        sys.exit(0)

    try:
        exit_code = run_setup()
        sys.exit(exit_code)
    except KeyboardInterrupt:
        print()
        warn("Setup cancelled.")
        sys.exit(0)
    except Exception as e:
        error(f"Setup failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
