#!/usr/bin/env python3
"""Add c3po MCP tool permissions to the current project's .claude/settings.local.json.

Reads the canonical tool list from hooks/hooks.json (PreToolUse matcher)
and ensures all tools are in the project's permission allow list.

Usage:
    python3 add-permissions.py [project_dir]

If project_dir is not specified, uses the current working directory.
"""

import json
import os
import sys


def get_plugin_root() -> str:
    """Get the plugin root directory (repo root)."""
    # From skills/c3po/scripts/ directory, go up four levels to reach repo root
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def get_tools_from_hooks() -> list[str]:
    """Parse the canonical tool list from hooks.json PreToolUse matcher."""
    hooks_path = os.path.join(get_plugin_root(), "hooks", "hooks.json")
    with open(hooks_path) as f:
        hooks = json.load(f)

    # Find the ensure_agent_id PreToolUse matcher — it lists all MCP tools
    for entry in hooks.get("hooks", {}).get("PreToolUse", []):
        matcher = entry.get("matcher", "")
        if "mcp__c3po__" in matcher and "|" in matcher:
            return matcher.split("|")

    raise RuntimeError("Could not find c3po tool list in hooks.json PreToolUse matcher")


def add_permissions(project_dir: str) -> None:
    """Add c3po MCP tool permissions to the project's settings.local.json."""
    settings_dir = os.path.join(project_dir, ".claude")
    settings_path = os.path.join(settings_dir, "settings.local.json")

    # Load existing settings or start fresh
    if os.path.exists(settings_path):
        with open(settings_path) as f:
            settings = json.load(f)
    else:
        settings = {}

    permissions = settings.setdefault("permissions", {})
    allow = permissions.setdefault("allow", [])

    # Get canonical tool list
    tools = get_tools_from_hooks()

    # Add any missing tools
    added = []
    for tool in tools:
        if tool not in allow:
            allow.append(tool)
            added.append(tool)

    if not added:
        print(f"[c3po] All {len(tools)} MCP tools already in {settings_path}")
        return

    # Write back
    os.makedirs(settings_dir, exist_ok=True)
    with open(settings_path, "w") as f:
        json.dump(settings, f, indent=2)
        f.write("\n")

    print(f"[c3po] Added {len(added)} MCP tool permission(s) to {settings_path}")
    for tool in added:
        print(f"  + {tool}")


def main() -> None:
    project_dir = sys.argv[1] if len(sys.argv) > 1 else os.getcwd()

    if not os.path.isdir(project_dir):
        print(f"Error: {project_dir} is not a directory", file=sys.stderr)
        sys.exit(1)

    try:
        add_permissions(project_dir)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
