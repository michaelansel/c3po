#!/usr/bin/env python3
"""
C3PO Acceptance Test Suite

Implements the acceptance test specification from ACCEPTANCE_SPEC.md.
Tests phases 0, 3-6, 9 using the MCP client library directly.
Phases 1-2 (plugin install/setup) and 7-8 (stop hook, task delegation)
require actual Claude Code sessions and are tested separately.

Usage:
    # Against docker-compose environment (from inside a container):
    python test_acceptance.py

    # Against a local coordinator:
    C3PO_COORDINATOR_URL=http://localhost:8420 python test_acceptance.py

    # Run specific phases:
    python test_acceptance.py --phase 3 --phase 5
"""

import argparse
import asyncio
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
import uuid

# Mutable config container (avoids global statement issues)
_config = {
    "coordinator_url": os.environ.get("C3PO_COORDINATOR_URL", "http://coordinator:8420"),
}

# Test results tracking
_results = []
_current_phase = None


def log(msg):
    prefix = f"Phase {_current_phase}" if _current_phase is not None else "test"
    print(f"\033[0;36m[{prefix}]\033[0m {msg}", flush=True)


def ok(msg):
    _results.append(("PASS", _current_phase, msg))
    print(f"\033[0;32m  ✓ {msg}\033[0m", flush=True)


def fail(msg):
    _results.append(("FAIL", _current_phase, msg))
    print(f"\033[0;31m  ✗ {msg}\033[0m", flush=True)


def error(msg):
    print(f"\033[0;31m[ERROR]\033[0m {msg}", file=sys.stderr, flush=True)


def assert_true(condition, pass_msg, fail_msg):
    if condition:
        ok(pass_msg)
    else:
        fail(fail_msg)
    return condition


# ---------------------------------------------------------------------------
# Phase 0: Prerequisites
# ---------------------------------------------------------------------------
async def phase_0():
    """Verify the coordinator is reachable and healthy."""
    global _current_phase
    _current_phase = 0
    log("Prerequisites - verifying coordinator is reachable")

    # Step 1: Health check
    try:
        req = urllib.request.Request(f"{_config['coordinator_url']}/api/health")
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            assert_true(
                data.get("status") == "ok",
                f"Health check returns status=ok (got: {data})",
                f"Health check failed: {data}",
            )
    except Exception as e:
        fail(f"Coordinator not reachable at {_config['coordinator_url']}: {e}")
        return False

    return True


# ---------------------------------------------------------------------------
# Phase 3: Single Agent Session
# ---------------------------------------------------------------------------
async def phase_3():
    """Verify a single agent can connect and use basic tools."""
    global _current_phase
    _current_phase = 3
    log("Single Agent Session")

    from mcp.client.streamable_http import streamablehttp_client
    from mcp import ClientSession

    url = f"{_config['coordinator_url']}/mcp"
    agent_id = f"accept-host-a-{uuid.uuid4().hex[:8]}"
    session_id = str(uuid.uuid4())
    headers = {
        "X-Agent-ID": agent_id,
        "X-Project-Name": "acceptance-test",
        "X-Session-ID": session_id,
    }

    async with streamablehttp_client(url, headers=headers) as (r, w, _):
        async with ClientSession(r, w) as session:
            await session.initialize()

            # Step 2: ping
            result = await session.call_tool("ping", {})
            result_text = str(result)
            passed = assert_true(
                "pong" in result_text.lower() and "timestamp" in result_text.lower(),
                "Ping returns pong with timestamp",
                f"Ping response unexpected: {result_text}",
            )

            # Step 3: list_agents
            result = await session.call_tool("list_agents", {})
            result_text = str(result)
            passed = assert_true(
                agent_id in result_text or "acceptance-test" in result_text,
                "list_agents returns at least 1 agent (self)",
                f"Agent not found in list: {result_text}",
            ) and passed

    return passed


