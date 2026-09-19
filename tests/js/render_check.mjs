// Headless render check for huntun/app.html: loads the page in jsdom with canned API responses and reports
// any uncaught error or an empty view. Usage: node render_check.mjs <route> <responses.json> [app.html]
// responses.json maps URL path (without query) -> JSON body; "*" is the fallback.
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import path from "node:path";

const require = createRequire(import.meta.url);
const { JSDOM, VirtualConsole } = require("jsdom");
const [route, responsesFile, appFile] = process.argv.slice(2);
const here = path.dirname(fileURLToPath(import.meta.url));
const html = readFileSync(appFile || path.join(here, "..", "..", "huntun", "app.html"), "utf8");
const responses = JSON.parse(readFileSync(responsesFile, "utf8"));
const errors = [];
const finish = () => {
  const view = dom?.window.document.querySelector("#view")?.textContent?.trim() || "";
  const main = dom?.window.document.querySelector("#main")?.textContent?.trim() || "";
  console.log(JSON.stringify({ route, errors, viewChars: view.length, mainChars: main.length, status: dom?.window.document.querySelector("#hstatus")?.textContent || "", sample: main.replace(/\s+/g, " ").slice(0, 220), text: main.replace(/\s+/g, " ").slice(0, 20000) }));
  process.exit(errors.length || main.length < 40 ? 1 : 0);
};
process.on("uncaughtException", (e) => { errors.push("uncaught: " + (e.stack || e)); finish(); });
process.on("unhandledRejection", (e) => { errors.push("unhandledRejection: " + (e?.stack || e)); finish(); });
let dom;
const vc = new VirtualConsole();
vc.on("jsdomError", (e) => errors.push("jsdomError: " + (e.detail?.stack || e.message || e)));
vc.on("error", (m) => errors.push("console.error: " + m));

dom = new JSDOM(html, { runScripts: "outside-only", pretendToBeVisual: true, url: "http://127.0.0.1:4747/" + (route || "#/"), virtualConsole: vc });
const w = dom.window;
w.fetch = async (url, opts) => {
  const p = new URL(url, "http://127.0.0.1:4747").pathname;
  const body = responses[p] ?? responses["*"];
  if (body === undefined) { errors.push("no canned response for " + p); return { ok: false, statusText: "not found", json: async () => ({ error: "no canned response for " + p }) }; }
  return { ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(body)) };
};
w.confirm = () => true; w.alert = (m) => errors.push("alert: " + m);
if (!w.HTMLDialogElement.prototype.showModal) { w.HTMLDialogElement.prototype.showModal = function () { this.open = true; }; w.HTMLDialogElement.prototype.close = function () { this.open = false; }; }
w.addEventListener("error", (e) => errors.push("window.error: " + (e.error?.stack || e.message)));
w.addEventListener("unhandledrejection", (e) => errors.push("unhandledrejection: " + (e.reason?.stack || e.reason)));
w.onerror = (m, s, l, c, e) => errors.push("onerror: " + (e?.stack || m));
// Run the inline script the way the browser would, but through the window so our fetch stub is used.
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
try { w.eval(script); } catch (e) { errors.push("script threw: " + (e.stack || e)); }
await new Promise((r) => setTimeout(r, 400));
finish();
