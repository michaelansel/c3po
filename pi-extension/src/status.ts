/** Idle/busy status file for the launcher (and anything else watching this pi from outside). */
import fs from "node:fs";
import path from "node:path";

export type State = "starting" | "idle" | "busy" | "stopped";

export interface Status {
  agent_id: string;
  session_id: string;
  state: State;
  /** Delivered to the agent but not yet acked. */
  pending: number;
  dropped: number;
  since: string;
  updated: string;
  error?: string;
}

export function writeStatus(file: string | undefined, s: Status): void {
  if (!file) return;
  try {
    fs.mkdirSync(path.dirname(file), { recursive: true });
    const tmp = `${file}.${process.pid}.tmp`;
    fs.writeFileSync(tmp, JSON.stringify(s) + "\n", { mode: 0o600 });
    fs.renameSync(tmp, file);
  } catch {
    /* status is best effort */
  }
}
