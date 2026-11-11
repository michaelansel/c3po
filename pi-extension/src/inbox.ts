/**
 * Inbound pump: long-polls the coordinator and hands NEW messages to a callback.
 *
 * /agent/api/wait returns every unacked message, so a message that has been
 * delivered to the agent but not yet acked comes back on every poll. The pump
 * remembers what it already delivered and sleeps briefly when a poll holds
 * nothing new, so a long agent turn doesn't turn into a hot loop.
 *
 * Acking is the caller's job (after the agent has actually handled the turn).
 */
import { HttpError, sleep, type C3poClient, type Message } from "./client.ts";

export interface PumpOptions {
  client: C3poClient;
  deliver: (messages: Message[]) => void;
  /** Sender globs (`*` and `?`). Empty or undefined means accept anyone. Non-matching messages are acked unseen. */
  allowFrom?: string[];
  pollSec?: number;
  /** Sleep when a poll returned only already-delivered messages. */
  redeliverSleepMs?: number;
  log?: (msg: string) => void;
  /** Called once on a non-retryable rejection (400/401/403); the pump then stops. */
  onFatal?: (err: Error) => void;
  onDropped?: (m: Message) => void;
}

export function messageKey(m: Message): string {
  return String(m.reply_id || m.id || "");
}

export function globToRegExp(glob: string): RegExp {
  const esc = glob.replace(/[.+^${}()|[\]\\]/g, "\\$&").replace(/\*/g, ".*").replace(/\?/g, ".");
  return new RegExp(`^${esc}$`);
}

export class InboxPump {
  private o: Required<Pick<PumpOptions, "pollSec" | "redeliverSleepMs">> & PumpOptions;
  private ctl = new AbortController();
  private delivered = new Set<string>();
  private allow: RegExp[];
  private running: Promise<void> | null = null;
  dropped = 0;

  constructor(o: PumpOptions) {
    this.o = { pollSec: 240, redeliverSleepMs: 2000, ...o };
    this.allow = (o.allowFrom || []).filter(Boolean).map(globToRegExp);
  }

  start(): void {
    if (this.running) return;
    this.running = this.loop();
  }

  async stop(): Promise<void> {
    this.ctl.abort();
    await this.running?.catch(() => undefined);
    this.running = null;
  }

  /** Forget ids once they are acked, so the set doesn't grow forever. */
  markAcked(ids: string[]): void {
    for (const id of ids) this.delivered.delete(id);
  }

  get inFlight(): number {
    return this.delivered.size;
  }

  private permitted(m: Message): boolean {
    if (!this.allow.length) return true;
    const from = String(m.from_agent || "");
    return this.allow.some((re) => re.test(from));
  }

  private async loop(): Promise<void> {
    const { client, log } = this.o;
    const signal = this.ctl.signal;
    let backoff = 1000;
    while (!signal.aborted) {
      let res;
      try {
        res = await client.waitOnce(this.o.pollSec, signal);
      } catch (e) {
        if (signal.aborted) return;
        if (e instanceof HttpError && [400, 401, 403].includes(e.status)) {
          log?.(`coordinator rejected the watch: ${e.message}`);
          this.o.onFatal?.(e);
          return;
        }
        log?.(`wait failed (${(e as Error).message}); retrying in ${backoff}ms`);
        await sleep(backoff, signal);
        backoff = Math.min(backoff * 2, 60_000);
        continue;
      }
      backoff = 1000;
      if (res.retryAfterSec) {
        await sleep(res.retryAfterSec * 1000, signal);
        continue;
      }
      const fresh: Message[] = [];
      const reject: string[] = [];
      for (const m of res.messages) {
        const key = messageKey(m);
        if (!key || this.delivered.has(key)) continue;
        if (!this.permitted(m)) {
          reject.push(key);
          this.dropped++;
          this.o.onDropped?.(m);
          continue;
        }
        this.delivered.add(key);
        fresh.push(m);
      }
      if (reject.length) await client.ack(reject).catch((e) => log?.(`ack of dropped messages failed: ${e.message}`));
      if (fresh.length) {
        try {
          this.o.deliver(fresh);
        } catch (e) {
          // Delivery failed: forget them so the next poll hands them over again.
          for (const m of fresh) this.delivered.delete(messageKey(m));
          log?.(`deliver failed: ${(e as Error).message}`);
          await sleep(this.o.redeliverSleepMs, signal);
        }
      } else if (res.messages.length) {
        await sleep(this.o.redeliverSleepMs, signal);
      }
    }
  }
}

/** Render a message as the user turn pi sees. Includes the id the reply tool needs. */
export function formatMessage(m: Message): string {
  const key = messageKey(m);
  const from = m.from_agent || "unknown";
  if (m.reply_to) {
    return `[C-3PO reply from ${from} to your message ${m.reply_to} (id: ${key})]\n${m.message ?? m.response ?? ""}`;
  }
  const ctx = m.context ? `\n\nContext from sender:\n${m.context}` : "";
  return (
    `[C-3PO message from ${from} (id: ${key})]\n${m.message ?? ""}${ctx}\n\n` +
    `To answer, call the c3po_reply tool with message_id "${key}". Your normal reply text is NOT sent to ${from}.`
  );
}