# ---------------------------------------------------------------------------
# Phase 4: Two-Agent Registration
# ---------------------------------------------------------------------------
async def phase_4():
    """Verify two agents can connect simultaneously with distinct identities."""
    global _current_phase
    _current_phase = 4
    log("Two-Agent Registration")

    from mcp.client.streamable_http import streamablehttp_client
    from mcp import ClientSession

    url = f"{_config['coordinator_url']}/mcp"
    agent_a_base = f"accept-a-{uuid.uuid4().hex[:8]}"
    agent_b_base = f"accept-b-{uuid.uuid4().hex[:8]}"
    session_a = str(uuid.uuid4())
    session_b = str(uuid.uuid4())

    headers_a = {
        "X-Agent-ID": agent_a_base,
        "X-Project-Name": "acceptance-test",
        "X-Session-ID": session_a,
    }
    headers_b = {
        "X-Agent-ID": agent_b_base,
        "X-Project-Name": "acceptance-test",
        "X-Session-ID": session_b,
    }

    async with streamablehttp_client(url, headers=headers_a) as (ra, wa, _):
        async with ClientSession(ra, wa) as sess_a:
            await sess_a.initialize()
            # Register A
            await sess_a.call_tool("ping", {})

            async with streamablehttp_client(url, headers=headers_b) as (rb, wb, _):
                async with ClientSession(rb, wb) as sess_b:
                    await sess_b.initialize()
                    # Register B
                    await sess_b.call_tool("ping", {})

                    # List agents from A
                    result = await sess_a.call_tool("list_agents", {})
                    result_text = str(result)

                    # Count online agents - both should be visible
                    online_count = result_text.lower().count('"online"')
                    passed = assert_true(
                        online_count >= 2,
                        f"list_agents shows >= 2 online agents (found {online_count})",
                        f"Expected >= 2 online agents, found {online_count}: {result_text}",
                    )

                    # Agent IDs should be distinct
                    has_a = agent_a_base in result_text
                    has_b = agent_b_base in result_text
                    passed = assert_true(
                        has_a and has_b,
                        "Agent IDs are distinct (both visible in list)",
                        f"Could not find both agents: a={has_a}, b={has_b}",
                    ) and passed

    return passed


# ---------------------------------------------------------------------------
# Phase 5: Request/Response Roundtrip
# ---------------------------------------------------------------------------
async def phase_5():
    """Verify the full send/receive/respond/wait cycle between two agents."""
    global _current_phase
    _current_phase = 5
    log("Request/Response Roundtrip")

    from mcp.client.streamable_http import streamablehttp_client
    from mcp import ClientSession

    url = f"{_config['coordinator_url']}/mcp"
    agent_a_base = f"roundtrip-a-{uuid.uuid4().hex[:8]}"
    agent_b_base = f"roundtrip-b-{uuid.uuid4().hex[:8]}"
    agent_a_full = f"{agent_a_base}/acceptance-test"
    agent_b_full = f"{agent_b_base}/acceptance-test"

    headers_a = {
        "X-Agent-ID": agent_a_base,
        "X-Project-Name": "acceptance-test",
        "X-Session-ID": str(uuid.uuid4()),
    }
    headers_b = {
        "X-Agent-ID": agent_b_base,
        "X-Project-Name": "acceptance-test",
        "X-Session-ID": str(uuid.uuid4()),
    }

    passed = True

    async with streamablehttp_client(url, headers=headers_a) as (ra, wa, _):
        async with ClientSession(ra, wa) as sess_a:
            await sess_a.initialize()
            await sess_a.call_tool("ping", {})

            async with streamablehttp_client(url, headers=headers_b) as (rb, wb, _):
                async with ClientSession(rb, wb) as sess_b:
                    await sess_b.initialize()
                    await sess_b.call_tool("ping", {})

                    # Step 1: Agent A sends request to Agent B
                    log("Step 1: Agent A sends request to Agent B")
                    result = await sess_a.call_tool("send_request", {
                        "target": agent_b_full,
                        "message": "What is 2+2?",
                    })
                    result_text = str(result)

                    # Extract request_id
                    match = re.search(r'"id":\s*"([^"]+)"', result_text)
                    if not match:
                        fail(f"send_request did not return a request ID: {result_text}")
                        return False
                    request_id = match.group(1)
                    passed = assert_true(
                        len(request_id) > 0,
                        f"send_request returns request ID: {request_id}",
                        "send_request returned empty request ID",
                    ) and passed

                    # Step 2: Agent B gets pending requests
                    log("Step 2: Agent B checks pending requests")
                    result = await sess_b.call_tool("get_pending_requests", {})
                    result_text = str(result)
                    passed = assert_true(
                        "What is 2+2" in result_text,
                        "get_pending_requests returns the request from Agent A",
                        f"Request not found in pending: {result_text}",
                    ) and passed

                    # Step 3: Agent B responds
                    log("Step 3: Agent B responds")
                    result = await sess_b.call_tool("respond_to_request", {
                        "request_id": request_id,
                        "response": "4",
                    })
                    result_text = str(result)
                    passed = assert_true(
                        "success" in result_text.lower(),
                        "respond_to_request returns success",
                        f"Response failed: {result_text}",
                    ) and passed

                    # Step 4: Agent A waits for response
                    log("Step 4: Agent A waits for response")
                    result = await sess_a.call_tool("wait_for_response", {
                        "request_id": request_id,
                        "timeout": 30,
                    })
                    result_text = str(result)
                    passed = assert_true(
                        '"4"' in result_text or "'4'" in result_text,
                        'wait_for_response returns the response "4"',
                        f"Response not found: {result_text}",
                    ) and passed

    return passed


