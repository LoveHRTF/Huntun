// Headless check for huntun/login.html: renders it in a language, submits the form against a canned /api/auth/login
// answer and reports what the page shows. Usage: node login_check.mjs <lang> <status> '<json answer>'
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import path from "node:path";

const require = createRequire(import.meta.url);
const { JSDOM, VirtualConsole } = require("jsdom");
const [lang, status, answer] = process.argv.slice(2);
const here = path.dirname(fileURLToPath(import.meta.url));
const html = readFileSync(path.join(here, "..", "..", "huntun", "login.html"), "utf8");
const errors = [], sent = [];
let reloaded = false;
const vc = new VirtualConsole();
vc.on("jsdomError", (e) => { if (/navigation/i.test(e.message || "")) reloaded = true; else errors.push("jsdomError: " + (e.message || e)); });
vc.on("error", (m) => errors.push("console.error: " + m));
const dom = new JSDOM(html, { runScripts: "outside-only", url: "http://127.0.0.1:4747/", virtualConsole: vc });
const w = dom.window, d = w.document;
if (lang && lang !== "en") w.localStorage.setItem("huntun.lang", lang);
w.fetch = async (url, opts) => { sent.push({ url, method: opts.method, headers: opts.headers, body: JSON.parse(opts.body) });
  return { ok: Number(status) < 400, status: Number(status), statusText: "status " + status, json: async () => JSON.parse(answer) }; };
try { w.eval(html.match(/<script>([\s\S]*?)<\/script>/)[1]); } catch (e) { errors.push("script threw: " + (e.stack || e)); }
const before = d.body.textContent.replace(/\s+/g, " ").trim();
d.getElementById("username").value = "ziwei"; d.getElementById("password").value = "pw";
d.getElementById("login").dispatchEvent(new w.Event("submit", { cancelable: true }));
await new Promise((r) => setTimeout(r, 100));
console.log(JSON.stringify({ errors, lang: d.documentElement.lang, text: before, error: d.getElementById("error").textContent, sent, reloaded,
  buttonEnabled: !d.getElementById("submit").disabled }));
