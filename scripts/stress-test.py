#!/usr/bin/env python3
"""
C3PO Coordinator Stress Test

A standalone tool for load-testing any C3PO coordinator instance.
Reports latency percentiles (p50/p95/p99), throughput, and error rates.

Usage:
    # Against local dev (default)
    python3 scripts/stress-test.py

    # Against production (unauthenticated — only if auth is disabled)
    python3 scripts/stress-test.py --url https://mcp.qerk.be

    # Escalating load
    python3 scripts/stress-test.py --senders 10 --msgs-per-sender 20

    # Quick smoke test
    python3 scripts/stress-test.py --senders 2 --msgs-per-sender 5 --quick

    # With auth — auto-enroll using admin token (creates stress/* API key)
    python3 scripts/stress-test.py --url https://mcp.qerk.be --admin-token '<admin_token>'

    # With auth — pre-existing API key
    python3 scripts/stress-test.py --url https://mcp.qerk.be --token '<api_token>'

Phases:
    1. Warmup      — single ping to verify connectivity
    2. Baseline    — sequential single-sender latency (10 pings, 10 sends)
    3. Throughput  — concurrent senders, measures msgs/s and per-msg latency
    4. Wait latency — send→wait_for_message round-trip timing
    5. Load peek   — get_messages latency with full inbox
    6. Full pipeline — many senders + many listeners running the complete
                       wait→get→ack loop; measures delivered msgs/s
    7. Cleanup     — ack all messages to leave coordinator clean

Requires: pip install mcp httpx  (both already in the project venv)
"""

import argparse
import asyncio
import json
import statistics
import sys
import time
from contextlib import asynccontextmanager

import httpx

# ---------------------------------------------------------------------------
# MCP client setup
# ---------------------------------------------------------------------------

try:
    from mcp.client.streamable_http import streamablehttp_client
    from mcp import ClientSession
except ImportError:
    print("Error: MCP client library not available. Run from the project venv:")
    print("  source .venv/bin/activate && python3 scripts/stress-test.py")
    sys.exit(1)


