/** Mock OpenAI-shaped model: on a "[C-3PO message from ... (id: X)]" turn it calls c3po_reply(X, "pong from pi"); otherwise says "done". */
import http from "node:http";

export function startMock(port = 0) {
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
    server.listen(port, "127.0.0.1", () => resolve({ server, port: (server.address() as any).port, seen })),
  );
}


if (import.meta.url === `file://${process.argv[1]}`) {
  const { port } = await startMock(Number(process.env.PORT || 18999));
  console.log(`mock model on ${port}`);
}
