// The companion's local server: the same framing as the AbletonMCP Remote
// Script (one JSON object per line each way, requests carry an `id` the reply
// echoes, the first request is `hello` with the session token), so the MCP
// server talks to both the same way.
import { createServer, type Server, type Socket } from "node:net";

export const PROTOCOL = 1;

export type Handler = (params: Record<string, unknown>) => unknown;

export interface BridgeOptions {
  port: number;
  token: string;
  version: string;
  handlers: Record<string, Handler>;
  log?: (message: string) => void;
}

export interface Reply {
  id: unknown;
  status: "success" | "error";
  result?: unknown;
  message?: string;
}

export interface Connection {
  authenticated: boolean;
}

// Errors from node: builtins come from the host's realm, so `instanceof Error`
// is false inside the extension's vm context: read the message by shape.
export function messageOf(error: unknown): string {
  if (error !== null && typeof error === "object" && "message" in error) {
    return String((error as { message: unknown }).message);
  }
  return String(error);
}

// bigint (how the host returns integers) is not JSON
export function toJson(value: unknown): string {
  return JSON.stringify(value, (_key, v) => (typeof v === "bigint" ? Number(v) : v));
}

export async function handleLine(line: string, connection: Connection,
                                 options: BridgeOptions): Promise<Reply> {
  let request: { id?: unknown; type?: unknown; params?: unknown };
  try {
    request = JSON.parse(line);
  } catch {
    return { id: null, status: "error", message: "Invalid JSON" };
  }
  const id = request.id ?? null;
  const type = typeof request.type === "string" ? request.type : "";
  const params = (request.params && typeof request.params === "object"
    ? request.params : {}) as Record<string, unknown>;
  if (type === "hello") {
    if (params.token !== options.token) {
      return { id, status: "error", message: "Invalid session token" };
    }
    connection.authenticated = true;
    return { id, status: "success",
             result: { companion: options.version, protocol: PROTOCOL,
                       commands: Object.keys(options.handlers).sort() } };
  }
  if (!connection.authenticated) {
    return { id, status: "error", message: "Authentication required: send hello with the token" };
  }
  const handler = options.handlers[type];
  if (!handler) {
    return { id, status: "error", message: `Unknown command: ${type}` };
  }
  try {
    return { id, status: "success", result: await handler(params) };
  } catch (error) {
    return { id, status: "error", message: messageOf(error) };
  }
}

export function serve(socket: Socket, options: BridgeOptions): void {
  const connection: Connection = { authenticated: false };
  let buffer = "";
  let queue = Promise.resolve();
  socket.setEncoding("utf8");
  socket.on("data", (chunk: string) => {
    buffer += chunk;
    // A browser's request to localhost starts with an HTTP verb: drop it unanswered
    if (!connection.authenticated && buffer.trimStart().length > 0
        && !buffer.trimStart().startsWith("{")) {
      socket.destroy();
      return;
    }
    let newline: number;
    while ((newline = buffer.indexOf("\n")) >= 0) {
      const line = buffer.slice(0, newline).trim();
      buffer = buffer.slice(newline + 1);
      if (!line) continue;
      // One request at a time, in order: a dialog waiting for the user holds the line
      queue = queue.then(async () => {
        const reply = await handleLine(line, connection, options);
        if (!socket.destroyed) socket.write(toJson(reply) + "\n");
      });
    }
  });
  socket.on("error", (error) => options.log?.(`connection error: ${error.message}`));
}

export function startBridge(options: BridgeOptions): Server {
  const server = createServer((socket) => serve(socket, options));
  server.on("error", (error: NodeJS.ErrnoException) => {
    options.log?.(error.code === "EADDRINUSE"
      ? `port ${options.port} is in use; restart Live's Extension Host`
      : `server error: ${error.message}`);
  });
  server.listen(options.port, "127.0.0.1", () => options.log?.(`listening on 127.0.0.1:${options.port}`));
  return server;
}
