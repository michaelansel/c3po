/**
 * C-3PO for pi: lets a pi session take part in the C-3PO agent mesh.
 *
 *  - registers with the coordinator at session start, unregisters (keeping the mailbox) at shutdown
 *  - long-polls for messages and injects each as a user turn (follow-up if pi is busy)
 *  - acks a message only after the turn that handled it has settled
 *  - tools: c3po_reply, c3po_send, c3po_agents
 *  - optional status file (idle/busy) for the session launcher
 *
 * Configuration (env):
 *   C3PO_COORDINATOR_URL / C3PO_API_TOKEN   else ~/.claude/c3po-credentials.json (written by setup.py --enroll)
 *   C3PO_AGENT_ID                           full machine/project; else C3PO_MACHINE_NAME (or hostname) + C3PO_PROJECT_NAME (or cwd name)
 *   C3PO_MCP_PATH                           default /agent/mcp (use /mcp against a bare local coordinator)
 *   C3PO_ALLOW_FROM                         comma-separated sender globs; default accept all
 *   C3PO_STATUS_FILE                        write {state: idle|busy|...} JSON here
 *   C3PO_DISABLE=1                          load but do nothing
 */
import os from "node:os";
import path from "node:path";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import { C3poClient, loadConfig, sanitizeName, type Message } from "../src/client.ts";
import { InboxPump, formatMessage, messageKey } from "../src/inbox.ts";
import { writeStatus, type State } from "../src/status.ts";

const HEARTBEAT_MS = 5 * 60_000;

function resolveAgentId(cwd: string, env = process.env): string {
  if (env.C3PO_AGENT_ID) return env.C3PO_AGENT_ID;
  const machine = sanitizeName(env.C3PO_MACHINE_NAME || os.hostname().split(".")[0]);
  const project = sanitizeName(env.C3PO_PROJECT_NAME || env.CLAUDE_PROJECT_NAME || path.basename(cwd));
  return `${machine}/${project}`;
}