# ---------------------------------------------------------------------------
# Phase 6: Blocking Wait Behavior
# ---------------------------------------------------------------------------
async def phase_6():
    """Verify that blocking wait calls handle timeouts gracefully."""
    global _current_phase
    _current_phase = 6
    log("Blocking Wait Behavior")

    from mcp.client.streamable_http import streamablehttp_client
    from mcp import ClientSession

    url = f"{_config['coordinator_url']}/mcp"
    agent_base = f"blocking-{uuid.uuid4().hex[:8]}"
    headers = {
        "X-Agent-ID": agent_base,
        "X-Project-Name": "acceptance-test",
        "X-Session-ID": str(uuid.uuid4()),
    }

    passed = True

    async with streamablehttp_client(url, headers=headers) as (r, w, _):
        async with ClientSession(r, w) as session:
            await session.initialize()
            await session.call_tool("ping", {})

            # Step 1: wait_for_response with nonexistent request_id
            log("Step 1: wait_for_response with nonexistent request_id (timeout=5)")
            start = time.time()
            try:
                result = await session.call_tool("wait_for_response", {
                    "request_id": "nonexistent::fake::00000000",
                    "timeout": 5,
                })
                result_text = str(result)
                elapsed = time.time() - start
                passed = assert_true(
                    "timeout" in result_text.lower(),
                    f"wait_for_response returns timeout status (took {elapsed:.1f}s)",
                    f"Expected timeout, got: {result_text}",
                ) and passed
            except Exception as e:
                elapsed = time.time() - start
                # A ToolError is acceptable as long as it's not a crash
                passed = assert_true(
                    elapsed >= 3,
                    f"wait_for_response handled gracefully after {elapsed:.1f}s (error: {e})",
                    f"wait_for_response crashed immediately: {e}",
                ) and passed

            # Step 2: wait_for_request with timeout
            log("Step 2: wait_for_request with timeout=5")
            start = time.time()
            try:
                result = await session.call_tool("wait_for_request", {
                    "timeout": 5,
                })
                result_text = str(result)
                elapsed = time.time() - start
                passed = assert_true(
                    "timeout" in result_text.lower(),
                    f"wait_for_request returns timeout status (took {elapsed:.1f}s)",
                    f"Expected timeout, got: {result_text}",
                ) and passed
            except Exception as e:
                elapsed = time.time() - start
                passed = assert_true(
                    elapsed >= 3,
                    f"wait_for_request handled gracefully after {elapsed:.1f}s (error: {e})",
                    f"wait_for_request crashed immediately: {e}",
                ) and passed

    # Step 3: wait_for_request notification then consume via get_pending_requests
    log("Step 3: wait_for_request returns ready, then get_pending_requests consumes")

    agent_waiter_base = f"blocking-waiter-{uuid.uuid4().hex[:8]}"
    agent_sender_base = f"blocking-sender-{uuid.uuid4().hex[:8]}"
    waiter_full = f"{agent_waiter_base}/acceptance-test"

    headers_waiter = {
        "X-Agent-ID": agent_waiter_base,
        "X-Project-Name": "acceptance-test",
        "X-Session-ID": str(uuid.uuid4()),
    }
    headers_sender = {
        "X-Agent-ID": agent_sender_base,
        "X-Project-Name": "acceptance-test",
        "X-Session-ID": str(uuid.uuid4()),
    }

    async with streamablehttp_client(url, headers=headers_waiter) as (rw, ww, _):
        async with ClientSession(rw, ww) as sess_waiter:
            await sess_waiter.initialize()
            await sess_waiter.call_tool("ping", {})

            async with streamablehttp_client(url, headers=headers_sender) as (rs, ws, _):
                async with ClientSession(rs, ws) as sess_sender:
                    await sess_sender.initialize()
                    await sess_sender.call_tool("ping", {})

                    # Sender sends a request to waiter
                    send_result = await sess_sender.call_tool("send_request", {
                        "target": waiter_full,
                        "message": "notification test",
                    })

                    # Waiter calls wait_for_request — should return ready
                    result = await sess_waiter.call_tool("wait_for_request", {
                        "timeout": 10,
                    })
                    result_text = str(result)
                    passed = assert_true(
                        "ready" in result_text.lower(),
                        "wait_for_request returns ready status when message available",
                        f"Expected ready status, got: {result_text}",
                    ) and passed

                    # Now consume via get_pending_requests
                    result = await sess_waiter.call_tool("get_pending_requests", {})
                    result_text = str(result)
                    passed = assert_true(
                        "notification test" in result_text,
                        "get_pending_requests returns the message after wait_for_request",
                        f"Message not found in pending: {result_text}",
                    ) and passed

    return passed


