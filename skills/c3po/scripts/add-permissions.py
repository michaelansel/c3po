#!/usr/bin/env python3
"""Add c3po permissions to the current project's .claude/settings.local.json.

Reads the canonical tool list from .claude-plugin/tools.json and ensures all
MCP tools are in the project's permission allow list, plus a Bash rule for
the /c3po auto background watcher (scripts/c3po-watch) so re-arming it
doesn't prompt.

Usage:
    python3 <plugin_root>/skills/c3po/scripts/add-permissions.py [project_dir]

If project_dir is not specified, uses $CLAUDE_PROJECT_DIR when set, otherwise
the current working directory. Run it from the project, not from the skill
directory: a project_dir inside the plugin root is refused, because writing
there (a versioned cache directory) leaves the real project without rules.
Each run also removes a stray skills/c3po/.claude/settings.local.json left by
older versions that had exactly that bug.
"""

import json
import os
import sys


def get_plugin_root() -> str:
    """Get the plugin root directory (repo root)."""
    # From skills/c3po/scripts/ directory, go up four levels to reach repo root
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def get_tools_from_hooks() -> list[str]:
    """Read the canonical tool list from .claude-plugin/tools.json."""
    tools_path = os.path.join(get_plugin_root(), ".claude-plugin", "tools.json")
    with open(tools_path) as f:
        tools = json.load(f)["tools"]
    return [f"mcp__c3po__{t['name']}" for t in tools]


def get_watcher_permission() -> str:
    """Bash permission rule for the background watcher at its installed path."""
    return f"Bash(python3 {os.path.join(get_plugin_root(), 'scripts', 'c3po-watch')}:*)"


def is_inside_plugin_root(project_dir: str) -> bool:
    """True if project_dir is the plugin root or anything beneath it."""
    root = os.path.realpath(get_plugin_root())
    path = os.path.realpath(project_dir)
    return os.path.commonpath([root, path]) == root


def remove_stray_settings() -> None:
    """Remove settings an older version wrote into the skill directory."""
    stray_dir = os.path.join(get_plugin_root(), "skills", "c3po", ".claude")
    stray_path = os.path.join(stray_dir, "settings.local.json")
    if not os.path.exists(stray_path):
        return
    os.remove(stray_path)
    try:
        os.rmdir(stray_dir)
    except OSError:
        pass  # not empty; leave anything else alone
    print(f"[c3po] Removed stray {stray_path}")


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

    # Get canonical tool list plus the watcher rule
    tools = get_tools_from_hooks() + [get_watcher_permission()]

    # Add any missing tools
    added = []
    for tool in tools:
        if tool not in allow:
            allow.append(tool)
            added.append(tool)

    if not added:
        print(f"[c3po] All {len(tools)} permissions already in {settings_path}")
        return

    # Write back
    os.makedirs(settings_dir, exist_ok=True)
    with open(settings_path, "w") as f:
        json.dump(settings, f, indent=2)
        f.write("\n")

    print(f"[c3po] Added {len(added)} permission(s) to {settings_path}")
    for tool in added:
        print(f"  + {tool}")


def main() -> None:
    if len(sys.argv) > 1:
        project_dir = sys.argv[1]
    else:
        project_dir = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()

    if not os.path.isdir(project_dir):
        print(f"Error: {project_dir} is not a directory", file=sys.stderr)
        sys.exit(1)

    try:
        remove_stray_settings()
        if is_inside_plugin_root(project_dir):
            print(
                f"Error: {project_dir} is inside the c3po plugin, not a project. "
                "Run from the project's working directory (or pass it as an argument):\n"
                f"  python3 {os.path.abspath(__file__)} \"$PWD\"",
                file=sys.stderr,
            )
            sys.exit(1)
        add_permissions(project_dir)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
