// Modal dialogs in Live: an HTML page as a data: URL. The page answers by
// posting {method: "close_and_send", params: [text]} to Live's web view
// (WKWebView on macOS, WebView2 on Windows), which closes it and resolves
// showModalDialog with the text.

export function escapeHtml(text: string): string {
  return text.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;",
                                             '"': "&quot;", "'": "&#39;" })[c] ?? c);
}

export interface ConfirmOptions {
  title?: unknown;
  message?: unknown;
  confirm_label?: unknown;
  cancel_label?: unknown;
}

export function confirmPage(options: ConfirmOptions): string {
  const title = escapeHtml(String(options.title ?? "AbletonMCP"));
  const message = escapeHtml(String(options.message ?? "Continue?")).replace(/\n/g, "<br>");
  const yes = escapeHtml(String(options.confirm_label ?? "OK"));
  const no = escapeHtml(String(options.cancel_label ?? "Cancel"));
  const html = `<!doctype html><html><head><meta charset="utf-8"><style>
body{margin:0;padding:18px 20px;font:13px -apple-system,"Segoe UI",sans-serif;background:#2b2b2b;color:#ddd}
h1{font-size:14px;margin:0 0 10px}p{margin:0 0 18px;line-height:1.45}
.row{display:flex;gap:8px;justify-content:flex-end}
button{font:inherit;padding:5px 14px;border-radius:4px;border:1px solid #555;background:#3a3a3a;color:#eee}
button.primary{background:#ff764d;border-color:#ff764d;color:#111}
</style></head><body><h1>${title}</h1><p>${message}</p>
<div class="row"><button id="no">${no}</button><button id="yes" class="primary">${yes}</button></div>
<script>
function send(m){var wk=window.webkit&&window.webkit.messageHandlers&&window.webkit.messageHandlers.live;
if(wk)wk.postMessage(m);else if(window.chrome&&window.chrome.webview)window.chrome.webview.postMessage(m);}
function answer(ok){send({method:"close_and_send",params:[JSON.stringify({confirmed:ok})]});}
document.getElementById("yes").onclick=function(){answer(true)};
document.getElementById("no").onclick=function(){answer(false)};
document.addEventListener("keydown",function(e){if(e.key==="Escape")answer(false);if(e.key==="Enter")answer(true);});
document.getElementById("yes").focus();
</script></body></html>`;
  return `data:text/html,${encodeURIComponent(html)}`;
}

// Anything but an explicit yes is a no: closing the window, an error, junk
export function confirmed(result: string | null | undefined): boolean {
  try {
    const parsed = JSON.parse(result ?? "");
    return parsed !== null && typeof parsed === "object" && parsed.confirmed === true;
  } catch {
    return false;
  }
}
