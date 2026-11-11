import test from "node:test";
import assert from "node:assert/strict";
import { HttpError, type Message } from "../src/client.ts";
import { InboxPump, formatMessage, globToRegExp, messageKey } from "../src/inbox.ts";

/** Fake client: each waitOnce pops the next scripted outcome; when the script runs out it blocks until aborted. */
function fakeClient(script: Array<Message[] | Error | { retryAfterSec: number }>) {
  const acked: string[][] = [];
  const client: any = {
    calls: 0,
    async waitOnce(_t: number, signal: AbortSignal) {
      client.calls++;
      const next = script.shift();
      if (next === undefined) {
        await new Promise<void>((r) => signal.addEventListener("abort", () => r(), { once: true }));
        return { messages: [] };
      }
      if (next instanceof Error) throw next;
      if (Array.isArray(next)) return { messages: next };
      return { messages: [], retryAfterSec: next.retryAfterSec };
    },
    async ack(ids: string[]) {
      acked.push(ids);
    },
  };
  return { client, acked };
}

const msg = (id: string, from = "a/b", extra: Partial<Message> = {}): Message => ({ id, from_agent: from, message: `m-${id}`, ...extra });
const until = async (cond: () => boolean, ms = 2000) => {
  const t0 = Date.now();
  while (!cond()) {
    if (Date.now() - t0 > ms) throw new Error("timeout waiting for condition");
    await new Promise((r) => setTimeout(r, 5));
  }
};

test("delivers each message once even when /wait keeps returning it", async () => {
  const { client } = fakeClient([[msg("1")], [msg("1")], [msg("1"), msg("2")]]);
  const got: string[] = [];
  const pump = new InboxPump({ client, deliver: (ms) => got.push(...ms.map(messageKey)), redeliverSleepMs: 1 });
  pump.start();
  await until(() => client.calls >= 4);
  await pump.stop();
  assert.deepEqual(got, ["1", "2"]);
  assert.equal(pump.inFlight, 2);
});

test("markAcked lets an id be delivered again if it reappears", async () => {
  const { client } = fakeClient([[msg("1")], [], [msg("1")]]);
  const got: string[] = [];
  const pump = new InboxPump({
    client,
    deliver: (ms) => {
      got.push(...ms.map(messageKey));
      pump.markAcked(ms.map(messageKey));
    },
    redeliverSleepMs: 1,
  });
  pump.start();
  await until(() => got.length === 2);
  await pump.stop();
});

test("replies are keyed by reply_id", () => {
  assert.equal(messageKey({ reply_id: "r", id: "i" }), "r");
  assert.equal(messageKey({ id: "i" }), "i");
});

test("allow list: non-matching senders are acked unseen and counted", async () => {
  const { client, acked } = fakeClient([[msg("1", "evil/x"), msg("2", "Michaels-Mac-mini/infra")]]);
  const got: string[] = [];
  const dropped: string[] = [];
  const pump = new InboxPump({
    client,
    deliver: (ms) => got.push(...ms.map(messageKey)),
    allowFrom: ["Michaels-Mac-mini/*"],
    onDropped: (m) => dropped.push(String(m.from_agent)),
    redeliverSleepMs: 1,
  });
  pump.start();
  await until(() => got.length === 1 && acked.length === 1);
  await pump.stop();
  assert.deepEqual(got, ["2"]);
  assert.deepEqual(acked, [["1"]]);
  assert.deepEqual(dropped, ["evil/x"]);
  assert.equal(pump.dropped, 1);
});

test("401 is fatal and stops the pump; 5xx and network errors retry", async () => {
  const fatalSeen: Error[] = [];
  const { client } = fakeClient([new HttpError(503, "edge"), new Error("ECONNRESET"), new HttpError(401, "nope")]);
  const pump = new InboxPump({ client, deliver: () => {}, onFatal: (e) => fatalSeen.push(e) });
  // backoff would be 1s + 2s; speed the test by shrinking via fake timers is overkill, so allow ~4s
  pump.start();
  await until(() => fatalSeen.length === 1, 6000);
  await pump.stop();
  assert.equal(client.calls, 3);
});

test("a throwing deliver puts messages back so the next poll retries them", async () => {
  let first = true;
  const { client } = fakeClient([[msg("1")], [msg("1")]]);
  const got: string[] = [];
  const pump = new InboxPump({
    client,
    deliver: (ms) => {
      if (first) {
        first = false;
        throw new Error("pi not ready");
      }
      got.push(...ms.map(messageKey));
    },
    redeliverSleepMs: 1,
  });
  pump.start();
  await until(() => got.length === 1);
  await pump.stop();
});

test("globToRegExp escapes regex metacharacters", () => {
  assert.ok(globToRegExp("a.b/*").test("a.b/proj"));
  assert.ok(!globToRegExp("a.b/*").test("aXb/proj"));
  assert.ok(globToRegExp("h?/x").test("h1/x"));
});

test("formatMessage carries the id and tells the model to use c3po_reply", () => {
  const t = formatMessage({ id: "x::y::1", from_agent: "x", message: "do it", context: "bg" });
  assert.match(t, /from x \(id: x::y::1\)/);
  assert.match(t, /do it/);
  assert.match(t, /Context from sender:\nbg/);
  assert.match(t, /c3po_reply/);
  const r = formatMessage({ reply_id: "r1", from_agent: "x", reply_to: "q1", message: "answer" });
  assert.match(r, /reply from x to your message q1/);
  assert.ok(!/c3po_reply/.test(r));
});
