"""What every panel shares: the page skeleton, theme, and the MCP Apps bridge.

The bridge speaks the ext-apps protocol over postMessage by hand (no SDK, no
network): `ui/initialize` → `ui/notifications/initialized`, then the host's
`tool-input` / `tool-result` notifications; `tools/call` lets a panel run a
tool through the host when the host offers `serverTools`. Everything is
built with DOM calls, never innerHTML, because track and clip names come
from the user's set.
"""

from __future__ import annotations

PROTOCOL_VERSION = "2026-01-26"  # ext-apps LATEST_PROTOCOL_VERSION

CSS = r"""
:root {
  --fg: var(--color-text-primary, #1d1d1f);
  --fg2: var(--color-text-secondary, #5b5b62);
  --bg2: var(--color-background-secondary, #f3f3f5);
  --bg3: var(--color-background-tertiary, #e8e8ec);
  --line: var(--color-border-primary, #dcdce1);
  --accent: #ff7a45;
  --good: #1f9d55; --warn: #d4870b; --bad: #d64545; --info: #3b82f6;
  --radius: var(--border-radius-md, 8px);
  --font: var(--font-sans, -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif);
  --mono: var(--font-mono, ui-monospace, "SF Mono", Menlo, Consolas, monospace);
  color-scheme: light dark;
}
:root[data-theme="dark"] {
  --fg: var(--color-text-primary, #ececf1); --fg2: var(--color-text-secondary, #a1a1aa);
  --bg2: var(--color-background-secondary, #26262b); --bg3: var(--color-background-tertiary, #303036);
  --line: var(--color-border-primary, #3a3a41);
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --fg: var(--color-text-primary, #ececf1); --fg2: var(--color-text-secondary, #a1a1aa);
    --bg2: var(--color-background-secondary, #26262b); --bg3: var(--color-background-tertiary, #303036);
    --line: var(--color-border-primary, #3a3a41);
  }
}
* { box-sizing: border-box; }
html, body { margin: 0; background: transparent; }
body { padding: 10px; color: var(--fg); font: 13px/1.45 var(--font); }
h1 { font-size: 15px; margin: 0 0 8px; font-weight: 600; }
h2 { font-size: 12px; margin: 14px 0 6px; font-weight: 600; color: var(--fg2);
     text-transform: uppercase; letter-spacing: .04em; }
.muted { color: var(--fg2); }
.mono { font-family: var(--mono); font-size: 12px; }
.row { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
.tiles { display: grid; grid-template-columns: repeat(auto-fill, minmax(120px, 1fr)); gap: 8px; }
.tile { background: var(--bg2); border-radius: var(--radius); padding: 8px 10px; }
.tile .label { font-size: 11px; color: var(--fg2); }
.tile .value { font-size: 18px; font-weight: 600; font-variant-numeric: tabular-nums; }
.tile.good .value { color: var(--good); } .tile.warn .value { color: var(--warn); }
.tile.bad .value { color: var(--bad); }
table { border-collapse: collapse; width: 100%; font-variant-numeric: tabular-nums; }
th, td { text-align: left; padding: 4px 6px; border-bottom: 1px solid var(--line); vertical-align: top; }
th { font-size: 11px; color: var(--fg2); font-weight: 600; }
td.num, th.num { text-align: right; }
ol, ul { margin: 0; padding-left: 20px; } li { margin: 3px 0; }
button { font: inherit; font-size: 12px; padding: 4px 10px; border-radius: 6px; cursor: pointer;
  border: 1px solid var(--line); background: var(--bg2); color: var(--fg); }
button:hover:not(:disabled) { border-color: var(--accent); }
button.primary { background: var(--accent); border-color: var(--accent); color: #fff; }
button:disabled { opacity: .5; cursor: default; }
.bar { height: 8px; border-radius: 4px; background: var(--bg3); overflow: hidden; min-width: 60px; }
.bar > span { display: block; height: 100%; background: var(--accent); }
.swatch { display: inline-block; width: 10px; height: 10px; border-radius: 3px; margin-right: 6px;
  vertical-align: -1px; }
.pill { display: inline-block; padding: 0 6px; border-radius: 9px; font-size: 11px;
  background: var(--bg3); color: var(--fg2); }
.notice { padding: 6px 10px; border-radius: var(--radius); background: var(--bg2); margin: 8px 0; }
.notice.bad { color: var(--bad); }
svg text { fill: var(--fg2); font: 10px var(--font); }
"""