export default function (pi: ExtensionAPI) {
  if (process.env.C3PO_DISABLE === "1") return;

  let client: C3poClient | null = null;
  let pump: InboxPump | null = null;
  let heartbeat: ReturnType<typeof setInterval> | null = null;
  let ctxNow: any = null; // latest context; the one captured at session_start can go stale across session switches
  let state: State = "starting";
  let stateSince = new Date().toISOString();
  let fatal: string | undefined;
  /** Delivered while idle: a run has not started for them yet. */
  let awaitingRun = new Set<string>();
  /** Part of the current run: ack when it settles. */
  let inRun = new Set<string>();

  const statusFile = process.env.C3PO_STATUS_FILE;
  const log = (m: string) => {
    if (process.env.C3PO_DEBUG) console.error(`[c3po-pi] ${m}`);
  };

  function publish(next?: State) {
    if (next && next !== state) {
      state = next;
      stateSince = new Date().toISOString();
    }
    if (!client) return;
    writeStatus(statusFile, {
      agent_id: client.agentId,
      session_id: client.sessionId,
      state,
      pending: awaitingRun.size + inRun.size,
      dropped: pump?.dropped ?? 0,
      since: stateSince,
      updated: new Date().toISOString(),
      ...(fatal ? { error: fatal } : {}),
    });
  }

  function deliver(messages: Message[]) {
    const idle = ctxNow?.isIdle?.() ?? true;
    for (const m of messages) {
      const key = messageKey(m);
      if (idle) {
        awaitingRun.add(key);
        pi.sendUserMessage(formatMessage(m));
      } else {
        inRun.add(key); // joins the run in progress, so it settles with it
        pi.sendUserMessage(formatMessage(m), { deliverAs: "followUp" });
      }
    }
    publish();
  }

  async function ackSettled() {
    if (!client || !pump || inRun.size === 0) return;
    const ids = [...inRun];
    try {
      await client.ack(ids);
      inRun = new Set([...inRun].filter((i) => !ids.includes(i)));
      pump.markAcked(ids);
    } catch (e) {
      // Leave them in inRun: the next settle retries. The pump won't re-deliver them (they are still "delivered").
      log(`ack failed: ${(e as Error).message}`);
    }
  }

  pi.on("session_start", async (_e, ctx) => {
    ctxNow = ctx;
    const cfg = loadConfig();
    const sessionId = ctx.sessionManager.getSessionId();
    client = new C3poClient(cfg, resolveAgentId(ctx.cwd), sessionId);
    try {
      const reg = await client.register();
      log(`registered as ${reg.id}`);
    } catch (e) {
      fatal = `register failed: ${(e as Error).message}`;
      state = "stopped";
      publish();
      ctx.ui?.notify?.(`C-3PO: ${fatal}`, "warning");
      return;
    }
    pump = new InboxPump({
      client,
      deliver,
      allowFrom: (process.env.C3PO_ALLOW_FROM || "").split(",").map((s) => s.trim()).filter(Boolean),
      log,
      onFatal: (err) => {
        fatal = err.message;
        publish("stopped");
        ctxNow?.ui?.notify?.(`C-3PO: ${err.message}`, "warning");
      },
      onDropped: (m) => log(`dropped message from ${m.from_agent} (not in C3PO_ALLOW_FROM)`),
    });
    pump.start();
    // REST waiting leaves the agent showing as "watching"; a periodic MCP call keeps it "online".
    heartbeat = setInterval(() => client?.ping().catch(() => undefined), HEARTBEAT_MS);
    heartbeat.unref?.();
    publish(ctx.isIdle() ? "idle" : "busy");
    ctx.ui?.notify?.(`C-3PO: connected as ${client.agentId}`, "info");
  });

  pi.on("agent_start", async (_e, ctx) => {
    ctxNow = ctx;
    for (const id of awaitingRun) inRun.add(id);
    awaitingRun = new Set();
    publish("busy");
  });

  pi.on("agent_settled", async (_e, ctx) => {
    ctxNow = ctx;
    await ackSettled();
    publish("idle");
  });

  pi.on("session_shutdown", async () => {
    if (heartbeat) clearInterval(heartbeat);
    heartbeat = null;
    await pump?.stop();
    pump = null;
    // Unacked messages stay queued for the next session; keep=true preserves the mailbox.
    await Promise.race([client?.unregister(true), new Promise((r) => setTimeout(r, 3000))]);
    if (client) {
      state = "stopped";
      publish();
    }
    client = null;
  });

  // ---- tools ---------------------------------------------------------------

  const need = () => {
    if (!client) throw new Error("C-3PO is not connected" + (fatal ? `: ${fatal}` : ""));
    return client;
  };
  const text = (v: unknown) => ({
    content: [{ type: "text" as const, text: typeof v === "string" ? v : JSON.stringify(v, null, 2) }],
    details: {},
  });

  pi.registerTool({
    name: "c3po_reply",
    label: "C-3PO reply",
    description:
      "Reply to a C-3PO message from another agent. Use the message_id shown in the incoming '[C-3PO message ... (id: ...)]' turn. Your ordinary reply text is not sent to the other agent; only this tool is.",
    parameters: Type.Object({
      message_id: Type.String({ description: "The id from the incoming message" }),
      response: Type.String({ description: "Your answer" }),
    }),
    async execute(_id, params) {
      return text(await need().reply(params.message_id, params.response));
    },
  });

  pi.registerTool({
    name: "c3po_send",
    label: "C-3PO send",
    description:
      "Send a new message to another agent on the C-3PO mesh (agent ids look like machine/project; use c3po_agents to find them). Replies arrive later as new turns.",
    parameters: Type.Object({
      to: Type.String({ description: "Target agent id" }),
      message: Type.String({ description: "What to say or ask" }),
      context: Type.Optional(Type.String({ description: "Background the recipient needs" })),
    }),
    async execute(_id, params) {
      return text(await need().send(params.to, params.message, params.context));
    },
  });

  pi.registerTool({
    name: "c3po_agents",
    label: "C-3PO agents",
    description: "List agents registered on the C-3PO mesh with their status (online/watching/offline) and descriptions.",
    parameters: Type.Object({}),
    async execute() {
      const agents = (await need().listAgents()) as any[];
      return text(
        agents
          .filter((a) => a.status !== "offline" || a.description)
          .map((a) => ({ id: a.id, status: a.status, description: a.description })),
      );
    },
  });

  pi.registerCommand("c3po", {
    description: "Show C-3PO connection status",
    handler: async (_args, ctx) => {
      ctx.ui.notify(
        client
          ? `C-3PO: ${client.agentId}, ${state}, ${awaitingRun.size + inRun.size} pending, ${pump?.dropped ?? 0} dropped${fatal ? `, error: ${fatal}` : ""}`
          : `C-3PO: not connected${fatal ? ` (${fatal})` : ""}`,
        "info",
      );
    },
  });
}
