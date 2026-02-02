#!/usr/bin/env python3
"""
C3PO Setup Script - Plugin-based enrollment.

This script runs when the user executes `claude --init` or `claude --maintenance`
with the c3po plugin installed. It guides the user through configuring the
coordinator connection and enrolling with an API key.

Can also be run directly with --enroll for non-interactive enrollment:
  python3 setup.py --enroll <url> <admin_key> [--machine <name>] [--pattern <glob>]

Exit codes:
- 0: Setup completed successfully or skipped
- 1: Setup failed (should not block Claude Code startup)
"""

from __future__ import annotations

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


CREDENTIALS_FILE = os.path.expanduser("~/.claude/c3po-credentials.json")


def load_credentials() -> dict:
    """Load existing credentials."""
    try:
        with open(CREDENTIALS_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_credentials(credentials: dict) -> None:
    """Save credentials with 0o600 permissions."""
    os.makedirs(os.path.dirname(CREDENTIALS_FILE), exist_ok=True)
    fd = os.open(CREDENTIALS_FILE, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(credentials, f, indent=2)
        f.write("\n")


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

    if len(agent_id) < 1 or len(agent_id) > 64:
        return None

    if not re.match(r'^[a-zA-Z0-9][a-zA-Z0-9._/-]*$', agent_id):
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


def enroll_api_key(url: str, admin_key: str, agent_pattern: str = "*",
                   description: str = "") -> dict | None:
    """Enroll by creating an API key via the admin endpoint.

    Args:
        url: Coordinator base URL
        admin_key: Admin key for authentication
        agent_pattern: fnmatch pattern for allowed agent IDs
        description: Human-readable description

    Returns:
        Dict with key_id, api_key, server_secret info, or None on failure
    """
    body = json.dumps({
        "agent_pattern": agent_pattern,
        "description": description,
    }).encode()

    req = urllib.request.Request(
        f"{url}/admin/api/keys",
        data=body,
        headers={
            "Authorization": f"Bearer {admin_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        error(f"Enrollment failed: HTTP {e.code}")
        try:
            error(f"  {e.read().decode()}")
        except Exception:
            pass
    except urllib.error.URLError as e:
        error(f"Enrollment failed: {e.reason}")
    except Exception as e:
        error(f"Enrollment failed: {type(e).__name__}: {e}")
    return None


def get_default_machine_name() -> str:
    """Generate default machine name from hostname."""
    import platform
    return platform.node().split('.')[0]


def add_mcp_server(url: str, machine_name: str, server_secret: str = "",
                   api_key: str = "") -> bool:
    """Add c3po MCP server to Claude Code config.

    Uses /agent/mcp endpoint with API key auth (Bearer server_secret.api_key).
    """
    machine_name_header = f"${{C3PO_MACHINE_NAME:-{machine_name}}}"
    project_header = "${C3PO_PROJECT_NAME:-${PWD##*/}}"
    session_id_header = "${C3PO_SESSION_ID:-$$}"

    cmd = [
        "claude", "mcp", "add", "c3po",
        f"{url}/agent/mcp",
        "-t", "http",
        "-s", "user",
        "-H", f"X-Machine-Name: {machine_name_header}",
        "-H", f"X-Project-Name: {project_header}",
        "-H", f"X-Session-ID: {session_id_header}",
    ]

    # Add Authorization header with API key
    if server_secret and api_key:
        cmd.extend(["-H", f"Authorization: Bearer {server_secret}.{api_key}"])

    try:
        result = subprocess.run(
            cmd,
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


def run_enroll(url: str, admin_key: str, machine_name: str | None = None,
               pattern: str | None = None) -> int:
    """Non-interactive enrollment via CLI flags."""
    url = validate_url(url)
    if not url:
        error("Invalid URL")
        return 1

    if not machine_name:
        machine_name = get_default_machine_name()

    if not pattern:
        pattern = f"{machine_name}/*"

    log(f"Enrolling with {url}...")
    log(f"  Machine: {machine_name}")
    log(f"  Pattern: {pattern}")

    # Create API key
    result = enroll_api_key(url, admin_key, agent_pattern=pattern,
                           description=f"Auto-enrolled: {machine_name}")
    if not result:
        return 1

    key_id = result["key_id"]
    api_key = result["api_key"]
    log(f"  Key ID: {key_id}")

    # We need the server secret from the admin — prompt or env var
    server_secret = os.environ.get("C3PO_SERVER_SECRET", "")
    if not server_secret:
        error("C3PO_SERVER_SECRET environment variable is required for enrollment.")
        error("This is the server secret configured on the coordinator.")
        return 1

    # Save credentials
    credentials = {
        "coordinator_url": url,
        "server_secret": server_secret,
        "api_key": api_key,
        "key_id": key_id,
        "agent_pattern": pattern,
    }
    save_credentials(credentials)
    log(f"Credentials saved to {CREDENTIALS_FILE}")

    # Configure MCP server
    remove_existing_config()
    if not add_mcp_server(url, machine_name, server_secret, api_key):
        error("Failed to configure MCP server")
        return 1

    log("Enrollment complete!")
    return 0


def run_setup() -> int:
    """Run the interactive setup process."""
    print()
    print(f"{GREEN}{'=' * 60}{NC}")
    print(f"{GREEN}  C3PO Setup - Multi-Agent Coordination{NC}")
    print(f"{GREEN}{'=' * 60}{NC}")
    print()

    # Check for existing configuration
    existing = check_existing_config()
    existing_creds = load_credentials()

    if existing or existing_creds:
        warn("C3PO is already configured:")
        if existing:
            print(f"  MCP: {existing.get('line', 'c3po MCP server')}")
        if existing_creds:
            print(f"  Credentials: {existing_creds.get('coordinator_url', 'unknown')} (key: {existing_creds.get('key_id', 'none')})")
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
    info("Example: https://mcp.qerk.be or http://localhost:8420")
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

    # Get machine name (auto-generate from hostname)
    print()
    default_machine_name = get_default_machine_name()
    info(f"Machine name will be: {default_machine_name}")
    info("(Based on hostname - override with C3PO_MACHINE_NAME env var)")
    info("(Project context is added automatically per-session)")
    print()

    response = prompt("Use a different machine name? (y/N)", "n").lower()
    if response == "y":
        machine_name = None
        while not machine_name:
            id_input = prompt("Machine name", default_machine_name)
            machine_name = validate_agent_id(id_input)
            if not machine_name:
                error("Invalid machine name. Must be 1-64 chars, alphanumeric start, may contain ._-/")
    else:
        machine_name = default_machine_name

    # Enrollment: get admin key to create per-agent API key
    print()
    info("API key enrollment authenticates this machine with the coordinator.")
    info("You need the admin key (displayed during coordinator deployment).")
    print()

    admin_key = prompt("Admin key (leave blank to skip enrollment)", "").strip()
    server_secret = ""
    api_key = ""
    key_id = ""
    agent_pattern = f"{machine_name}/*"

    if admin_key:
        # Get server secret
        server_secret = os.environ.get("C3PO_SERVER_SECRET", "")
        if not server_secret:
            info("The server secret is configured on the coordinator (C3PO_SERVER_SECRET).")
            server_secret = prompt("Server secret").strip()

        if not server_secret:
            error("Server secret is required for enrollment.")
            return 1

        # Enroll — create API key
        info(f"Creating API key with pattern: {agent_pattern}")
        enrollment = enroll_api_key(
            coordinator_url, admin_key,
            agent_pattern=agent_pattern,
            description=f"Setup: {machine_name}",
        )

        if enrollment:
            api_key = enrollment["api_key"]
            key_id = enrollment["key_id"]
            log(f"API key created (ID: {key_id})")

            # Save credentials
            credentials = {
                "coordinator_url": coordinator_url,
                "server_secret": server_secret,
                "api_key": api_key,
                "key_id": key_id,
                "agent_pattern": agent_pattern,
            }
            save_credentials(credentials)
            log(f"Credentials saved to {CREDENTIALS_FILE}")
        else:
            error("Enrollment failed. You can retry later with:")
            error(f"  python3 {__file__} --enroll {coordinator_url} <admin_key>")
            warn("Continuing setup without API key (dev mode).")
    else:
        warn("Skipping enrollment. Hooks will use dev mode (no auth).")

    # Configure MCP server
    print()
    log("Configuring Claude Code...")

    if not add_mcp_server(coordinator_url, machine_name, server_secret, api_key):
        error("Failed to configure MCP server.")
        error("You can try manual setup with:")
        print(f"  claude mcp add c3po {coordinator_url}/agent/mcp -t http -s user -H \"X-Machine-Name: {machine_name}\"")
        return 1

    # Determine auth mode label
    if api_key:
        auth_mode = "API key"
    else:
        auth_mode = "Dev mode (no auth)"

    # Success!
    print()
    print(f"{GREEN}{'=' * 60}{NC}")
    print(f"{GREEN}  C3PO Setup Complete!{NC}")
    print(f"{GREEN}{'=' * 60}{NC}")
    print()
    print(f"  Coordinator: {coordinator_url}")
    print(f"  Machine name: {machine_name}")
    print(f"  Auth mode:    {auth_mode}")
    if key_id:
        print(f"  Key ID:       {key_id}")
    print()
    print("  Next steps:")
    print("    1. Restart Claude Code to connect")
    print("    2. Use 'list_agents' tool to see online agents")
    print("    3. Use '/c3po status' to check connection")
    print()
    print(f"{GREEN}{'=' * 60}{NC}")

    return 0


def main() -> None:
    """Entry point for setup script."""
    # Handle --enroll flag for non-interactive mode
    if len(sys.argv) >= 4 and sys.argv[1] == "--enroll":
        url = sys.argv[2]
        admin_key = sys.argv[3]
        machine_name = None
        pattern = None

        i = 4
        while i < len(sys.argv):
            if sys.argv[i] == "--machine" and i + 1 < len(sys.argv):
                machine_name = sys.argv[i + 1]
                i += 2
            elif sys.argv[i] == "--pattern" and i + 1 < len(sys.argv):
                pattern = sys.argv[i + 1]
                i += 2
            else:
                i += 1

        exit_code = run_enroll(url, admin_key, machine_name, pattern)
        sys.exit(exit_code)

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
