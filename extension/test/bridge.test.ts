import assert from "node:assert/strict";
import { once } from "node:events";
import { connect, type AddressInfo } from "node:net";
import { test } from "node:test";

import { handleLine, startBridge, toJson, type BridgeOptions } from "../src/bridge.ts";

const options: BridgeOptions = {
  port: 0, token: "secret", version: "9.9.9",
  handlers: {
    echo: (params) => params,
    fail: () => { throw new Error("nope"); },
    slow: async () => { await new Promise((r) => setTimeout(r, 50)); return "slow"; },
  },
};

test("hello with the token unlocks the commands", async () => {
  const connection = { authenticated: false };
  const locked = await handleLine('{"id":1,"type":"echo","params":{}}', connection, options);
  assert.match(locked.message ?? "", /Authentication required/);
  const wrong = await handleLine('{"id":2,"type":"hello","params":{"token":"x"}}', connection, options);
  assert.equal(wrong.message, "Invalid session token");
  const hello = await handleLine('{"id":3,"type":"hello","params":{"token":"secret"}}', connection, options);
  assert.deepEqual(hello, { id: 3, status: "success",
                            result: { companion: "9.9.9", protocol: 1, commands: ["echo", "fail", "slow"] } });
  assert.deepEqual(await handleLine('{"id":4,"type":"echo","params":{"a":1}}', connection, options),
                   { id: 4, status: "success", result: { a: 1 } });
  assert.deepEqual(await handleLine('{"id":5,"type":"fail"}', connection, options),
                   { id: 5, status: "error", message: "nope" });
  assert.equal((await handleLine('{"id":6,"type":"what"}', connection, options)).message,
               "Unknown command: what");
  assert.equal((await handleLine("{oops", connection, options)).message, "Invalid JSON");
});

test("bigints become numbers in replies", () => {
  assert.equal(toJson({ pitch: 60n }), '{"pitch":60}');
});

async function listening() {
  const server = startBridge(options);
  await once(server, "listening");
  return { server, port: (server.address() as AddressInfo).port };
}

test("over a socket: one line per request, answered in order", async () => {
  const { server, port } = await listening();
  const socket = connect(port, "127.0.0.1");
  socket.setEncoding("utf8");
  let received = "";
  socket.on("data", (chunk: string) => { received += chunk; });
  socket.write('{"id":1,"type":"hello","params":{"token":"secret"}}\n'
               + '{"id":2,"type":"slow"}\n{"id":3,"type":"echo","params":{"b":2}}\n');
  while (received.split("\n").length < 4) await new Promise((r) => setTimeout(r, 10));
  const ids = received.trim().split("\n").map((line) => JSON.parse(line).id);
  assert.deepEqual(ids, [1, 2, 3]);
  socket.destroy();
  server.close();
});

test("a browser's HTTP request is dropped unanswered", async () => {
  const { server, port } = await listening();
  const socket = connect(port, "127.0.0.1");
  let answer = "";
  socket.on("data", (chunk) => { answer += String(chunk); });
  socket.write("POST / HTTP/1.1\r\nHost: localhost\r\n\r\n{}\n");
  await once(socket, "close");
  assert.equal(answer, "");
  server.close();
});
