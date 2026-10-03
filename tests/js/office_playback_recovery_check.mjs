// Sustained playback after slow initial loads, clock drift, and a network outage.
import assert from "node:assert/strict";
import {execFileSync} from "node:child_process";
import {readFileSync} from "node:fs";
import {createRequire} from "node:module";
import {fileURLToPath} from "node:url";

const require = createRequire(import.meta.url);
const {chromium} = require(process.env.HUNTUN_PLAYWRIGHT || "playwright");
const root = fileURLToPath(new URL("../../", import.meta.url));
const source = readFileSync(new URL("../../huntun/office.js", import.meta.url), "utf8");
const base = JSON.parse(execFileSync(root + ".venv/bin/python", ["-c", `
import json,tempfile,time
from pathlib import Path
from huntun.office import OfficeRuntime
from huntun.store import Store
from tests.test_office_state import state
with tempfile.TemporaryDirectory() as directory:
 store=Store(Path(directory)/'board.db');store.set_control('office_theme','chinese_tech')
 runtime=OfficeRuntime(store,state(),now=time.time()*1000,seed=1)
 print(json.dumps(runtime.view()));runtime.close();store.close()
`], {cwd:root, encoding:"utf8"}));
const html = `<div id="officebox" style="width:900px;height:750px"></div><script src="./office.js"></script><script>
 const $=s=>document.querySelector(s), tr=x=>x, BLABEL={codex:"Codex"};
 function hash(name){let h=2166136261;for(const c of name){h^=c.charCodeAt(0);h=Math.imul(h,16777619)>>>0;}return h;}
 const api=async path=>(await fetch(path)).json();
 window.textMeasurements=0;
 const measure=CanvasRenderingContext2D.prototype.measureText;
 CanvasRenderingContext2D.prototype.measureText=function(...args){textMeasurements++;return measure.apply(this,args);};
 const office=makeOffice("fixture");
 api("api/w/fixture/office").then(scene=>{
   Math.random=()=>{throw new Error("The browser cannot choose agent behavior");};
   office.start({office:scene});window.ready=true;
 });
</script>`;
const browser = await chromium.launch({headless:true, ...(process.env.HUNTUN_CHROMIUM ? {executablePath:process.env.HUNTUN_CHROMIUM} : {})});
const cases = [
  {name:"slow first response", initialDelay:2800},
  {name:"slow polling after slow first response", initialDelay:2800, pollDelay:600},
  {name:"playback clock behind", skew:5000},
  {name:"playback clock ahead", skew:-5000},
  {name:"connection restored", outage:2500},
];
try {
  for (const scenario of cases) {
    const context = await browser.newContext(), page = await context.newPage(), errors = [];
    page.on("pageerror", e=>errors.push(e.message));
    let started = 0, serial = 0, skew = 0, offline = false;
    const packet = () => {
      const elapsed = Date.now() - started, out = structuredClone(base);
      out.at = base.at + elapsed + skew; out.revision = ++serial;
      const poseAt = t => {
        const phase = ((t / 1000 % 6) + 6) % 6;
        const x = 150 + (phase <= 3 ? phase : 6 - phase) * 80;
        return [x,220,Math.floor(x/32),6,phase<3?"right":"left","w1",true,.3,"work",1,
          null,null,null,null,false,"type",null,false,null,null];
      };
      out.animation_frames = [];
      for (let t = elapsed - 2400; t <= elapsed; t += 50) {
        const frame = structuredClone(base.animation_frames.at(-1));
        frame.at = base.at + t + skew; frame.chars.dev = poseAt(t);
        out.animation_frames.push(frame);
      }
      const pose = poseAt(elapsed), c = out.chars.dev;
      [c.px,c.py,c.tx,c.ty,c.dir,c.frame,c.moving,c.prog,c.mode] = pose;
      c.steps = [[c.tx+1,c.ty]];
      c.motion = {at:out.at,ms:200,from:[c.px,c.py],to:[c.px+(c.dir==="right"?16:-16),c.py]};
      return out;
    };
    await page.route("http://huntun.test/**", async route => {
      const path = new URL(route.request().url()).pathname;
      if (path === "/huntun/office.js") return route.fulfill({contentType:"text/javascript",body:source});
      if (path === "/huntun/api/w/fixture/office") {
        if (offline) return route.abort();
        if (!started) started = Date.now();
        const first = !serial, body = JSON.stringify(packet());
        if (first && scenario.initialDelay) await new Promise(r=>setTimeout(r,scenario.initialDelay));
        else if (scenario.pollDelay) await new Promise(r=>setTimeout(r,scenario.pollDelay));
        return route.fulfill({contentType:"application/json",body});
      }
      return route.fulfill({contentType:"text/html",body:html});
    });
    await page.goto("http://huntun.test/huntun/");
    await page.waitForFunction(()=>window.ready);
    await page.waitForTimeout(800);
    skew = scenario.skew || 0;
    if (scenario.outage) {
      offline = true; await page.waitForTimeout(scenario.outage); offline = false;
    }
    await page.waitForTimeout(1200);
    const samples = await page.evaluate(()=>new Promise(resolve=>{
      window.textMeasurements=0;
      const result = [], began = performance.now();
      function sample(at) {
        const d = office._debug(), clock = d.nowMs(), c = d.renderChar(d.chars.dev), bracket = d.animationSamples(clock);
        result.push({elapsed:at-began,clock,x:c.px,y:c.py,before:bracket.before.at,after:bracket.after.at});
        if (at-began < 2200) requestAnimationFrame(sample); else resolve(result);
      }
      requestAnimationFrame(sample);
    }));
    let stalls = 0, jumps = 0, unbuffered = 0;
    for (let i=1;i<samples.length;i++) {
      const a=samples[i-1], b=samples[i], speed=Math.hypot(b.x-a.x,b.y-a.y)/(b.elapsed-a.elapsed)*1000;
      if (speed<1) stalls++;
      if (speed>180) jumps++;
      if (b.before===b.after) unbuffered++;
    }
    console.log(`${scenario.name}: ${samples.length} frames, ${stalls} stalls, ${jumps} jumps, ${unbuffered} outside buffer.`);
    assert(samples.length>80, "rendering must remain independent of network polling");
    assert(stalls/samples.length<.1, `${scenario.name}: permanently stalled in ${stalls}/${samples.length} frames`);
    assert.equal(jumps,0, `${scenario.name}: playback must recover from polling-speed movement`);
    assert(unbuffered/samples.length<.1, `${scenario.name}: the playhead must recover into the recorded buffer`);
    assert.deepEqual(errors,[]);
    assert(await page.evaluate(()=>textMeasurements)<10, "unchanged name labels must not be measured on every frame");
    await context.close();
  }
} finally {await browser.close();}
