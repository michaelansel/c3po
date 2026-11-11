/**
 * Round trip with a real pi + model against a real coordinator:
 * sender -> pi (extension) -> model calls c3po_reply -> sender sees the reply.
 *
 * Driven by scripts/roundtrip-check.sh. Env:
 *   PI_CMD            JSON array: the command that starts pi in RPC mode (the script builds it)
 *   C3PO_CREDENTIALS_FILE  throwaway key; its agent_pattern must allow both ids below
 *   PI_AGENT_ID, SENDER_ID  e.g. vale-palm/pi-roundtrip, vale-palm/pi-sender
 *   QUESTION, EXPECT  question to ask; substring the reply must contain (optional)
 */
import { spawn } from "node:child_process";
import { C3poClient, loadConfig, sleep } from "../src/client.ts";

const cmd: string[] = JSON.parse(process.env.PI_CMD!);
const piId = process.env.PI_AGENT_ID!;
const senderId = process.env.SENDER_ID!;
const question = process.env.QUESTION || "What is 17 * 23? Work it out and answer with just the number.";
const expect = process.env.EXPECT || "";
const cfg = loadConfig();
const log = (m: string) => console.log(`[roundtrip] ${m}`);

const pi = spawn(cmd[0], cmd.slice(1), { stdio: ["pipe", "pipe", "pipe"], env: process.env });
let err = "";
pi.stderr.on("data", (d) => (err += d));
pi.stdout.resume();

const sender = new C3poClient(cfg, senderId, `roundtrip-${Date.now()}`);
let code = 1;
try {
  await sender.register();
  // Wait for pi's extension to register: it shows up in the agent list.
  const t0 = Date.now();
  for (;;) {
    const agents = (await sender.listAgents()) as any[];
    if (agents.some((a) => a.id === piId && a.status !== "offline")) break;
    if (Date.now() - t0 > 90_000) throw new Error(`pi never registered as ${piId}`);
    await sleep(1000);
  }
  log(`pi is up as ${piId}`);
  const sent = await sender.send(piId, question);
  log(`sent ${sent.id}`);
  const t1 = Date.now();
  let reply: any;
  while (!reply) {
    if (Date.now() - t1 > 180_000) throw new Error("no reply within 180 s");
    const { messages } = await sender.waitOnce(5);
    reply = messages.find((m) => m.reply_to === sent.id);
  }
  log(`reply after ${Math.round((Date.now() - t1) / 1000)}s: ${JSON.stringify(reply.message)}`);
  await sender.ack([reply.reply_id || reply.id]);
  if (expect && !String(reply.message).includes(expect)) throw new Error(`reply lacks "${expect}"`);
  // The inbound message must be acked once the turn settled (nothing pending for pi).
  const t2 = Date.now();
  for (;;) {
    const r = await fetch(`${cfg.coordinatorUrl}/agent/api/pending`, {
      headers: { "X-Machine-Name": piId, ...(cfg.apiToken ? { Authorization: `Bearer ${cfg.apiToken}` } : {}) },
    });
    if (((await r.json()) as any).count === 0) break;
    if (Date.now() - t2 > 30_000) throw new Error("inbound message was never acked");
    await sleep(500);
  }
  log("inbound acked; PASS");
  code = 0;
} catch (e) {
  log(`FAIL: ${(e as Error).message}`);
  if (err) log(`pi stderr tail:\n${err.split("\n").slice(-25).join("\n")}`);
} finally {
  pi.stdin.end();
  await sleep(2000);
  pi.kill();
  await sender.unregister(false).catch(() => undefined);
}
process.exit(code);
