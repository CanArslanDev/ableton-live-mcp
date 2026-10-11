import assert from "node:assert/strict";
import { test } from "node:test";

import { confirmPage, confirmed, escapeHtml } from "../src/dialogs.ts";

test("the dialog is a data: URL with the text escaped", () => {
  const url = confirmPage({ title: "Delete <b>3</b> tracks?", message: "Drums & Bass\nVox",
                            confirm_label: "Delete" });
  assert.ok(url.startsWith("data:text/html,"));
  const html = decodeURIComponent(url.slice("data:text/html,".length));
  assert.ok(html.includes("Delete &lt;b&gt;3&lt;/b&gt; tracks?"));
  assert.ok(html.includes("Drums &amp; Bass<br>Vox"));
  assert.ok(html.includes(">Delete</button>") && html.includes(">Cancel</button>"));
  assert.ok(html.includes("close_and_send"));
});

test("only an explicit yes confirms", () => {
  assert.equal(confirmed('{"confirmed":true}'), true);
  assert.equal(confirmed('{"confirmed":false}'), false);
  assert.equal(confirmed('{"confirmed":"true"}'), false);
  assert.equal(confirmed(""), false);
  assert.equal(confirmed(null), false);
  assert.equal(confirmed("not json"), false);
  assert.equal(escapeHtml(`'"`), "&#39;&quot;");
});