@asynccontextmanager
async def mcp_session(url: str, agent_id: str, token: str | None = None):
    """Create an MCP client session."""
    # The coordinator reads X-Machine-Name and X-Project-Name, not X-Agent-ID.
    # Agent IDs are formatted as "machine/project".
    parts = agent_id.split("/", 1)
    headers = {"X-Machine-Name": parts[0]}
    if len(parts) > 1:
        headers["X-Project-Name"] = parts[1]
    if token:
        headers["Authorization"] = f"Bearer {token}"

    mcp_url = f"{url.rstrip('/')}/agent/mcp" if token else f"{url.rstrip('/')}/mcp"
    async with streamablehttp_client(mcp_url, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


def parse_result(result) -> dict | list | str:
    """Extract JSON from MCP CallToolResult."""
    for block in result.content:
        if hasattr(block, "text"):
            try:
                return json.loads(block.text)
            except json.JSONDecodeError:
                return block.text
    return str(result.content)


# ---------------------------------------------------------------------------
# Statistics helpers
# ---------------------------------------------------------------------------

def fmt_stats(latencies_ms: list[float]) -> str:
    """Format latency list into a stats summary string."""
    if not latencies_ms:
        return "(no data)"
    s = sorted(latencies_ms)
    n = len(s)
    return (
        f"n={n}  "
        f"avg={statistics.mean(s):.1f}ms  "
        f"p50={s[n // 2]:.1f}ms  "
        f"p95={s[int(n * 0.95)]:.1f}ms  "
        f"p99={s[int(n * 0.99)]:.1f}ms  "
        f"min={s[0]:.1f}ms  max={s[-1]:.1f}ms"
    )


# ---------------------------------------------------------------------------
# Enrollment
# ---------------------------------------------------------------------------

async def enroll_stress_key(url: str, admin_token: str) -> str:
    """Create a stress/* API key using the admin endpoint.

    Returns the composite API token to use for stress test sessions.
    """
    endpoint = f"{url.rstrip('/')}/admin/api/keys"
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            endpoint,
            headers={"Authorization": f"Bearer {admin_token}"},
            json={
                "agent_pattern": "stress/*",
                "description": "Stress test (auto-enrolled)",
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return data["api_key"]


# ---------------------------------------------------------------------------
# Test phases
# ---------------------------------------------------------------------------

async def phase_warmup(url: str, token: str | None):
    """Phase 0: Verify connectivity."""
    print("\n--- Phase 0: Warmup ---")
    try:
        async with mcp_session(url, "stress/warmup", token) as s:
            await s.call_tool("ping", {})
        print("  Coordinator reachable.")
        return True
    except Exception as e:
        print(f"  FAILED to connect: {e}")
        return False


async def phase_baseline(url: str, token: str | None, n: int = 10):
    """Phase 1: Sequential single-sender baseline."""
    print(f"\n--- Phase 1: Baseline (sequential, n={n}) ---")

    async with mcp_session(url, "stress/baseline", token) as s:
        await s.call_tool("register_agent", {"name": "stress/baseline"})

        # Ping latency
        ping_latencies = []
        for _ in range(n):
            t0 = time.perf_counter()
            await s.call_tool("ping", {})
            ping_latencies.append((time.perf_counter() - t0) * 1000)
        print(f"  ping:         {fmt_stats(ping_latencies)}")

        # send_message latency
        send_latencies = []
        for i in range(n):
            t0 = time.perf_counter()
            await s.call_tool("send_message", {
                "to": "stress/sink",
                "message": f"baseline-{i}",
            })
            send_latencies.append((time.perf_counter() - t0) * 1000)
        print(f"  send_message: {fmt_stats(send_latencies)}")

    return {"ping": ping_latencies, "send": send_latencies}


async def phase_throughput(
    url: str, token: str | None,
    senders: int = 5, msgs_per_sender: int = 10,
):
    """Phase 2: Concurrent senders to a single target."""
    total = senders * msgs_per_sender
    print(f"\n--- Phase 2: Throughput ({senders} senders × {msgs_per_sender} msgs = {total}) ---")

    target = "stress/throughput-target"
    errors = []

    async def sender_work(sender_idx: int) -> list[float]:
        agent_id = f"stress/sender-{sender_idx}"
        latencies = []
        try:
            async with mcp_session(url, agent_id, token) as s:
                await s.call_tool("register_agent", {"name": agent_id})
                for i in range(msgs_per_sender):
                    t0 = time.perf_counter()
                    await s.call_tool("send_message", {
                        "to": target,
                        "message": f"from-{sender_idx}-{i}",
                    })
                    latencies.append((time.perf_counter() - t0) * 1000)
        except Exception as e:
            errors.append(f"sender-{sender_idx}: {e}")
        return latencies

    wall_start = time.perf_counter()
    tasks = [asyncio.create_task(sender_work(i)) for i in range(senders)]
    all_latencies = []
    for task in tasks:
        all_latencies.extend(await task)
    wall_ms = (time.perf_counter() - wall_start) * 1000

    if errors:
        print(f"  ERRORS ({len(errors)}):")
        for e in errors[:5]:
            print(f"    {e}")

    throughput = len(all_latencies) / (wall_ms / 1000) if wall_ms > 0 else 0
    print(f"  per-msg:    {fmt_stats(all_latencies)}")
    print(f"  wall time:  {wall_ms:.0f}ms")
    print(f"  throughput: {throughput:.1f} msgs/s")
    print(f"  errors:     {len(errors)}")

    return {
        "latencies": all_latencies,
        "wall_ms": wall_ms,
        "throughput": throughput,
        "errors": len(errors),
    }


async def phase_wait_latency(url: str, token: str | None, rounds: int = 5):
    """Phase 3: send → wait_for_message round-trip."""
    print(f"\n--- Phase 3: Wait latency (send→receive, {rounds} rounds) ---")

    latencies = []
    errors = []

    for i in range(rounds):
        try:
            async with mcp_session(url, "stress/waiter", token) as receiver:
                await receiver.call_tool("register_agent", {"name": "stress/waiter"})

                wait_task = asyncio.create_task(
                    receiver.call_tool("wait_for_message", {"timeout": 15})
                )
                await asyncio.sleep(0.3)

                # Send from a concurrent session (nested inside receiver's scope)
                async with mcp_session(url, f"stress/pinger-{i}", token) as sender:
                    await sender.call_tool("register_agent", {
                        "name": f"stress/pinger-{i}",
                    })
                    t0 = time.perf_counter()
                    await sender.call_tool("send_message", {
                        "to": "stress/waiter",
                        "message": f"wait-test-{i}",
                    })

                result = await asyncio.wait_for(wait_task, timeout=15)
                latency = (time.perf_counter() - t0) * 1000
                latencies.append(latency)

                # Ack to clean up
                parsed = parse_result(result)
                if isinstance(parsed, dict) and parsed.get("status") == "received":
                    msg_ids = [m["id"] for m in parsed.get("messages", [])
                               if "id" in m]
                    if msg_ids:
                        await receiver.call_tool("ack_messages", {"message_ids": msg_ids})

        except Exception as e:
            errors.append(f"round {i}: {e}")

    if errors:
        print(f"  ERRORS ({len(errors)}):")
        for e in errors[:5]:
            print(f"    {e}")

    print(f"  send→receive: {fmt_stats(latencies)}")
    return {"latencies": latencies, "errors": len(errors)}


async def phase_peek_under_load(url: str, token: str | None, inbox_size: int = 50):
    """Phase 4: get_messages latency with a full inbox."""
    print(f"\n--- Phase 4: Peek under load ({inbox_size} messages in inbox) ---")

    target = "stress/peek-target"

    # Fill inbox
    async with mcp_session(url, "stress/filler", token) as filler:
        await filler.call_tool("register_agent", {"name": "stress/filler"})
        for i in range(inbox_size):
            await filler.call_tool("send_message", {
                "to": target,
                "message": f"load-{i}",
            })

    # Time get_messages
    async with mcp_session(url, target, token) as session:
        await session.call_tool("register_agent", {"name": target})
        latencies = []
        for _ in range(10):
            t0 = time.perf_counter()
            await session.call_tool("get_messages", {})
            latencies.append((time.perf_counter() - t0) * 1000)

    print(f"  get_messages: {fmt_stats(latencies)}")
    return {"latencies": latencies}


async def phase_full_pipeline(
    url: str, token: str | None,
    senders: int = 5, listeners: int = 3, msgs_per_sender: int = 10,
):
    """Phase 5: Many senders + many listeners running the full receive loop.

    Each listener runs: wait_for_message → get_messages → ack_messages → repeat.
    Senders distribute messages round-robin across listeners.
    Measures delivered msgs/s throughput and per-cycle latency.
    """
    total = senders * msgs_per_sender
    print(f"\n--- Phase 5: Full pipeline ({senders} senders × {msgs_per_sender} msgs "
          f"→ {listeners} listeners) ---")

    # Coordination signals
    listeners_ready = asyncio.Event()
    senders_done = asyncio.Event()
    ready_count = 0
    ready_lock = asyncio.Lock()

    errors = []
    delivered = []  # (listener_idx, cycle_latency_ms) for each delivered message
    cycle_latencies = []  # per wait→get→ack cycle time

    async def listener_work(listener_idx: int):
        nonlocal ready_count
        agent_id = f"stress/listener-{listener_idx}"
        try:
            async with mcp_session(url, agent_id, token) as s:
                await s.call_tool("register_agent", {"name": agent_id})

                # Signal ready
                async with ready_lock:
                    ready_count += 1
                    if ready_count >= listeners:
                        listeners_ready.set()

                # Run the full receive loop until senders are done and inbox is empty
                while True:
                    # wait_for_message (short timeout so we can check senders_done)
                    t0 = time.perf_counter()
                    result = await s.call_tool("wait_for_message", {"timeout": 5})
                    parsed = parse_result(result)

                    if isinstance(parsed, dict) and parsed.get("status") == "timeout":
                        # No message arrived — check if senders are done
                        if senders_done.is_set():
                            # Poll until inbox is empty for 2 consecutive checks
                            empty_checks = 0
                            while empty_checks < 2:
                                result = await s.call_tool("get_messages", {})
                                parsed = parse_result(result)
                                if isinstance(parsed, list) and parsed:
                                    msg_ids = []
                                    for m in parsed:
                                        mid = m.get("id") or m.get("reply_id")
                                        if mid:
                                            msg_ids.append(mid)
                                    if msg_ids:
                                        await s.call_tool("ack_messages", {"message_ids": msg_ids})
                                        cycle_ms = (time.perf_counter() - t0) * 1000
                                        for _ in msg_ids:
                                            delivered.append((listener_idx, cycle_ms / len(msg_ids)))
                                            cycle_latencies.append(cycle_ms / len(msg_ids))
                                    empty_checks = 0
                                else:
                                    empty_checks += 1
                                    await asyncio.sleep(0.5)
                            break
                        continue

                    # wait returned messages — now get_messages to see full inbox
                    result = await s.call_tool("get_messages", {})
                    parsed = parse_result(result)

                    if isinstance(parsed, list) and parsed:
                        msg_ids = []
                        for m in parsed:
                            mid = m.get("id") or m.get("reply_id")
                            if mid:
                                msg_ids.append(mid)
                        if msg_ids:
                            await s.call_tool("ack_messages", {"message_ids": msg_ids})
                            cycle_ms = (time.perf_counter() - t0) * 1000
                            for _ in msg_ids:
                                delivered.append((listener_idx, cycle_ms / len(msg_ids)))
                                cycle_latencies.append(cycle_ms / len(msg_ids))

        except BaseException as e:
            # Unwrap ExceptionGroups to get the actual sub-exception
            detail = f"{type(e).__name__}: {e}"
            if isinstance(e, BaseExceptionGroup):
                for sub in e.exceptions:
                    detail += f"\n    sub: {type(sub).__name__}: {sub}"
            errors.append(f"listener-{listener_idx}: {detail}")

    async def sender_work(sender_idx: int) -> list[float]:
        agent_id = f"stress/pipeline-sender-{sender_idx}"
        latencies = []
        try:
            async with mcp_session(url, agent_id, token) as s:
                await s.call_tool("register_agent", {"name": agent_id})
                for i in range(msgs_per_sender):
                    target = f"stress/listener-{(sender_idx * msgs_per_sender + i) % listeners}"
                    t0 = time.perf_counter()
                    await s.call_tool("send_message", {
                        "to": target,
                        "message": f"pipeline-{sender_idx}-{i}",
                    })
                    latencies.append((time.perf_counter() - t0) * 1000)
        except BaseException as e:
            detail = f"{type(e).__name__}: {e}"
            if isinstance(e, BaseExceptionGroup):
                for sub in e.exceptions:
                    detail += f"\n    sub: {type(sub).__name__}: {sub}"
            errors.append(f"pipeline-sender-{sender_idx}: {detail}")
        return latencies

    # Start listeners
    wall_start = time.perf_counter()
    listener_tasks = [asyncio.create_task(listener_work(i)) for i in range(listeners)]

    # Wait for all listeners to register
    await asyncio.wait_for(listeners_ready.wait(), timeout=30)

    # Start senders
    sender_tasks = [asyncio.create_task(sender_work(i)) for i in range(senders)]
    send_latencies = []
    for task in sender_tasks:
        send_latencies.extend(await task)

    # Signal senders are done
    senders_done.set()

    # Wait for listeners to drain
    await asyncio.gather(*listener_tasks, return_exceptions=True)
    wall_ms = (time.perf_counter() - wall_start) * 1000

    if errors:
        print(f"  ERRORS ({len(errors)}):")
        for e in errors[:5]:
            print(f"    {e}")

    delivered_count = len(delivered)
    throughput = delivered_count / (wall_ms / 1000) if wall_ms > 0 else 0
    loss = total - delivered_count

    print(f"  sent:         {total} messages")
    print(f"  delivered:    {delivered_count} messages ({loss} lost)")
    print(f"  send latency: {fmt_stats(send_latencies)}")
    print(f"  cycle (wait→get→ack): {fmt_stats(cycle_latencies)}")
    print(f"  wall time:    {wall_ms:.0f}ms")
    print(f"  throughput:   {throughput:.1f} delivered msgs/s")
    print(f"  errors:       {len(errors)}")

    return {
        "sent": total,
        "delivered": delivered_count,
        "loss": loss,
        "send_latencies": send_latencies,
        "cycle_latencies": cycle_latencies,
        "wall_ms": wall_ms,
        "throughput": throughput,
        "errors": len(errors),
    }


async def phase_high_client_count(
    url: str, token: str | None,
    num_clients: int = 50,
    msgs_per_client: int = 2,
    setup_batch: int = 3,
    send_interval: float = 0.15,
):
    """High client count test with staggered startup and messaging.

    All clients are both senders and receivers. Sessions are established in
    small batches to respect per-IP rate limits. Once all clients are ready,
    messages are sent one-at-a-time with controlled pacing. Idle clients sit
    in wait_for_message (single long-poll = ~0 rate limit cost).

    Args:
        num_clients: Total MCP sessions
        msgs_per_client: Messages each client sends
        setup_batch: Sessions to start per batch (with 1s pause between)
        send_interval: Seconds between consecutive sends (across all clients)
    """
    total_msgs = num_clients * msgs_per_client
    print(f"\n--- High Client Count ({num_clients} clients × "
          f"{msgs_per_client} msgs each = {total_msgs} messages) ---")
    print(f"  Setup: {setup_batch} sessions/batch, "
          f"send interval: {send_interval:.2f}s")

    # Shared coordination
    all_ready = asyncio.Event()
    ready_count = 0
    ready_lock = asyncio.Lock()

    # Send queue: client tasks pull from this to stagger sends globally
    send_queue = asyncio.Queue()

    # Results
    errors = []
    send_latencies = []
    receive_latencies = []
    delivered_count = 0
    delivered_lock = asyncio.Lock()

    async def client_work(idx: int):
        nonlocal ready_count, delivered_count
        agent_id = f"stress/client-{idx}"

        try:
            async with mcp_session(url, agent_id, token) as s:
                await s.call_tool("register_agent", {"name": agent_id})

                # Signal ready
                async with ready_lock:
                    ready_count += 1
                    if ready_count >= num_clients:
                        all_ready.set()

                # Wait for all clients
                await all_ready.wait()

                # === SEND PHASE ===
                # Wait for our turn(s) from the send queue
                sends_remaining = msgs_per_client
                while sends_remaining > 0:
                    send_token = await send_queue.get()
                    if send_token is None:
                        break  # Poison pill
                    target_idx = (idx + sends_remaining) % num_clients
                    if target_idx == idx:
                        target_idx = (idx + 1) % num_clients
                    target = f"stress/client-{target_idx}"
                    t0 = time.perf_counter()
                    await s.call_tool("send_message", {
                        "to": target,
                        "message": f"from-{idx}-to-{target_idx}",
                    })
                    send_latencies.append((time.perf_counter() - t0) * 1000)
                    sends_remaining -= 1

                # === RECEIVE PHASE ===
                # Each client expects to receive msgs_per_client messages
                received = 0
                deadline = time.perf_counter() + 60
                while received < msgs_per_client and time.perf_counter() < deadline:
                    t0 = time.perf_counter()
                    result = await s.call_tool("wait_for_message", {"timeout": 10})
                    parsed = parse_result(result)

                    if isinstance(parsed, dict) and parsed.get("status") == "timeout":
                        continue

                    # Get and ack
                    result = await s.call_tool("get_messages", {})
                    parsed = parse_result(result)
                    if isinstance(parsed, list) and parsed:
                        msg_ids = []
                        for m in parsed:
                            mid = m.get("id") or m.get("reply_id")
                            if mid:
                                msg_ids.append(mid)
                        if msg_ids:
                            await s.call_tool("ack_messages", {"message_ids": msg_ids})
                            cycle_ms = (time.perf_counter() - t0) * 1000
                            received += len(msg_ids)
                            for _ in msg_ids:
                                receive_latencies.append(cycle_ms / len(msg_ids))
                            async with delivered_lock:
                                delivered_count += len(msg_ids)

        except BaseException as e:
            detail = f"{type(e).__name__}: {e}"
            if isinstance(e, BaseExceptionGroup):
                for sub in e.exceptions:
                    detail += f"\n    sub: {type(sub).__name__}: {sub}"
            errors.append(f"client-{idx}: {detail}")

    # Launch clients in staggered batches
    wall_start = time.perf_counter()
    tasks = []
    for batch_start in range(0, num_clients, setup_batch):
        batch_end = min(batch_start + setup_batch, num_clients)
        for idx in range(batch_start, batch_end):
            tasks.append(asyncio.create_task(client_work(idx)))
        setup_pct = batch_end * 100 // num_clients
        print(f"  Setup: {batch_end}/{num_clients} sessions ({setup_pct}%)...",
              end="\r", flush=True)
        if batch_end < num_clients:
            await asyncio.sleep(1.0)

    # Wait for all to register
    print(f"  Setup: {num_clients}/{num_clients} sessions (100%)    ")
    try:
        await asyncio.wait_for(all_ready.wait(), timeout=120)
    except asyncio.TimeoutError:
        print(f"  TIMEOUT waiting for all clients to register "
              f"({ready_count}/{num_clients} ready)")
        return {"errors": 1}
    setup_ms = (time.perf_counter() - wall_start) * 1000
    print(f"  All {num_clients} clients registered in {setup_ms:.0f}ms")

    # Feed the send queue: round-robin through clients, one send at a time
    # Each client gets msgs_per_client turns
    send_start = time.perf_counter()
    for round_num in range(msgs_per_client):
        for client_idx in range(num_clients):
            await send_queue.put(True)  # Signal one client to send
            await asyncio.sleep(send_interval)
    send_phase_ms = (time.perf_counter() - send_start) * 1000
    print(f"  Send phase complete: {total_msgs} messages in {send_phase_ms:.0f}ms "
          f"({total_msgs / (send_phase_ms / 1000):.1f} msgs/s)")

    # Wait for all clients to finish receiving
    await asyncio.gather(*tasks, return_exceptions=True)
    wall_ms = (time.perf_counter() - wall_start) * 1000

    # Results
    if errors:
        print(f"  ERRORS ({len(errors)}):")
        for e in errors[:10]:
            print(f"    {e}")

    loss = total_msgs - delivered_count
    throughput = delivered_count / (wall_ms / 1000) if wall_ms > 0 else 0

    print(f"  clients:        {num_clients}")
    print(f"  sent:           {total_msgs} messages")
    print(f"  delivered:      {delivered_count} messages ({loss} lost)")
    print(f"  send latency:   {fmt_stats(send_latencies)}")
    print(f"  receive cycle:  {fmt_stats(receive_latencies)}")
    print(f"  wall time:      {wall_ms:.0f}ms")
    print(f"  throughput:     {throughput:.1f} delivered msgs/s")
    print(f"  errors:         {len(errors)}")

    return {
        "clients": num_clients,
        "sent": total_msgs,
        "delivered": delivered_count,
        "loss": loss,
        "send_latencies": send_latencies,
        "receive_latencies": receive_latencies,
        "wall_ms": wall_ms,
        "throughput": throughput,
        "errors": len(errors),
    }


async def phase_cleanup(url: str, token: str | None, num_clients: int = 0):
    """Clean up stress test artifacts."""
    print("\n--- Cleanup ---")
    agents_to_clean = [
        "stress/baseline", "stress/sink", "stress/throughput-target",
        "stress/waiter", "stress/peek-target", "stress/filler",
        "stress/warmup",
    ]
    # Also clean sender/listener/client agents
    for i in range(max(20, num_clients)):
        agents_to_clean.append(f"stress/sender-{i}")
        agents_to_clean.append(f"stress/pinger-{i}")
        agents_to_clean.append(f"stress/listener-{i}")
        agents_to_clean.append(f"stress/pipeline-sender-{i}")
        agents_to_clean.append(f"stress/client-{i}")

    cleaned = 0
    for agent in agents_to_clean:
        try:
            async with mcp_session(url, agent, token) as s:
                await s.call_tool("register_agent", {"name": agent})
                result = await s.call_tool("get_messages", {})
                parsed = parse_result(result)
                if isinstance(parsed, list) and parsed:
                    msg_ids = []
                    for m in parsed:
                        mid = m.get("id") or m.get("reply_id")
                        if mid:
                            msg_ids.append(mid)
                    if msg_ids:
                        await s.call_tool("ack_messages", {"message_ids": msg_ids})
                        cleaned += len(msg_ids)
        except Exception:
            pass

    print(f"  Acked {cleaned} leftover messages.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def run(args):
    print(f"C3PO Stress Test")
    print(f"Target: {args.url}")

    # Auto-enroll if admin token provided
    if args.admin_token:
        if args.token:
            print("Warning: --admin-token and --token both set; ignoring --token")
        print("Enrolling stress/* API key via admin endpoint...")
        try:
            args.token = await enroll_stress_key(args.url, args.admin_token)
            print(f"  Enrolled successfully.")
        except Exception as e:
            print(f"  FAILED to enroll: {e}")
            return 1

    if not await phase_warmup(args.url, args.token):
        print("\nAborted: coordinator not reachable.")
        return 1

    # High client count mode: skip standard phases
    if args.high_clients > 0:
        print(f"Mode: high client count ({args.high_clients} clients)")
        result = await phase_high_client_count(
            args.url, args.token,
            num_clients=args.high_clients,
            msgs_per_client=args.msgs_per_client,
            send_interval=args.send_interval,
        )
        await phase_cleanup(args.url, args.token, num_clients=args.high_clients)

        print("\n" + "=" * 60)
        print("SUMMARY")
        print("=" * 60)
        print(f"  Clients:        {result['clients']}")
        print(f"  Sent:           {result['sent']}")
        print(f"  Delivered:      {result['delivered']}/{result['sent']} "
              f"({result['loss']} lost)")
        print(f"  Send latency:   {fmt_stats(result['send_latencies'])}")
        print(f"  Receive cycle:  {fmt_stats(result['receive_latencies'])}")
        print(f"  Wall time:      {result['wall_ms']:.0f}ms")
        print(f"  Throughput:     {result['throughput']:.1f} delivered msgs/s")

        if result["errors"]:
            print(f"\n  Total errors: {result['errors']}")
            return 1
        print("\nAll phases completed successfully.")
        return 0

    # Standard mode
    print(f"Config: {args.senders} senders × {args.msgs_per_sender} msgs/sender")

    results = {}
    results["baseline"] = await phase_baseline(args.url, args.token)

    if not args.quick:
        results["throughput"] = await phase_throughput(
            args.url, args.token,
            senders=args.senders,
            msgs_per_sender=args.msgs_per_sender,
        )
        results["wait"] = await phase_wait_latency(
            args.url, args.token,
            rounds=args.wait_rounds,
        )
        results["peek"] = await phase_peek_under_load(
            args.url, args.token,
            inbox_size=args.inbox_size,
        )
        results["pipeline"] = await phase_full_pipeline(
            args.url, args.token,
            senders=args.senders,
            listeners=args.listeners,
            msgs_per_sender=args.msgs_per_sender,
        )

    await phase_cleanup(args.url, args.token)

    # Summary
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)

    baseline = results.get("baseline", {})
    if baseline.get("ping"):
        print(f"  Ping:           {fmt_stats(baseline['ping'])}")
    if baseline.get("send"):
        print(f"  Send (seq):     {fmt_stats(baseline['send'])}")

    tp = results.get("throughput")
    if tp:
        print(f"  Send (conc):    {fmt_stats(tp['latencies'])}")
        print(f"  Throughput:     {tp['throughput']:.1f} msgs/s")

    wait = results.get("wait")
    if wait and wait.get("latencies"):
        print(f"  Send→receive:   {fmt_stats(wait['latencies'])}")

    peek = results.get("peek")
    if peek and peek.get("latencies"):
        print(f"  get_messages:   {fmt_stats(peek['latencies'])}")

    pipeline = results.get("pipeline")
    if pipeline:
        print(f"  Pipeline send:  {fmt_stats(pipeline['send_latencies'])}")
        print(f"  Pipeline cycle: {fmt_stats(pipeline['cycle_latencies'])}")
        print(f"  Pipeline thru:  {pipeline['throughput']:.1f} delivered msgs/s")
        print(f"  Pipeline loss:  {pipeline['loss']}/{pipeline['sent']}")

    total_errors = sum(
        r.get("errors", 0) for r in results.values() if isinstance(r, dict)
    )
    if total_errors:
        print(f"\n  Total errors: {total_errors}")
        return 1

    print("\nAll phases completed successfully.")
    return 0


def main():
    parser = argparse.ArgumentParser(
        description="C3PO Coordinator Stress Test",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--url", default="http://localhost:8420",
        help="Coordinator URL (default: http://localhost:8420)",
    )
    parser.add_argument(
        "--token", default=None,
        help="API token for authenticated endpoints (Bearer token value)",
    )
    parser.add_argument(
        "--admin-token", default=None,
        help="Admin token to auto-enroll a stress/* API key (used instead of --token)",
    )
    parser.add_argument(
        "--senders", type=int, default=5,
        help="Number of concurrent senders (default: 5)",
    )
    parser.add_argument(
        "--msgs-per-sender", type=int, default=10,
        help="Messages per sender (default: 10)",
    )
    parser.add_argument(
        "--listeners", type=int, default=3,
        help="Number of concurrent listeners for full pipeline (default: 3)",
    )
    parser.add_argument(
        "--wait-rounds", type=int, default=5,
        help="Number of send→receive round-trips (default: 5)",
    )
    parser.add_argument(
        "--inbox-size", type=int, default=50,
        help="Messages to load into inbox for peek test (default: 50)",
    )
    parser.add_argument(
        "--quick", action="store_true",
        help="Quick mode: only run baseline (skip throughput/wait/peek)",
    )
    parser.add_argument(
        "--high-clients", type=int, default=0, metavar="N",
        help="Run high client count test with N clients (e.g., 50). "
             "Skips standard phases; runs staggered setup + messaging test.",
    )
    parser.add_argument(
        "--msgs-per-client", type=int, default=2,
        help="Messages per client in --high-clients mode (default: 2)",
    )
    parser.add_argument(
        "--send-interval", type=float, default=0.15,
        help="Seconds between sends in --high-clients mode (default: 0.15)",
    )

    args = parser.parse_args()
    exit_code = asyncio.run(run(args))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