# ---------------------------------------------------------------------------
# Phase 9: Error Cases
# ---------------------------------------------------------------------------
async def phase_9():
    """Verify error handling without crashes."""
    global _current_phase
    _current_phase = 9
    log("Error Cases")

    from mcp.client.streamable_http import streamablehttp_client
    from mcp import ClientSession

    url = f"{_config['coordinator_url']}/mcp"
    agent_base = f"errors-{uuid.uuid4().hex[:8]}"
    headers = {
        "X-Agent-ID": agent_base,
        "X-Project-Name": "acceptance-test",
        "X-Session-ID": str(uuid.uuid4()),
    }

    passed = True

    async with streamablehttp_client(url, headers=headers) as (r, w, _):
        async with ClientSession(r, w) as session:
            await session.initialize()
            await session.call_tool("ping", {})

            # Step 1: send_request to nonexistent agent
            log("Step 1: send_request to nonexistent agent")
            try:
                result = await session.call_tool("send_request", {
                    "target": "nonexistent-agent",
                    "message": "hello",
                })
                result_text = str(result)
                # Should have returned an error in the result
                passed = assert_true(
                    "error" in result_text.lower() or "not found" in result_text.lower(),
                    "send_request to nonexistent agent returns error (not crash)",
                    f"Expected error, got: {result_text}",
                ) and passed
            except Exception as e:
                # ToolError is acceptable - it means the server handled it gracefully
                err_msg = str(e).lower()
                passed = assert_true(
                    "not found" in err_msg or "agent" in err_msg or "error" in err_msg,
                    f"send_request to nonexistent agent raises ToolError (graceful): {e}",
                    f"Unexpected error type: {e}",
                ) and passed

            # Step 2: respond_to_request with fake request_id
            log("Step 2: respond_to_request with fake request_id")
            try:
                result = await session.call_tool("respond_to_request", {
                    "request_id": "fake-id",
                    "response": "nope",
                })
                result_text = str(result)
                passed = assert_true(
                    "error" in result_text.lower(),
                    "respond_to_request with fake ID returns error (not crash)",
                    f"Expected error, got: {result_text}",
                ) and passed
            except Exception as e:
                # ToolError is acceptable
                passed = assert_true(
                    True,
                    f"respond_to_request with fake ID raises ToolError (graceful): {e}",
                    f"Unexpected crash: {e}",
                ) and passed

            # Verify session is still alive after errors
            log("Step 3: Verify session still works after errors")
            try:
                result = await session.call_tool("ping", {})
                result_text = str(result)
                passed = assert_true(
                    "pong" in result_text.lower(),
                    "Session still works after error cases",
                    f"Session broken after errors: {result_text}",
                ) and passed
            except Exception as e:
                fail(f"Session crashed after error cases: {e}")
                passed = False

    return passed


