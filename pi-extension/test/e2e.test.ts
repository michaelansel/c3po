/**
 * End-to-end: a real pi process (RPC mode) with this extension, a real coordinator, and a mock model.
 *
 * Skipped unless C3PO_TEST_LIVE=1. Needs:
 *   C3PO_TEST_URL   coordinator base URL, default http://127.0.0.1:8420 (bare dev coordinator: `python -m coordinator.server`)
 *   C3PO_TEST_MCP_PATH  default /mcp (a bare coordinator has no nginx to rewrite /agent/mcp)
 *   PI_CLI          path to pi's dist/cli.js (npm i @earendil-works/pi-coding-agent)
 */
import test from "node:test";
import assert from "node:assert/strict";
import http from "node:http";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawn } from "node:child_process";
import { C3poClient, sleep } from "../src/client.ts";

const live = process.env.C3PO_TEST_LIVE === "1" && !!process.env.PI_CLI;
const URL_ = process.env.C3PO_TEST_URL || "http://127.0.0.1:8420";
const MCP_PATH = process.env.C3PO_TEST_MCP_PATH || "/mcp";
const EXT = path.resolve(import.meta.dirname, "../extensions/c3po.ts");

function startMock() {
  const seen: any[] = [];
  const server = http.createServer((req, res) => {
    let body = "";
    req.on("data", (d) => (body += d));
    req.on("end", () => {
      if (req.url!.endsWith("/models")) return res.writeHead(200).end(JSON.stringify({ data: [{ id: "mock" }] }));
      const j = JSON.parse(body || "{}");
      seen.push(j);
      const last = (j.messages || []).at(-1) || {};
      const lastText = typeof last.content === "string" ? last.content : JSON.stringify(last.content);
      const chunk = (delta: any, fin?: string) =>
        `data: ${JSON.stringify({ id: "c", object: "chat.completion.chunk", model: "mock", choices: [{ index: 0, delta, finish_reason: fin ?? null }] })}\n\n`;
      res.writeHead(200, { "content-type": "text/event-stream" });
      const m = last.role === "user" && /\[C-3PO message from [^\]]*\(id: ([^)]+)\)/.exec(lastText);
      if (m) {
        const args = JSON.stringify({ message_id: m[1], response: "pong from pi" });
        res.write(chunk({ role: "assistant", content: "" }));
        res.write(chunk({ tool_calls: [{ index: 0, id: "call_1", type: "function", function: { name: "c3po_reply", arguments: args } }] }));
        res.write(chunk({}, "tool_calls"));
      } else {
        res.write(chunk({ role: "assistant", content: "done" }));
        res.write(chunk({}, "stop"));
      }
      res.end("data: [DONE]\n\n");
    });
  });
  return new Promise<{ server: http.Server; port: number; seen: any[] }>((resolve) =>
    server.listen(0, "127.0.0.1", () => resolve({ server, port: (server.address() as any).port, seen })),
  );
}

async function until(cond: () => boolean | Promise<boolean>, ms = 20000) {
  const t0 = Date.now();
  while (!(await cond())) {
    if (Date.now() - t0 > ms) throw new Error("timeout");
    await sleep(100);
  }
}

test("pi receives a C-3PO message, replies with c3po_reply, acks, and goes idle", { skip: !live }, async () => {
  const mock = await startMock();
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "c3po-pi-"));
  fs.mkdirSync(path.join(dir, "agent"));
  fs.mkdirSync(path.join(dir, "work"));
  fs.writeFileSync(
    path.join(dir, "agent", "models.json"),
    JSON.stringify({ providers: { mock: { baseUrl: `http://127.0.0.1:${mock.port}/v1`, api: "openai-completions", apiKey: "x", models: [{ id: "mock" }] } } }),
  );
  const suffix = Math.random().toString(36).slice(2, 8);
  const agentId = `pitest-host/e2e-${suffix}`;
  const statusFile = path.join(dir, "status.json");
  const pi = spawn("node", [process.env.PI_CLI!, "--mode", "rpc", "--provider", "mock", "--model", "mock", "--approve", "--no-session", "-e", EXT], {
    cwd: path.join(dir, "work"),
    env: {
      ...process.env,
      PI_CODING_AGENT_DIR: path.join(dir, "agent"),
      C3PO_COORDINATOR_URL: URL_,
      C3PO_MCP_PATH: MCP_PATH,
      C3PO_AGENT_ID: agentId,
      C3PO_STATUS_FILE: statusFile,
      C3PO_CREDENTIALS_FILE: path.join(dir, "none.json"),
    },
    stdio: ["pipe", "pipe", "pipe"],
  });
  let stderr = "";
  pi.stderr.on("data", (d) => (stderr += d));
  pi.stdout.resume();

  const me = new C3poClient({ coordinatorUrl: URL_, apiToken: "", mcpPath: MCP_PATH }, `pitest-host/sender-${suffix}`, `sender-${suffix}`);
  try {
    await me.register();
    const status = () => JSON.parse(fs.readFileSync(statusFile, "utf8"));
    await until(() => fs.existsSync(statusFile) && status().state === "idle");
    assert.equal(status().agent_id, agentId);

    const sent = await me.send(agentId, "ping please");
    // The reply arrives as a reply to our message, written by the model through c3po_reply.
    let reply: any;
    await until(async () => {
      const { messages } = await me.waitOnce(2);
      reply = messages.find((m) => m.reply_to === sent.id);
      return !!reply;
    });
    assert.equal(reply.message, "pong from pi");
    await me.ack([reply.reply_id || reply.id]);

    // The inbound message is acked only after the turn settled, and the status returns to idle.
    await until(async () => {
      const r = await fetch(`${URL_}/agent/api/pending`, { headers: { "X-Machine-Name": agentId } });
      return ((await r.json()) as any).count === 0 && status().state === "idle" && status().pending === 0;
    });

    assert.ok(mock.seen.some((j) => JSON.stringify(j.messages).includes("ping please")), "model saw the message");
    assert.ok(mock.seen.some((j) => (j.tools || []).some((t: any) => t.function?.name === "c3po_reply")), "c3po_reply tool offered to the model");
  } finally {
    pi.stdin.end();
    await sleep(1500);
    // Shutdown unregistered with keep=true: the mailbox is still listed, but no longer online.
    const entry = ((await me.listAgents()) as any[]).find((a) => a.id === agentId);
    pi.kill();
    await me.unregister(false);
    mock.server.close();
    if (process.env.C3PO_TEST_DEBUG) console.error(stderr);
    assert.ok(!entry || entry.status !== "online", `agent still online after shutdown: ${JSON.stringify(entry)}`);
  }
});
