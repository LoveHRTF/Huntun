// Exercise the real Needs You dialog with full requests, inline replies and slow fetches.
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import {createRequire} from "node:module";
const require = createRequire(import.meta.url);
const {chromium} = require(process.env.HUNTUN_PLAYWRIGHT || "playwright");
const html = readFileSync(new URL("../../huntun/app.html", import.meta.url), "utf8");
const source = html.slice(html.indexOf("let attentionGeneration = 0;"), html.indexOf("// ---------------------------------------------------------------- local watchdog dialog"));
const markdown = html.slice(html.indexOf("function md(src)"), html.indexOf("const when ="));
const drafts = html.split("\n").filter(line => ["const draftKey =", "const loadDraft =", "const saveDraft =", "const clearDrafts ="].some(prefix => line.startsWith(prefix))).join("\n");
const style = html.slice(html.indexOf("<style>") + 7, html.indexOf("</style>"));
const browser = await chromium.launch({headless:true, ...(process.env.HUNTUN_CHROMIUM ? {executablePath:process.env.HUNTUN_CHROMIUM} : {})});
try {
  const page = await browser.newPage({viewport:{width:1100,height:860}});
  const errors = [];
  page.on("pageerror", e => errors.push(e.message));
  await page.route("http://attention.test/**", route => route.fulfill({contentType:"text/html",body:"<style>"+style+"</style><button id='needs'></button><dialog id='attdlg'></dialog>"}));
  await page.goto("http://attention.test/");
  await page.evaluate(() => {
    window.$ = (selector, root = document) => root.querySelector(selector);
    window.esc = text => String(text).replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll(">","&gt;").replaceAll('"',"&quot;");
    window.avatar = () => "";
    window.when = () => "now";
    window.attachMentions = () => {};
    window.boardId = "fixture";
    window.state = {workspace:{name:"Project fixture"}, agents:[{name:"qa-1",status:"active"}]};
    window.threadKey = window.chatKey = "";
    window.requests = Array.from({length:40}, (_,i) => ({
      id:i+1, agent:i === 39 ? "qa-1" : "master", thread_id:1, comment_id:i+1,
      title:"A complete decision", text:"@human **Choose a hostname**. " + "Full background. ".repeat(90) + "FINAL ASK: use localhost?",
      created_at:"2026-01-01"
    }));
    window.comments = Array.from({length:40}, (_,i) => ({id:i+1,author:"dev",body:"Context reply "+(i+1)}));
    window.sent = []; window.calls = []; window.replyError = false; window.fetchError = false;
    window.api = async (url, body) => {
      calls.push(url);
      if (url.includes("/attention") && !body && window.deferFetch) {
        window.deferFetch = false;
        await new Promise(resolve => {window.releaseFetch = resolve;});
      }
      if (body) {
        if (url.endsWith("/reply") && window.replyError) throw new Error("Server rejected this reply");
        if (window.deferReply) {window.deferReply = false; await new Promise(resolve => {window.releaseReply = resolve;});}
        const itemId = Number(url.match(/attention\/(\d+)/)[1]);
        requests = requests.filter(it => it.id !== itemId);
        sent.push({url,body});
        return url.endsWith("/reply") ? {author:"human",thread_id:1,body:body.body} : {ok:true};
      }
      const q = new URL(url,"http://attention.test").searchParams;
      if (url.includes("/threads/")) {
        const before = Number(q.get("before") || 0);
        const rows = comments.filter(c => !before || c.id < before);
        const page = rows.slice(-20);
        return {thread:q.get("include_thread") === "0" ? null : {id:1,author:"human",body:"Full original opening"},
          comments:page,has_more:rows.length > 20,first_id:page[0]?.id || 0,last_id:page.at(-1)?.id || 0};
      }
      if (window.fetchError) throw new Error("Temporary connection failure");
      const before = Number(q.get("before") || 0), limit = Number(q.get("limit") || 30);
      return {items:[...requests].sort((a,b) => b.id-a.id).filter(it => !before || it.id < before).slice(0,limit),
        pending_ids:[...requests].map(it => it.id).sort((a,b) => b-a),count:requests.length};
    };
  });
  await page.addScriptTag({content:markdown + drafts + "\n" + source});
  await page.evaluate(() => openAttention("fixture"));
  assert.equal(await page.locator(".att").count(), 30);
  assert.equal(await page.locator("#attproject").innerText(), "Project fixture");
  assert.match(await page.locator(".att-request").first().innerText(), /FINAL ASK/);
  assert.equal(await page.locator(".att-request b").first().innerText(), "Choose a hostname");
  assert.equal(await page.locator("#attcount").innerText(), "40 pending requests");
  await page.locator("#attmore").click();
  await page.waitForFunction(() => document.querySelectorAll(".att").length === 40);
  assert.equal(await page.locator("#attmore").isVisible(), false);

  // Background refresh preserves the draft, DOM, focus, selection and reading position.
  await page.locator("#attreply-25").fill("keep my draft");
  await page.locator("#attreply-25").focus();
  await page.evaluate(() => {
    const input = document.querySelector("#attreply-25");
    input.setSelectionRange(3,8);
    window.original = document.querySelector('.att[data-id="25"]');
    window.originalTop = original.getBoundingClientRect().top;
    requests.push({...requests[0],id:41,text:"@human A new complete question: use localhost?"});
  });
  await page.evaluate(() => document.querySelector("#attrefresh").onclick());
  assert.equal(await page.locator("#attreply-25").inputValue(), "keep my draft");
  assert.deepEqual(await page.evaluate(() => [original === document.querySelector('.att[data-id="25"]'),
    document.activeElement.id, document.activeElement.selectionStart,document.activeElement.selectionEnd]),
    [true,"attreply-25",3,8]);
  assert.ok(await page.evaluate(() => Math.abs(original.getBoundingClientRect().top - originalTop) < 1));
  assert.equal(await page.locator(".att").count(), 41);
  await page.evaluate(() => {fetchError = true;});
  await page.locator("#attrefresh").click();
  await page.waitForFunction(() => document.querySelector("#attstatus").textContent.includes("Temporary"));
  assert.equal(await page.locator("#attreply-25").inputValue(), "keep my draft");
  assert.equal(await page.locator(".att").count(), 41);
  await page.evaluate(() => {fetchError = false;});

  // Failed sends keep the draft; success replies within the dialog and clears only that item.
  const ask = page.locator('.att[data-id="40"]');
  await ask.locator("textarea").fill("Approved, use localhost.");
  await page.evaluate(() => {replyError = true;});
  await ask.locator(".attsend").click();
  await page.waitForFunction(() => document.querySelector('.att[data-id="40"] .att-status').textContent.includes("rejected"));
  assert.equal(await ask.locator("textarea").inputValue(), "Approved, use localhost.");
  assert.equal(await ask.locator(".attsend").isDisabled(), false);
  await page.evaluate(() => {replyError = false; deferReply = true;});
  await ask.locator(".attsend").click();
  await page.waitForFunction(() => typeof releaseReply === "function");
  assert.equal(await ask.locator(".attsend").isDisabled(), true);
  await page.evaluate(() => {document.querySelector('.att[data-id="40"] form').requestSubmit();releaseReply();});
  await page.waitForFunction(() => !document.querySelector('.att[data-id="40"]'));
  assert.deepEqual(await page.evaluate(() => sent.filter(s => s.url.endsWith("/reply"))),
    [{url:"/api/w/fixture/attention/40/reply",body:{body:"Approved, use localhost."}}]);
  assert.equal(await page.evaluate(() => document.querySelector("#attdlg").open), true);
  assert.equal(await page.evaluate(() => location.hash), "");

  // Context is lazy, complete and paged in place, without rendering an entire long thread.
  assert.equal(await page.evaluate(() => calls.filter(u => u.includes("/threads/")).length), 0);
  const other = page.locator('.att[data-id="39"]');
  await other.locator("summary").click();
  await page.waitForFunction(() => document.querySelector('.att[data-id="39"] .att-context article'));
  assert.equal(await other.locator(".att-context article").count(), 21);
  assert.match(await other.locator(".att-context").innerText(), /Full original opening/);
  await other.locator(".att-older").click();
  await page.waitForFunction(() => document.querySelectorAll('.att[data-id="39"] .att-context article').length === 41);
  assert.equal(await other.locator(".att-older").count(), 0);
  assert.match(await other.locator(".att-context article").first().innerText(), /Full original opening/);

  // Closing/reopening restores drafts; project namespaces prevent drafts crossing projects.
  await page.locator("#attclose").click();
  await page.evaluate(() => openAttention("fixture"));
  assert.equal(await page.locator("#attreply-25").inputValue(), "keep my draft");
  await page.locator("#attclose").click();
  await page.evaluate(() => openAttention("other-project"));
  assert.equal(await page.locator("#attreply-25").inputValue(), "");
  await page.locator("#attclose").click();
  await page.evaluate(() => openAttention("fixture"));

  // External resolution preserves a typed draft and prevents posting to a stale request.
  await page.evaluate(() => {requests = requests.filter(it => it.id !== 25);return document.querySelector("#attrefresh").onclick();});
  const resolved = page.locator('.att[data-id="25"]');
  assert.match(await resolved.locator(".att-status").innerText(), /draft is preserved/);
  assert.equal(await resolved.locator(".attsend").isDisabled(), true);
  assert.equal(await resolved.locator("textarea").inputValue(), "keep my draft");
  const reply = page.locator('.att[data-id="39"]');
  await reply.locator("textarea").fill("Use localhost");
  await reply.locator("textarea").press("Control+Enter");
  await page.waitForFunction(() => !document.querySelector('.att[data-id="39"]'));
  assert.equal(await page.evaluate(() => sent.filter(s => s.url.endsWith("/reply")).length), 2);
  await page.locator('.att[data-id="38"] .attdone').click();
  await page.waitForFunction(() => !document.querySelector('.att[data-id="38"]'));
  assert.equal(await page.evaluate(() => sent.at(-1).url), "/api/w/fixture/attention/38/resolve");

  // A late request from a closed dialog cannot paint another project's dialog.
  await page.evaluate(async () => {
    deferFetch = true;
    window.oldFetch = document.querySelector("#attrefresh").onclick();
    await new Promise(r => setTimeout(r,0));
    document.querySelector("#attdlg").close();
  });
  await page.evaluate(() => openAttention("fresh-project"));
  await page.locator("#attreply-30").fill("New project draft");
  await page.evaluate(async () => {releaseFetch();await oldFetch;});
  assert.equal(await page.locator("#attreply-30").inputValue(), "New project draft");
  await page.locator("#attclose").click();
  await page.setViewportSize({width:390,height:844});
  await page.evaluate(() => openAttention("phone-project"));
  assert.ok(await page.evaluate(() => {
    const dlg = document.querySelector("#attdlg");
    const scroll = document.querySelector("#attscroll");
    return dlg.getBoundingClientRect().width <= innerWidth && scroll.scrollWidth <= scroll.clientWidth + 1;
  }), "full requests and reply controls fit a phone without horizontal page overflow");
  await page.locator(".att textarea").first().fill("Phone reply");
  await page.locator(".attsend").first().click();
  await page.waitForFunction(() => sent.at(-1).body.body === "Phone reply");
  await page.locator("#attclose").click();
  await page.evaluate(() => {requests = [];return openAttention("empty-project");});
  assert.match(await page.locator("#attlist").innerText(), /Nothing needs you/);
  await page.locator("#attclose").click();
  assert.deepEqual(errors, []);
  console.log("Needs You browser checks passed: full requests, scoped inline replies, drafts, refresh position, context pages and stale-response isolation.");
} finally {await browser.close();}
