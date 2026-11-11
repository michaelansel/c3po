/**
 * Coordinator client for the pi extension: REST for register / wait / unregister
 * (same endpoints the Claude Code hooks use) and a minimal MCP-over-HTTP client
 * for ack / reply / send / ping, which have no REST equivalent.
 *
 * Plain Node (fetch). No pi imports, so it is testable on its own.
 */
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

export interface Config {
  coordinatorUrl: string;
  /** Composite `server_secret.api_key`; empty in dev mode. */
  apiToken: string;
  /** Path of the MCP endpoint on the coordinator. `/agent/mcp` in prod, `/mcp` against a bare local coordinator. */
  mcpPath: string;
}

export interface Message {
  id?: string;
  reply_id?: string;
  from_agent?: string;
  to_agent?: string;
  message?: string;
  response?: string;
  context?: string | null;
  reply_to?: string;
  timestamp?: string;
  [k: string]: unknown;
}

export class HttpError extends Error {
  status: number;
  body: string;
  constructor(status: number, body: string) {
    super(`HTTP ${status}: ${body.slice(0, 200)}`);
    this.status = status;
    this.body = body;
  }
}

/** Load config from env, falling back to the credentials file the Claude hooks use. */
export function loadConfig(env: NodeJS.ProcessEnv = process.env): Config {
  let creds: Record<string, string> = {};
  const file = env.C3PO_CREDENTIALS_FILE || path.join(os.homedir(), ".claude", "c3po-credentials.json");
  try {
    creds = JSON.parse(fs.readFileSync(file, "utf8"));
  } catch {
    /* no file: env only (or dev mode) */
  }
  let apiToken = env.C3PO_API_TOKEN || creds.api_token || "";
  if (!apiToken && creds.server_secret && creds.api_key) apiToken = `${creds.server_secret}.${creds.api_key}`;
  const coordinatorUrl = (env.C3PO_COORDINATOR_URL || creds.coordinator_url || "http://localhost:8420").replace(/\/+$/, "");
  return { coordinatorUrl, apiToken, mcpPath: env.C3PO_MCP_PATH || "/agent/mcp" };
}

/** Same sanitising as hooks/c3po_common.sanitize_name. */
export function sanitizeName(name: string): string {
  let out = name.replace(/[^a-zA-Z0-9_./-]/g, "-");
  while (out.includes("--")) out = out.replace(/--/g, "-");
  return out.replace(/^-+|-+$/g, "");
}

export function sleep(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    if (signal?.aborted) return resolve();
    const t = setTimeout(done, ms);
    function done() {
      signal?.removeEventListener("abort", done);
      clearTimeout(t);
      resolve();
    }
    signal?.addEventListener("abort", done, { once: true });
  });
}

export class C3poClient {
  cfg: Config;
  /** Full agent id once registered (may carry a -2 collision suffix). */
  agentId: string;
  sessionId: string;
  private mcpSession: string | null = null;
  private rpcId = 0;

  constructor(cfg: Config, agentId: string, sessionId: string) {
    this.cfg = cfg;
    this.agentId = agentId;
    this.sessionId = sessionId;
  }

  private headers(extra: Record<string, string> = {}): Record<string, string> {
    const h: Record<string, string> = { "X-Machine-Name": this.agentId, "X-Session-ID": this.sessionId, ...extra };
    if (this.cfg.apiToken) h.Authorization = `Bearer ${this.cfg.apiToken}`;
    return h;
  }

  /** POST /agent/api/register. Adopts the id the coordinator assigns. */
  async register(): Promise<{ id: string; [k: string]: unknown }> {
    const [machine, ...rest] = this.agentId.split("/");
    const res = await fetch(`${this.cfg.coordinatorUrl}/agent/api/register`, {
      method: "POST",
      headers: this.headers({ "X-Machine-Name": machine, "X-Project-Name": rest.join("/") }),
      body: "",
    });
    const text = await res.text();
    if (!res.ok) throw new HttpError(res.status, text);
    const out = JSON.parse(text);
    if (out.id) this.agentId = out.id;
    return out;
  }

  /** POST /agent/api/unregister. `keep` leaves the mailbox so queued messages survive. */
  async unregister(keep = true): Promise<void> {
    await fetch(`${this.cfg.coordinatorUrl}/agent/api/unregister${keep ? "?keep=true" : ""}`, {
      method: "POST",
      headers: this.headers(),
      body: "",
    }).catch(() => undefined);
  }

