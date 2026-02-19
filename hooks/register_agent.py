#!/usr/bin/env python3
"""
C3PO SessionStart Hook - Register agent and show coordination context.

This hook runs when a Claude Code session starts. It registers the agent
with the coordinator, displays connection status, and provides context
about available coordination features.

Exit codes:
- 0: Always (hooks should not block session start)

Environment variables:
- C3PO_COORDINATOR_URL: Coordinator URL (default: http://localhost:8420)
- C3PO_MACHINE_NAME: Machine name identifier (default: from MCP config or hostname)
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
import urllib.error

from c3po_common import auth_headers, get_coordinator_url, get_machine_name, get_session_id, parse_hook_input, sanitize_name, save_agent_id, urlopen_with_ssl


# Configuration
COORDINATOR_URL = get_coordinator_url()
MACHINE_NAME = get_machine_name()

# Project context from Gas Town environment variables
# Format: gt-{rig}-{role}-{crew} (e.g., gt-c3po-crew-michaelansel)
# Fallback to CLAUDE_PROJECT_NAME or basename(cwd) if GT_RIG not set
gt_rig = os.environ.get("GT_RIG", "")
gt_role = os.environ.get("GT_ROLE", "")
gt_crew = os.environ.get("GT_CREW", "")

if gt_role:
    # Log all GT_* environment variables for future analysis
    if os.environ.get("C3PO_DEBUG"):
        gt_env_vars = {k: v for k, v in os.environ.items() if k.startswith("GT_")}
        print(f"[c3po:debug] GT environment variables: {gt_env_vars}", file=sys.stderr)

    # Build project name from Gas Town env vars
    # Handle special cases for roles like mayor/deacon
    if gt_role in ["mayor", "deacon"]:
        # For mayor/deacon, we use a different pattern: gt-{rig}-{role}
        # This prevents the crew from being appended inappropriately
        # Remove "gt-" prefix from rig if present to avoid double prefix
        clean_rig = gt_rig.replace("gt-", "", 1) if gt_rig.startswith("gt-") else gt_rig
        # If clean_rig is empty (GT_RIG was not set), we don't want to include it
        if clean_rig:
            components = [clean_rig, gt_role]
        else:
            components = [gt_role]
        PROJECT_NAME = sanitize_name("gt-" + "-".join(components))
        # Log for future enhancement analysis
        if os.environ.get("C3PO_DEBUG"):
            if clean_rig:
                print(f"[c3po:debug] Special role handling: {gt_role} -> gt-{clean_rig}-{gt_role}", file=sys.stderr)
            else:
                print(f"[c3po:debug] Special role handling: {gt_role} -> gt-{gt_role}", file=sys.stderr)
    else:
        # Standard crew pattern: gt-{rig}-{role}-{crew}
        # Build components properly, filtering out empty values
        components = []
        if gt_rig:
            components.append(gt_rig)
        components.append(gt_role)
        if gt_crew:  # Only add crew if it's not empty
            components.append(gt_crew)
        PROJECT_NAME = sanitize_name("gt-" + "-".join(components))
        if os.environ.get("C3PO_DEBUG"):
            if gt_rig:
                print(f"[c3po:debug] Standard role handling: {gt_role} -> gt-{gt_rig}-{gt_role}{('-' + gt_crew) if gt_crew else ''}", file=sys.stderr)
            else:
                print(f"[c3po:debug] Standard role handling: {gt_role} -> gt-{gt_role}{('-' + gt_crew) if gt_crew else ''}", file=sys.stderr)
else:
    # Fallback: use legacy behavior
    PROJECT_NAME = sanitize_name(
        os.environ.get("CLAUDE_PROJECT_NAME") or os.path.basename(os.getcwd())
    )


def register_with_coordinator(session_id: str) -> dict | None:
    """Register this session with the coordinator via REST API.

    Sends headers that coordinator uses to construct full agent_id:
    - X-Machine-Name: Machine identifier (base)
    - X-Project-Name: Project name (appended to make full agent_id)
    - X-Session-ID: Session identifier (for same-session detection)

    Returns:
        Registration result dict (with assigned agent_id) or None if failed
    """
    attempted_agent_id = f"{MACHINE_NAME}/{PROJECT_NAME}"
    if os.environ.get("C3PO_DEBUG"):
        print(f"[c3po:debug] Attempting to register agent: {attempted_agent_id}", file=sys.stderr)

    headers = {
        "X-Machine-Name": MACHINE_NAME,
        "X-Project-Name": PROJECT_NAME,
        "X-Session-ID": session_id,
    }
    headers.update(auth_headers())
    req = urllib.request.Request(
        f"{COORDINATOR_URL}/agent/api/register",
        data=b"",  # POST with empty body
        headers=headers,
        method="POST",
    )

    max_retries = 2
    retry_delay = int(os.environ.get("C3PO_RETRY_DELAY", "2"))

    for attempt in range(max_retries + 1):
        try:
            with urlopen_with_ssl(req, timeout=15) as resp:
                result = json.loads(resp.read())
                if os.environ.get("C3PO_DEBUG"):
                    print(f"[c3po:debug] Registration successful: {result}", file=sys.stderr)
                return result
        except urllib.error.HTTPError as e:
            print(f"[c3po:debug] HTTPError registering {attempted_agent_id}: {e.code}", file=sys.stderr)
            if e.code == 429 and attempt < max_retries:
                if os.environ.get("C3PO_DEBUG"):
                    print(f"[c3po:debug] Rate limited (attempt {attempt + 1}), retrying in {retry_delay}s...", file=sys.stderr)
                time.sleep(retry_delay)
                continue
            if os.environ.get("C3PO_DEBUG"):
                try:
                    error_body = e.read().decode()
                    print(f"[c3po:debug] Response body: {error_body}", file=sys.stderr)
                except Exception:
                    pass
            break
        except urllib.error.URLError as e:
            if os.environ.get("C3PO_DEBUG"):
                print(f"[c3po:debug] URLError registering {attempted_agent_id}: {e.reason}", file=sys.stderr)
            break
        except Exception as e:
            if os.environ.get("C3PO_DEBUG"):
                print(f"[c3po:debug] Exception registering {attempted_agent_id}: {type(e).__name__}: {e}", file=sys.stderr)
            break

    return None


def main() -> None:
    """Register with coordinator and output session context."""
    # Parse stdin to get session_id from Claude Code
    stdin_data = parse_hook_input()
    try:
        session_id = get_session_id(stdin_data)
    except ValueError:
        print("[c3po] Cannot register without session_id. Running in local mode.")
        sys.exit(0)

    try:
        # Register this session with the coordinator
        registration = register_with_coordinator(session_id)

        if registration:
            # The coordinator returns the assigned agent_id (may have collision suffix)
            assigned_id = registration.get("id", f"{MACHINE_NAME}/{PROJECT_NAME}")

            # Save assigned agent_id keyed by session_id for other hooks to read
            save_agent_id(session_id, assigned_id)

            # Get agent count from health endpoint (no auth required for health)
            req = urllib.request.Request(
                f"{COORDINATOR_URL}/api/health",
            )
            with urlopen_with_ssl(req, timeout=5) as resp:
                health = json.loads(resp.read())

            agents_online = health.get("agents_online", 0)

            # Output context for Claude
            print(f"[c3po] Connected to coordinator at {COORDINATOR_URL}")
            print(f"[c3po] Your agent ID: {assigned_id}")
            print(f"[c3po] {agents_online} agent(s) currently online")
            print(f"[c3po] Tip: call set_description to tell other agents what you can help with")
        else:
            print(f"[c3po] Could not register with coordinator at {COORDINATOR_URL}")
            print(f"[c3po] Running in local mode. Set C3PO_DEBUG=1 for more details.")

    except urllib.error.URLError as e:
        print(f"[c3po] Coordinator not available at {COORDINATOR_URL}")
        if os.environ.get("C3PO_DEBUG"):
            print(f"[c3po:debug] URLError: {e.reason}", file=sys.stderr)
        print(f"[c3po] Running in local mode.")
    except urllib.error.HTTPError as e:
        print(f"[c3po] Coordinator error ({e.code}) at {COORDINATOR_URL}")
        if os.environ.get("C3PO_DEBUG"):
            print(f"[c3po:debug] HTTPError: {e.read().decode()}", file=sys.stderr)
        print(f"[c3po] Running in local mode.")
    except json.JSONDecodeError as e:
        print(f"[c3po] Invalid coordinator response from {COORDINATOR_URL}")
        if os.environ.get("C3PO_DEBUG"):
            print(f"[c3po:debug] JSONDecodeError: {e}", file=sys.stderr)
        print(f"[c3po] Running in local mode.")
    except Exception as e:
        print(f"[c3po] Coordinator check failed for {COORDINATOR_URL}")
        if os.environ.get("C3PO_DEBUG"):
            print(f"[c3po:debug] Exception: {type(e).__name__}: {e}", file=sys.stderr)
        print(f"[c3po] Running in local mode.")

    # Always exit successfully - don't block session start
    sys.exit(0)


if __name__ == "__main__":
    main()
