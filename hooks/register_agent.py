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
- C3PO_AGENT_ID_FILE: If set, write the assigned agent ID to this path after successful registration
- C3PO_REGISTER_DEADLINE: Seconds the whole hook may spend on network calls (default: 7,
  under the 10s hooks.json timeout so the hook always prints a status line)
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
import urllib.error

from c3po_common import auth_headers, derive_project_name, get_coordinator_url, get_machine_name, get_session_id, parse_hook_input, save_agent_id, urlopen_with_ssl


# The whole hook must finish inside its hooks.json timeout (10s). Claude Code
# kills a hook that overruns, and a killed hook prints nothing, so the session
# gets no agent ID line and no hint why. Every request gets only the time left
# in this budget (measured from process start), leaving a margin for output.
REGISTER_DEADLINE_S = float(os.environ.get("C3PO_REGISTER_DEADLINE", "7"))
_START = time.monotonic()


def _remaining() -> float:
    """Seconds left in the hook's time budget (may be <= 0)."""
    return REGISTER_DEADLINE_S - (time.monotonic() - _START)


# Configuration
COORDINATOR_URL = get_coordinator_url()
MACHINE_NAME = get_machine_name()

# Project name: Gas Town naming (GT_* env vars), CLAUDE_PROJECT_NAME, or basename(cwd)
PROJECT_NAME = derive_project_name()


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
        remaining = _remaining()
        if remaining < 0.5:
            print(f"[c3po:debug] Out of time registering {attempted_agent_id}", file=sys.stderr)
            break
        try:
            with urlopen_with_ssl(req, timeout=remaining) as resp:
                result = json.loads(resp.read())
                if os.environ.get("C3PO_DEBUG"):
                    print(f"[c3po:debug] Registration successful: {result}", file=sys.stderr)
                return result
        except urllib.error.HTTPError as e:
            print(f"[c3po:debug] HTTPError registering {attempted_agent_id}: {e.code}", file=sys.stderr)
            if e.code == 429 and attempt < max_retries and _remaining() > retry_delay + 1:
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

            # Write to watcher-provided file if configured
            agent_id_out = os.environ.get("C3PO_AGENT_ID_FILE")
            if agent_id_out:
                try:
                    fd = os.open(agent_id_out, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
                    with os.fdopen(fd, "w") as f:
                        f.write(assigned_id)
                except OSError:
                    pass  # Best effort

            # Agent count from the health endpoint (no auth). Best effort: the
            # agent is registered by now, so a slow or failed health check must
            # not hide the agent ID line.
            agents_online = None
            remaining = _remaining()
            if remaining >= 0.5:
                try:
                    req = urllib.request.Request(f"{COORDINATOR_URL}/api/health")
                    with urlopen_with_ssl(req, timeout=min(5, remaining)) as resp:
                        agents_online = json.loads(resp.read()).get("agents_online", 0)
                except Exception as e:
                    if os.environ.get("C3PO_DEBUG"):
                        print(f"[c3po:debug] Health check failed: {type(e).__name__}: {e}", file=sys.stderr)

            # Output context for Claude
            print(f"[c3po] Connected to coordinator at {COORDINATOR_URL}")
            print(f"[c3po] Your agent ID: {assigned_id}")
            if agents_online is not None:
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
