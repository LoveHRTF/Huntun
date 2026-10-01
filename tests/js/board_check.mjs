// Behavioral browser checks of the actual thread functions, with controlled fetch order.
// HUNTUN_PLAYWRIGHT may name an installed playwright package; no provider/server is used.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const { chromium } = require(process.env.HUNTUN_PLAYWRIGHT || "playwright");
const html = readFileSync(new URL("../../huntun/app.html", import.meta.url), "utf8");
const functions = html.slice(html.indexOf("async function drawThreads(force)"), html.indexOf("// ---------------------------------------------------------------- a new thread"));
const browser = await chromium.launch({ headless: true, ...(process.env.HUNTUN_CHROMIUM ? { executablePath: process.env.HUNTUN_CHROMIUM } : {}) });
try {
  const page = await browser.newPage();
  const errors = [];
  page.on("pageerror", e => errors.push(e.message));
  await page.setContent('<style>.messages{height:200px;overflow:auto}.msg{min-height:40px}</style><div id="threadsbox"></div>');
  await page.evaluate(() => {
    window.$ = (selector, root = document) => root.querySelector(selector);
    window.esc = text => String(text).replaceAll("&", "&amp;").replaceAll("<", "&lt;");
    window.mdCalls = 0;
    window.md = text => { mdCalls++; return esc(text); };
    window.avatar = () => "";
    window.when = () => "now";
    window.listScroll = () => 0;
    window.readingChat = () => false;
    window.typingInThreads = () => false;
    window.hidePending = window.showPending = window.attachMentions = () => {};
    window.calls = [];
    window.posts = Array.from({ length: 1000 }, (_, i) => ({ id: i + 1, author: "dev", body: "reply " + (i + 1), created_at: "2026-01-01" }));
    window.postThread = { id: 1, author: "human", title: "Long thread", body: "opening", created_at: "2026-01-01" };
    window.api = async url => {
      calls.push(url);
      if (window.deferNext) { window.deferNext = false; await new Promise(resolve => { window.releaseFetch = resolve; }); }
      const q = new URL(url, "http://localhost").searchParams;
      const after = Number(q.get("after") || 0), before = Number(q.get("before") || 0);
      const rows = posts.filter(c => (!after || c.id > after) && (!before || c.id < before));
      const comments = after ? rows.slice(0, 100) : rows.slice(-100);
      return { thread: q.get("include_thread") === "0" ? null : postThread, comments,
        has_more: rows.length > 100, first_id: comments[0]?.id || 0, last_id: comments.at(-1)?.id || after };
    };
  });
  await page.addScriptTag({ content: `
    let boardId = "aaaaaaaaaa", openThread = 1, sortBy = "activity", filterBy = "all", composing = false;
    let threadKey = "", chatKey = "", chatData = null, chatRestore = null, threadsDirty = false, shownThreads = new Map();
    let state = { agents: [], threads: [{...postThread, comment_count: 1000, last_activity: "1", snippet: "opening", last_comments: []}] };
    const sortedThreads = () => state.threads;
    ${functions}
  ` });
  await page.evaluate(async () => { await drawThreads(true); await new Promise(r => setTimeout(r, 0)); });
  assert.equal(await page.locator(".msg").count(), 101);
  assert.equal(await page.locator(".older-comments").count(), 1);
  await page.evaluate(() => { window.original = document.querySelector('[data-comment="901"]'); window.previousCalls = mdCalls; document.querySelector("#cbody").value = "keep my draft"; });
  await page.evaluate(async () => { posts.push({ id: 1001, author: "dev", body: "new reply", created_at: "2026-01-01" }); await drawChat(); });
  assert.equal(await page.evaluate(() => original === document.querySelector('[data-comment="901"]')), true);
  assert.equal(await page.evaluate(() => mdCalls - previousCalls), 1, "only new Markdown renders");
  assert.match(await page.evaluate(() => calls.at(-1)), /after=1000&include_thread=0/);
  assert.equal(await page.locator("#cbody").inputValue(), "keep my draft");
  await page.evaluate(async () => { state.threads[0].comment_count = 1001; state.threads[0].last_activity = "2"; await drawThreads(true); await new Promise(r => setTimeout(r, 0)); });
  assert.equal(await page.evaluate(() => original === document.querySelector('[data-comment="901"]')), true, "preview redraw preserves message nodes");
  assert.equal(await page.locator("#cbody").inputValue(), "keep my draft");
  await page.evaluate(async () => { document.querySelector("#messages").scrollTop = 0; await drawChat(true); });
  assert.equal(await page.locator('[data-comment="801"]').count(), 1);
  assert.equal(await page.evaluate(() => original === document.querySelector('[data-comment="901"]')), true);
  assert.match(await page.evaluate(() => calls.at(-1)), /before=901/);
  // A slow fetch cannot paint replies into another thread after navigation.
  await page.evaluate(async () => {
    window.deferNext = true;
    window.pending = drawChat();
    await new Promise(r => setTimeout(r, 0));
    openThread = 2;
    document.querySelector("#messages").innerHTML = '<p id="other-thread">other thread</p>';
    releaseFetch();
    await pending;
  });
  assert.equal(await page.locator("#other-thread").count(), 1);
  assert.equal(await page.locator(".msg").count(), 0);
  // An empty incremental poll retains its cursor.
  await page.evaluate(async () => { openThread = 1; chatKey = ""; await drawChat(); await drawChat(); });
  assert.match(await page.evaluate(() => calls.at(-1)), /after=1001/);
  // Hundreds of replies between polls drain forward pages, without omissions.
  await page.evaluate(async () => {
    for (let id = 1002; id <= 1501; id++) posts.push({ id, author: "dev", body: "reply " + id, created_at: "2026-01-01" });
    document.querySelector("#messages").scrollTop = document.querySelector("#messages").scrollHeight;
    await drawChat();
  });
  await page.waitForFunction(() => !!document.querySelector('[data-comment="1501"]'));
  assert.equal(await page.locator(".msg:not(.first)").count(), 500);
  assert.equal(await page.locator('[data-comment="1002"]').count(), 1);
  // Exercise the actual watchdog dialog: selection changes, persistent history and pending replies.
  await page.setContent('<dialog id="watchdogdlg"></dialog>');
  await page.evaluate(() => {
    window.MODELS = [];
    window.wdHistory = [];
    window.wdSelection = {};
    window.wdBusy = false;
    window.modelOptions = selected => ['m1', 'm2'].map(id => `<option value="${id}" ${selected === id ? 'selected' : ''}>${id}</option>`).join('');
    window.effortOptions = () => '<option>high</option>';
    window.bindEfforts = () => {};
    window.api = async (url, body) => {
      if (url === '/api/models') return {models: [{id:'m1'}, {id:'m2'}]};
      if (body) {
        window.wdSent = {url, ...body};
        wdSelection = body; wdBusy = true;
        wdHistory.push({id: wdHistory.length + 1, role:'human', body:body.message, model:body.model, target:body.target});
      }
      return {messages: wdHistory, busy: wdBusy, selection: wdSelection,
        agents:[{name:'master', title:'Master'}, {name:'dev', title:'Developer'}]};
    };
  });
  const watchdog = html.slice(html.indexOf('let watchdogGeneration = 0;'), html.indexOf('// ---------------------------------------------------------------- agent dialog'));
  await page.addScriptTag({content: watchdog});
  await page.evaluate(() => openWatchdog('aaaaaaaaaa'));
  await page.locator('#wdmodel').selectOption('m2');
  await page.locator('#wdtarget').selectOption('dev');
  await page.locator('#wdinput').fill('Resume this session');
  await page.locator('#wdsend').click();
  assert.deepEqual(await page.evaluate(() => ({model:wdSent.model, target:wdSent.target, message:wdSent.message})),
    {model:'m2', target:'dev', message:'Resume this session'});
  assert.equal(await page.locator('#wdsend').isDisabled(), true);
  assert.match(await page.locator('#wdmessages').innerText(), /Resume this session/);
  await page.locator('#wdclose').click();
  await page.evaluate(() => { wdBusy = false; wdHistory.push({id:2, role:'assistant', body:'Session resumed', model:'m2', target:'dev'}); });
  await page.evaluate(() => openWatchdog('aaaaaaaaaa'));
  assert.equal(await page.locator('#wdmodel').inputValue(), 'm2');
  assert.equal(await page.locator('#wdtarget').inputValue(), 'dev');
  assert.match(await page.locator('#wdmessages').innerText(), /Session resumed/);
  await page.locator('#wdmodel').selectOption('m1');
  assert.match(await page.locator('#wdmessages').innerText(), /Resume this session/);
  await page.locator('#wdclose').click();

  // The actual Office state machine must make the same choices for every backend/model.
  const officeSource = html.slice(html.indexOf("function makeOffice(wid)"), html.indexOf("const offices = {}"));
  await page.setContent('<div id="officebox" style="width:900px;height:750px"></div>');
  await page.addScriptTag({content: `
    const LANG = "en", tr = x => x;
    const hash = text => [...text].reduce((v, c) => ((v * 31) + c.charCodeAt(0)) >>> 0, 0);
    ${officeSource}
    window.testOffice = makeOffice("fixture");
  `});
  const coverage = await page.evaluate(() => {
    const backends = ["claude-code", "codex", "kimi", "api", "deepseek", "ollama", "llamacpp", "vllm"];
    const agents = backends.map((backend, i) => ({name: "dev-" + i, role: "backend", title: backend,
      model: backend === "codex" ? "gpt-6.1-sol" : "any-model", backend, status: "active", live: {status:"working"},
      info: {compactions: 12, compacting:false, activity:{kind:"tool", at:new Date(Date.now()-120000).toISOString(), active:true}}}));
    const st = {agents, running:true, events:[], limits:{backends:{}}};
    testOffice.start(st);
    const d = testOffice._debug();
    const results = [];
    const record = (condition, message) => {if (!condition) throw new Error(message);};
    for (const a of agents) {
      const c = d.chars[a.name];
      record(c.mode === "work", a.backend+": initial work");
      record(c.toiletUntil === 0, a.backend+": no old compaction replay");
      record(d.actKind(c) === "type", a.backend+": long running tool");
      c.act = {kind:"thinking", at:new Date(Date.now()-120000).toISOString(), active:true};
      record(d.actKind(c) === "think", a.backend+": long running thinking");
      c.act = {kind:"text", at:new Date(Date.now()-120000).toISOString(), active:true};
      record(d.actKind(c) === "type", a.backend+": streaming text");
      c.act.active = false;
      record(d.actKind(c) === "read", a.backend+": stale activity ends");
      a.info.compacting = true;
    }
    testOffice.update(st);
    for (const a of agents) record(d.chars[a.name].mode === "toilet", a.backend+": live compaction");
    for (const a of agents) {a.info.compacting = false; a.info.compactions++;}
    testOffice.update(st);
    for (const a of agents) record(d.chars[a.name].toiletUntil > Date.now()+10000, a.backend+": completed compaction");
    for (const a of agents) {
      const c = d.chars[a.name]; c.toiletUntil=0;
      a.live.status = "resuming";
      record(d.modeFor(a,c,{...st,running:false}) === "coffee", a.backend+": recovery coffee while paused");
      a.live.status = "working";
      record(d.modeFor(a,c,{...st,running:false}) === "work", a.backend+": recovery work while paused");
      a.live.status = "paused";
      record(d.modeFor(a,c,st) === "strike", a.backend+": pause");
      a.live.status = "paused (usage limit)";
      record(d.modeFor(a,c,st) === "strike", a.backend+": limit");
      a.live.status = "error";
      record(d.modeFor(a,c,st) === "faint", a.backend+": error");
      a.live.status = "idle";
      record(d.modeFor(a,c,st) === "sleep", a.backend+": idle");
      a.live.status = "working";
      st.events = [{id:1,agent:a.name,kind:"comment",created_at:new Date().toISOString(),detail:"@all progress"}];
      record(d.modeFor(a,c,st) === "talk", a.backend+": discussion post");
      c.talk = null; st.events=[];
      results.push(a.backend);
    }
    const master = agents[0]; master.role="master"; d.chars[master.name].role="master";
    st.events=[{id:2,agent:master.name,kind:"delivery",created_at:new Date().toISOString(),detail:"Finished"}];
    testOffice.update(st);
    record(d.chars[master.name].mode === "print", "delivery printing");
    st.events=[{id:3,agent:"watchdog",kind:"recovery",created_at:new Date().toISOString(),detail:"Wake master"}];
    testOffice.update(st);
    record(d.guardQueue().some(job => job.k === "recovery"), "watchdog recovery animation");
    const newcomer = {...agents[1], name:"new-hire", live:{status:"working"}, info:{compactions:0}};
    st.agents = [...agents, newcomer]; testOffice.update(st);
    record(d.chars[newcomer.name].newHire && d.chars[newcomer.name].setup.length > 0, "hiring furniture animation");
    st.agents = agents; testOffice.update(st);
    record(d.chars[newcomer.name].leaving && d.chars[newcomer.name].mode === "leave", "retirement animation");
    testOffice.stop();
    return results;
  });
  assert.equal(coverage.length, 8);
  assert.deepEqual(errors, []);
  console.log("Board browser checks passed: paging, incremental rendering, drafts, navigation races, cursor retention, bounded live tail, persistent watchdog dialog, and Office animation mappings for all eight backends.");
} finally {
  await browser.close();
}