BRIDGE_JS = r"""
const VERSION = "__VERSION__";
const host = { caps: {}, context: {}, input: {}, data: null };
let nextId = 1;
const pending = new Map();
const handlers = {};

function post(message) { window.parent.postMessage(message, "*"); }

function request(method, params) {
  const id = nextId++;
  post({ jsonrpc: "2.0", id, method, params: params || {} });
  return new Promise((resolve, reject) => {
    pending.set(id, { resolve, reject });
    setTimeout(() => {
      if (pending.has(id)) { pending.delete(id); reject(new Error(method + " timed out")); }
    }, method === "tools/call" ? 300000 : 15000);
  });
}

function notify(method, params) { post({ jsonrpc: "2.0", method, params: params || {} }); }

window.addEventListener("message", (event) => {
  const msg = event.data;
  if (!msg || msg.jsonrpc !== "2.0") return;
  if (msg.id !== undefined && (msg.result !== undefined || msg.error !== undefined)) {
    const waiter = pending.get(msg.id);
    if (!waiter) return;
    pending.delete(msg.id);
    if (msg.error) waiter.reject(new Error(msg.error.message || "request failed"));
    else waiter.resolve(msg.result);
    return;
  }
  if (!msg.method) return;
  if (msg.method === "ui/resource-teardown" && msg.id !== undefined) {
    post({ jsonrpc: "2.0", id: msg.id, result: {} });
    return;
  }
  const handler = handlers[msg.method];
  if (handler) handler(msg.params || {});
  else if (msg.id !== undefined) {
    post({ jsonrpc: "2.0", id: msg.id, error: { code: -32601, message: "Method not found" } });
  }
});

function applyContext(ctx) {
  if (!ctx) return;
  if (ctx.theme) document.documentElement.dataset.theme = ctx.theme;
  const vars = (ctx.styles && ctx.styles.variables) || {};
  for (const [name, value] of Object.entries(vars)) {
    if (value) document.documentElement.style.setProperty(name, value);
  }
}

function resultData(result) {
  if (!result) return {};
  if (result.structuredContent) return result.structuredContent;
  const text = (result.content || []).filter((c) => c.type === "text").map((c) => c.text).join("\n");
  try { return JSON.parse(text); } catch (e) { return { text }; }
}

function canCallTools() { return !!host.caps.serverTools; }

async function callTool(name, args) {
  if (!canCallTools()) throw new Error("This client cannot run tools from a panel.");
  const result = await request("tools/call", { name, arguments: args || {} });
  const data = resultData(result);
  if (result.isError) throw new Error(data.text || "The tool failed.");
  return data;
}

function el(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "style") node.setAttribute("style", value);
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? "" : String(value));
  }
  for (const child of children.flat(Infinity)) {
    if (child === undefined || child === null || child === false) continue;
    node.appendChild(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

const SVG_NS = "http://www.w3.org/2000/svg";
function svg(tag, attrs, ...children) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value !== undefined && value !== null) node.setAttribute(key, String(value));
  }
  for (const child of children.flat(Infinity)) {
    if (child === undefined || child === null || child === false) continue;
    node.appendChild(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

function show(...nodes) {
  const app = document.getElementById("app");
  app.replaceChildren(...nodes.flat(Infinity).filter(Boolean));
  reportSize();
}

function notice(text, kind) { return el("div", { class: "notice" + (kind ? " " + kind : "") }, text); }

function tile(label, value, status) {
  return el("div", { class: "tile" + (status ? " " + status : "") },
    el("div", { class: "label" }, label), el("div", { class: "value" }, value));
}

function fmt(value, digits, unit) {
  if (value === null || value === undefined || Number.isNaN(value)) return "–";
  const text = typeof value === "number" ? value.toFixed(digits === undefined ? 1 : digits) : String(value);
  return unit ? text + " " + unit : text;
}

function bar(fraction, color) {
  const width = Math.max(0, Math.min(1, fraction || 0)) * 100;
  return el("div", { class: "bar" },
    el("span", { style: "width:" + width.toFixed(1) + "%" + (color ? ";background:" + color : "") }));
}

function liveColor(index) {
  // A stable hue per Live colour index, close enough to tell tracks apart
  return "hsl(" + ((Number(index) || 0) * 47 % 360) + " 65% 55%)";
}

// A button that runs `action` once, shows progress, and reports failures
function actionButton(label, action, options) {
  const opts = options || {};
  const button = el("button", { class: opts.primary ? "primary" : "", title: opts.title });
  button.textContent = label;
  if (!canCallTools()) {
    button.disabled = true;
    button.title = "Ask the assistant instead: this client cannot run tools from a panel.";
    return button;
  }
  let armed = false;
  button.addEventListener("click", async () => {
    if (opts.confirm && !armed) {
      armed = true;
      button.textContent = opts.confirm;
      setTimeout(() => { if (armed) { armed = false; button.textContent = label; } }, 4000);
      return;
    }
    armed = false;
    button.disabled = true;
    button.textContent = (opts.busy || "Working") + "…";
    try {
      await action();
      button.textContent = opts.done || label;
    } catch (e) {
      button.textContent = label;
      const app = document.getElementById("app");
      app.insertBefore(notice(String(e.message || e), "bad"), app.firstChild);
      reportSize();
    } finally {
      button.disabled = false;
    }
  });
  return button;
}

let lastWidth = 0, lastHeight = 0, scheduled = false;
function reportSize() {
  if (scheduled) return;
  scheduled = true;
  requestAnimationFrame(() => {
    scheduled = false;
    const html = document.documentElement;
    const original = html.style.height;
    html.style.height = "max-content";
    const height = Math.ceil(html.getBoundingClientRect().height);
    html.style.height = original;
    const width = Math.ceil(window.innerWidth);
    if (width !== lastWidth || height !== lastHeight) {
      lastWidth = width; lastHeight = height;
      notify("ui/notifications/size-changed", { width, height });
    }
  });
}

async function start(render) {
  const draw = () => {
    try { render(host.data, host.input); } catch (e) { show(notice("Could not draw: " + e.message, "bad")); }
  };
  handlers["ui/notifications/tool-input"] = (params) => { host.input = params.arguments || {}; };
  handlers["ui/notifications/tool-result"] = (params) => { host.data = resultData(params); draw(); };
  handlers["ui/notifications/tool-cancelled"] = (params) =>
    show(notice("Cancelled" + (params.reason ? ": " + params.reason : ".")));
  handlers["ui/notifications/host-context-changed"] = (params) => {
    Object.assign(host.context, params); applyContext(params);
  };
  try {
    const init = await request("ui/initialize", {
      appInfo: { name: "Ableton Live MCP", version: VERSION },
      appCapabilities: { availableDisplayModes: ["inline"] },
      protocolVersion: "__PROTOCOL__",
    });
    host.caps = init.hostCapabilities || {};
    host.context = init.hostContext || {};
    applyContext(host.context);
  } catch (e) {
    // No host answered: opened on its own (a preview); nothing to draw yet
  }
  notify("ui/notifications/initialized", {});
  const observer = new ResizeObserver(reportSize);
  observer.observe(document.documentElement);
  observer.observe(document.body);
  reportSize();
}
"""


def page(title: str, script: str, css: str = "", version: str = "") -> str:
    """A complete, self-contained panel document."""
    bridge = BRIDGE_JS.replace("__VERSION__", version).replace("__PROTOCOL__", PROTOCOL_VERSION)
    return (
        "<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        f"<title>{title}</title>\n<style>{CSS}{css}</style>\n</head>\n<body>\n"
        "<div id=\"app\"><div class=\"muted\">Waiting for Live…</div></div>\n"
        f"<script>\n{bridge}\n{script}\nstart(render);\n</script>\n</body>\n</html>\n"
    )
