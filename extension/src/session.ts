// Where the MCP server finds the port and token: a JSON file it can read but
// other users cannot. Some Live betas only let extensions write inside their
// storage directory, so that is the fallback (the server searches there too).
import { randomBytes } from "node:crypto";
import { mkdirSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

import { messageOf } from "./bridge.ts";

export const SESSION_FILE = "companion-session.json";

export interface Session {
  port: number;
  token: string;
  companion: string;
  pid: number;
}

export function newToken(): string {
  return randomBytes(24).toString("hex");
}

export function defaultFolders(storageDirectory: string | undefined): string[] {
  const folders = [join(homedir(), ".ableton-mcp")];
  if (storageDirectory) folders.push(storageDirectory);
  return folders;
}

/** The path written, or null when no folder could be written. */
export function writeSession(session: Session, folders: string[],
                             log?: (message: string) => void): string | null {
  for (const folder of folders) {
    try {
      mkdirSync(folder, { recursive: true, mode: 0o700 });
      const path = join(folder, SESSION_FILE);
      writeFileSync(path, JSON.stringify(session), { mode: 0o600 });
      return path;
    } catch (error) {
      log?.(`cannot write ${folder}: ${messageOf(error)}`);
    }
  }
  return null;
}