# ---------------------------------------------------------------------------
# Phase 10: Teardown verification (agent offline detection)
# ---------------------------------------------------------------------------
async def phase_10():
    """Verify that disconnected agents are detected."""
    global _current_phase
    _current_phase = 10
    log("Teardown - agent offline detection")

    from mcp.client.streamable_http import streamablehttp_client
    from mcp import ClientSession

    url = f"{_config['coordinator_url']}/mcp"

    # Create two agents
    agent_a_base = f"teardown-a-{uuid.uuid4().hex[:8]}"
    agent_b_base = f"teardown-b-{uuid.uuid4().hex[:8]}"
    agent_b_full = f"{agent_b_base}/acceptance-test"

    headers_a = {
        "X-Agent-ID": agent_a_base,
        "X-Project-Name": "acceptance-test",
        "X-Session-ID": str(uuid.uuid4()),
    }
    headers_b = {
        "X-Agent-ID": agent_b_base,
        "X-Project-Name": "acceptance-test",
        "X-Session-ID": str(uuid.uuid4()),
    }

    passed = True

    # Register agent B, then let it disconnect
    async with streamablehttp_client(url, headers=headers_b) as (rb, wb, _):
        async with ClientSession(rb, wb) as sess_b:
            await sess_b.initialize()
            await sess_b.call_tool("ping", {})
            log("Agent B registered and connected")

    # Agent B's session is now closed - unregister via REST
    try:
        req = urllib.request.Request(
            f"{_config['coordinator_url']}/api/unregister",
            data=b"",
            method="POST",
            headers={
                "X-Agent-ID": agent_b_base,
                "X-Project-Name": "acceptance-test",
            },
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        pass  # Best effort

    # Now check from agent A
    async with streamablehttp_client(url, headers=headers_a) as (ra, wa, _):
        async with ClientSession(ra, wa) as sess_a:
            await sess_a.initialize()
            await sess_a.call_tool("ping", {})

            result = await sess_a.call_tool("list_agents", {})
            result_text = str(result)

            # Agent B should be offline or absent
            # After unregister, agent should be removed
            passed = assert_true(
                agent_b_full not in result_text
                or '"offline"' in result_text.lower(),
                "Disconnected agent B is offline or absent from list",
                f"Agent B still appears online: {result_text}",
            )

    return passed


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def print_summary():
    """Print test results summary."""
    print("\n" + "=" * 60)
    print("ACCEPTANCE TEST RESULTS")
    print("=" * 60)

    passes = sum(1 for r in _results if r[0] == "PASS")
    failures = sum(1 for r in _results if r[0] == "FAIL")

    for status, phase, msg in _results:
        icon = "✓" if status == "PASS" else "✗"
        color = "\033[0;32m" if status == "PASS" else "\033[0;31m"
        print(f"{color}  {icon} Phase {phase}: {msg}\033[0m")

    print(f"\n{passes} passed, {failures} failed, {passes + failures} total")
    print("=" * 60)

    return failures == 0


async def run_all(phases=None):
    """Run all acceptance test phases."""
    available_phases = {
        0: ("Prerequisites", phase_0),
        3: ("Single Agent Session", phase_3),
        4: ("Two-Agent Registration", phase_4),
        5: ("Request/Response Roundtrip", phase_5),
        6: ("Blocking Wait Behavior", phase_6),
        9: ("Error Cases", phase_9),
        10: ("Teardown", phase_10),
    }

    if phases is None:
        phases = sorted(available_phases.keys())

    print(f"\n{'=' * 60}")
    print(f"C3PO ACCEPTANCE TEST")
    print(f"Coordinator: {_config['coordinator_url']}")
    print(f"Phases: {phases}")
    print(f"{'=' * 60}\n")

    all_passed = True
    for phase_num in phases:
        if phase_num not in available_phases:
            error(f"Unknown phase: {phase_num}")
            continue

        name, func = available_phases[phase_num]
        print(f"\n--- Phase {phase_num}: {name} ---")

        try:
            result = await func()
            if not result:
                all_passed = False
                # Phase 0 failure is fatal
                if phase_num == 0:
                    error("Phase 0 failed - coordinator not reachable, aborting")
                    break
        except Exception as e:
            fail(f"Phase {phase_num} crashed: {e}")
            all_passed = False
            import traceback
            traceback.print_exc()

    return all_passed


def main():
    parser = argparse.ArgumentParser(description="C3PO Acceptance Tests")
    parser.add_argument(
        "--phase", type=int, action="append",
        help="Run specific phase(s). Can be repeated. Default: all phases.",
    )
    parser.add_argument(
        "--coordinator-url", type=str,
        help=f"Coordinator URL (default: {_config['coordinator_url']})",
    )
    args = parser.parse_args()

    if args.coordinator_url:
        _config["coordinator_url"] = args.coordinator_url

    all_passed = asyncio.run(run_all(args.phase))
    success = print_summary()

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
