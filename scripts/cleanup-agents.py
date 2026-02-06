#!/usr/bin/env python3
"""CLI tool for listing and cleaning up stale C3PO agent registrations.

Usage:
  python3 scripts/cleanup-agents.py list                        # all agents
  python3 scripts/cleanup-agents.py list --offline              # offline only
  python3 scripts/cleanup-agents.py list --pattern "dev/*"      # filter by pattern
  python3 scripts/cleanup-agents.py remove --offline            # remove all offline
  python3 scripts/cleanup-agents.py remove --pattern "stress/*"
  python3 scripts/cleanup-agents.py remove --offline --dry-run  # preview only
  python3 scripts/cleanup-agents.py remove --offline -y         # skip confirmation

Auth (required in production):
  --admin-token TOKEN   or   C3PO_ADMIN_TOKEN env var

Coordinator URL:
  --url URL   or read from ~/.claude/c3po-credentials.json   or default http://localhost:8420
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

# Retry config: nginx REST per-IP limit is 8r/s burst=3.
# Start at 2s, double each retry, max 16s, 5 attempts total.
_RETRY_MAX_ATTEMPTS = 5
_RETRY_INITIAL_DELAY = 2.0
_RETRY_MAX_DELAY = 16.0


def _request_with_retry(method: str, url: str, **kwargs) -> httpx.Response:
    """Make an HTTP request with exponential backoff on 429 responses."""
    delay = _RETRY_INITIAL_DELAY
    for attempt in range(1, _RETRY_MAX_ATTEMPTS + 1):
        resp = httpx.request(method, url, **kwargs)
        if resp.status_code != 429:
            return resp
        if attempt == _RETRY_MAX_ATTEMPTS:
            return resp  # Give up, return the 429
        print(f"Rate limited, retrying in {delay:.0f}s...", file=sys.stderr)
        time.sleep(delay)
        delay = min(delay * 2, _RETRY_MAX_DELAY)
    return resp  # Unreachable, but satisfies type checker


def _load_coordinator_url() -> str:
    """Load coordinator URL from credentials file or default."""
    creds_path = Path.home() / ".claude" / "c3po-credentials.json"
    if creds_path.exists():
        try:
            creds = json.loads(creds_path.read_text())
            url = creds.get("coordinator_url", "")
            if url:
                return url.rstrip("/")
        except (json.JSONDecodeError, OSError):
            pass
    return "http://localhost:8420"


def _format_relative_time(iso_timestamp: str) -> str:
    """Format an ISO timestamp as a human-friendly relative time."""
    try:
        dt = datetime.fromisoformat(iso_timestamp)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        delta = now - dt
        seconds = int(delta.total_seconds())

        if seconds < 60:
            return f"{seconds}s ago"
        elif seconds < 3600:
            return f"{seconds // 60}m ago"
        elif seconds < 86400:
            return f"{seconds // 3600}h ago"
        else:
            return f"{seconds // 86400}d ago"
    except (ValueError, TypeError):
        return iso_timestamp


def _print_agents_table(agents: list[dict]) -> None:
    """Print agents in a human-friendly table format."""
    if not agents:
        print("No agents found.")
        return

    # Calculate column widths
    id_width = max(len("AGENT ID"), max(len(a["id"]) for a in agents))
    status_width = max(len("STATUS"), max(len(a.get("status", "")) for a in agents))

    # Header
    print(f"{'AGENT ID':<{id_width}}  {'STATUS':<{status_width}}  LAST SEEN")

    # Rows
    for agent in sorted(agents, key=lambda a: a.get("last_seen", ""), reverse=True):
        agent_id = agent["id"]
        status = agent.get("status", "unknown")
        last_seen = _format_relative_time(agent.get("last_seen", ""))
        print(f"{agent_id:<{id_width}}  {status:<{status_width}}  {last_seen}")

    # Summary
    online = sum(1 for a in agents if a.get("status") == "online")
    offline = len(agents) - online
    print(f"\n{len(agents)} agents ({online} online, {offline} offline)")


def cmd_list(args: argparse.Namespace) -> int:
    """List agents."""
    params = {}
    if args.offline:
        params["status"] = "offline"
    elif args.online:
        params["status"] = "online"
    if args.pattern:
        params["pattern"] = args.pattern

    headers = {}
    if args.admin_token:
        headers["Authorization"] = f"Bearer {args.admin_token}"

    try:
        resp = _request_with_retry(
            "GET",
            f"{args.url}/admin/api/agents",
            params=params,
            headers=headers,
            timeout=30,
        )
    except httpx.ConnectError:
        print(f"Error: Could not connect to {args.url}", file=sys.stderr)
        return 1

    if resp.status_code == 401:
        print("Error: Authentication required. Provide --admin-token or set C3PO_ADMIN_TOKEN.", file=sys.stderr)
        return 1
    if resp.status_code != 200:
        print(f"Error: {resp.status_code} — {resp.text}", file=sys.stderr)
        return 1

    data = resp.json()
    agents = data.get("agents", [])

    if args.json:
        print(json.dumps(data, indent=2))
    else:
        _print_agents_table(agents)

    return 0


def cmd_remove(args: argparse.Namespace) -> int:
    """Remove agents."""
    if not args.offline and not args.pattern:
        print("Error: Must specify --offline and/or --pattern for remove.", file=sys.stderr)
        return 1

    headers = {}
    if args.admin_token:
        headers["Authorization"] = f"Bearer {args.admin_token}"

    # First, list what would be removed
    list_params = {}
    if args.offline:
        list_params["status"] = "offline"
    if args.pattern:
        list_params["pattern"] = args.pattern

    try:
        resp = _request_with_retry(
            "GET",
            f"{args.url}/admin/api/agents",
            params=list_params,
            headers=headers,
            timeout=30,
        )
    except httpx.ConnectError:
        print(f"Error: Could not connect to {args.url}", file=sys.stderr)
        return 1

    if resp.status_code == 401:
        print("Error: Authentication required. Provide --admin-token or set C3PO_ADMIN_TOKEN.", file=sys.stderr)
        return 1
    if resp.status_code != 200:
        print(f"Error: {resp.status_code} — {resp.text}", file=sys.stderr)
        return 1

    agents = resp.json().get("agents", [])

    if not agents:
        print("No agents match the criteria.")
        return 0

    # Show what would be removed
    if args.json:
        print(json.dumps({"would_remove": [a["id"] for a in agents], "count": len(agents)}, indent=2))
    else:
        print("Agents to remove:")
        _print_agents_table(agents)

    if args.dry_run:
        print("\n(dry run — no changes made)")
        return 0

    # Confirm
    if not args.yes:
        try:
            answer = input(f"\nRemove {len(agents)} agent(s)? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.")
            return 1
        if answer != "y":
            print("Aborted.")
            return 1

    # Execute removal
    delete_params = {}
    if args.offline:
        delete_params["status"] = "offline"
    delete_params["pattern"] = args.pattern or "*"

    try:
        resp = _request_with_retry(
            "DELETE",
            f"{args.url}/admin/api/agents",
            params=delete_params,
            headers=headers,
            timeout=30,
        )
    except httpx.ConnectError:
        print(f"Error: Could not connect to {args.url}", file=sys.stderr)
        return 1

    if resp.status_code != 200:
        print(f"Error: {resp.status_code} — {resp.text}", file=sys.stderr)
        return 1

    data = resp.json()
    removed = data.get("removed", 0)
    removed_ids = data.get("agent_ids", [])

    if args.json:
        print(json.dumps(data, indent=2))
    else:
        print(f"\nRemoved {removed} agent(s):")
        for aid in sorted(removed_ids):
            print(f"  {aid}")

    return 0


def main():
    parser = argparse.ArgumentParser(
        description="List and clean up stale C3PO agent registrations.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--admin-token",
        default=os.environ.get("C3PO_ADMIN_TOKEN", ""),
        help="Admin token (or set C3PO_ADMIN_TOKEN env var)",
    )
    parser.add_argument(
        "--url",
        default=os.environ.get("C3PO_URL", "") or _load_coordinator_url(),
        help="Coordinator URL (default: from credentials or localhost:8420)",
    )
    parser.add_argument("--json", action="store_true", help="Output as JSON")

    subparsers = parser.add_subparsers(dest="command", required=True)

    # list subcommand
    list_parser = subparsers.add_parser("list", help="List agents")
    list_parser.add_argument("--offline", action="store_true", help="Show only offline agents")
    list_parser.add_argument("--online", action="store_true", help="Show only online agents")
    list_parser.add_argument("--pattern", help="Filter by fnmatch glob pattern")

    # remove subcommand
    remove_parser = subparsers.add_parser("remove", help="Remove agents")
    remove_parser.add_argument("--offline", action="store_true", help="Remove only offline agents")
    remove_parser.add_argument("--pattern", help="Filter by fnmatch glob pattern")
    remove_parser.add_argument("--dry-run", action="store_true", help="Preview without removing")
    remove_parser.add_argument("-y", "--yes", action="store_true", help="Skip confirmation prompt")

    args = parser.parse_args()

    if args.command == "list":
        sys.exit(cmd_list(args))
    elif args.command == "remove":
        sys.exit(cmd_remove(args))


if __name__ == "__main__":
    main()