  /**
   * One long-poll of /agent/api/wait. Returns every UNACKED message, including ones
   * already delivered, so callers must de-duplicate. `retry` (coordinator shutting
   * down) and `timeout` both come back as an empty array.
   */
  async waitOnce(timeoutSec: number, signal?: AbortSignal): Promise<{ messages: Message[]; retryAfterSec?: number }> {
    const res = await fetch(`${this.cfg.coordinatorUrl}/agent/api/wait?timeout=${timeoutSec}`, {
      headers: this.headers(),
      signal,
    });
    const text = await res.text();
    if (!res.ok) throw new HttpError(res.status, text);
    const data = JSON.parse(text);
    if (data.status === "retry") return { messages: [], retryAfterSec: Number(res.headers.get("retry-after") || 15) };
    return { messages: data.status === "received" ? data.messages || [] : [] };
  }

  // ---- MCP ---------------------------------------------------------------

  private async rpc(method: string, params: unknown, opts: { notify?: boolean } = {}): Promise<any> {
    const body: Record<string, unknown> = { jsonrpc: "2.0", method, params };
    if (!opts.notify) body.id = ++this.rpcId;
    const extra: Record<string, string> = {
      "Content-Type": "application/json",
      Accept: "application/json, text/event-stream",
    };
    if (this.mcpSession) extra["Mcp-Session-Id"] = this.mcpSession;
    const res = await fetch(`${this.cfg.coordinatorUrl}${this.cfg.mcpPath}`, {
      method: "POST",
      headers: this.headers(extra),
      body: JSON.stringify(body),
    });
    const text = await res.text();
    if (!res.ok) throw new HttpError(res.status, text);
    const sid = res.headers.get("mcp-session-id");
    if (sid) this.mcpSession = sid;
    if (opts.notify || !text.trim()) return null;
    const ctype = res.headers.get("content-type") || "";
    let payload: any;
    if (ctype.includes("text/event-stream")) {
      const dataLine = text.split("\n").filter((l) => l.startsWith("data:")).map((l) => l.slice(5).trim()).pop();
      payload = dataLine ? JSON.parse(dataLine) : null;
    } else {
      payload = JSON.parse(text);
    }
    if (payload?.error) throw new Error(`MCP ${method}: ${payload.error.message || JSON.stringify(payload.error)}`);
    return payload?.result;
  }

  private async mcpInit(): Promise<void> {
    this.mcpSession = null;
    await this.rpc("initialize", {
      protocolVersion: "2025-03-26",
      capabilities: {},
      clientInfo: { name: "c3po-pi-extension", version: "0.1.0" },
    });
    await this.rpc("notifications/initialized", {}, { notify: true });
  }

  /** Call a coordinator MCP tool and return its parsed result (structured content, else JSON text, else raw text). */
  async callTool(name: string, args: Record<string, unknown> = {}): Promise<any> {
    const attempt = async () => {
      if (!this.mcpSession) await this.mcpInit();
      return this.rpc("tools/call", { name, arguments: args });
    };
    let result: any;
    try {
      result = await attempt();
    } catch (e) {
      // The MCP session can disappear (coordinator restart): re-initialise once.
      if (e instanceof HttpError && (e.status === 400 || e.status === 404)) {
        this.mcpSession = null;
        result = await attempt();
      } else throw e;
    }
    if (result?.isError) {
      const msg = (result.content || []).map((c: any) => c.text).join("\n");
      throw new Error(`${name} failed: ${msg}`);
    }
    if (result?.structuredContent) return result.structuredContent.result ?? result.structuredContent;
    const text = (result?.content || []).map((c: any) => c.text).join("\n");
    try {
      return JSON.parse(text);
    } catch {
      return text;
    }
  }

  ack(ids: string[]) {
    return this.callTool("ack_messages", { message_ids: ids });
  }
  reply(messageId: string, response: string) {
    return this.callTool("reply", { message_id: messageId, response });
  }
  send(to: string, message: string, context?: string) {
    return this.callTool("send_message", { to, message, ...(context ? { context } : {}) });
  }
  ping() {
    return this.callTool("ping", {});
  }
  listAgents() {
    return this.callTool("list_agents", {});
  }
}
