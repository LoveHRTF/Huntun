// Paint the real backend scene in two browsers, then reload and reconnect it.
import assert from "node:assert/strict";
import {execFileSync} from "node:child_process";
import {readFileSync} from "node:fs";
import {createRequire} from "node:module";
import {fileURLToPath} from "node:url";

const require=createRequire(import.meta.url);
const {chromium}=require(process.env.HUNTUN_PLAYWRIGHT || "playwright");
const root=fileURLToPath(new URL("../../", import.meta.url));
const source=readFileSync(process.env.HUNTUN_OFFICE_SOURCE || new URL("../../huntun/office.js", import.meta.url), "utf8");
const fixture=JSON.parse(execFileSync(root+".venv/bin/python", ["-c", `
import copy,json,tempfile,time
from pathlib import Path
from datetime import datetime, timezone
from huntun.office import OfficeRuntime
from huntun.store import Store
from tests.test_office_state import state
with tempfile.TemporaryDirectory() as directory:
 store=Store(Path(directory)/'board.db')
 store.set_control('office_theme','chinese_tech')
 st=state('idle'); at=time.time()*1000
 thinker=copy.deepcopy(st['agents'][0]); thinker['name']='thinker'; thinker['live']['status']='working'; thinker['info']['activity']['kind']='thinking'
 st['agents'].append(thinker)
 runtime=OfficeRuntime(store,st,now=at,seed=1)
 st['agents'].append({**copy.deepcopy(st['agents'][0]),'name':'new-hire','live':{'status':'working'}})
 for i in range(1,13): runtime.tick(st,at+i*50)
 scene=runtime.view()
 runtime.close(); store.close()
 store=Store(Path(directory)/'motion.db'); store.set_control('office_theme','chinese_tech')
 st=state('idle'); runtime=OfficeRuntime(store,st,now=at,seed=1)
 stream=[runtime.view()]; st['agents'][0]['live']['status']='working'
 for i in range(1,101):
  runtime.tick(st,at+i*50)
  if i%5==0: stream.append(runtime.view())
 runtime.close(); store.close()
 layouts=[]
 for theme in runtime.themes:
  store=Store(Path(directory)/(theme+'.db')); store.set_control('office_theme',theme)
  st=state('idle'); st['agents'][0]['role']='master'; st['agents'][0]['name']='master'
  st['agents'] += [{**copy.deepcopy(st['agents'][0]),'name':f'dev-{i}','role':'backend'} for i in range(8)]
  runtime=OfficeRuntime(store,st,now=at,seed=1)
  layouts.append({'scene':runtime.view(),'map':runtime.ctx.execute('scene._debug().map'),
    'desks':runtime.ctx.execute('scene._debug().DESKS'),'beds':runtime.ctx.execute('scene._debug().BEDS')})
  runtime.close(); store.close()
 store=Store(Path(directory)/'exit.db');store.set_control('office_theme','chinese_tech')
 st=state();runtime=OfficeRuntime(store,st,now=at,seed=1)
 st['events']=[{'id':1,'agent':'human','kind':'comment','speech':'老板说完了。','detail':'#1: goodbye',
   'created_at':datetime.fromtimestamp((at+50)/1000,timezone.utc).isoformat()}]
 runtime.tick(st,at+50)
 runtime.ctx.eval('''const d=scene._debug(), b=d.chars["#boss"];
   b.hidden=false;b.tx=d.DOOR[0];b.ty=d.DOOR[1]+3;b.px=b.tx*32;b.py=b.ty*32;
   b.setup=[{k:"exit"},{k:"gone"}];b.moving=false;b.steps=[];''')
 for i in range(2,101):
  runtime.tick(None,at+i*50)
  if '#boss' not in runtime.scene['chars']:break
 else:raise AssertionError('Boss did not leave')
 exitScene=runtime.view();runtime.close();store.close()
 store=Store(Path(directory)/'resize.db');store.set_control('office_theme','chinese_tech')
 st=state();st['agents'][0]['name']='master';st['agents'][0]['role']='master'
 st['agents'] += [{**copy.deepcopy(st['agents'][0]),'name':f'dev-{i}','role':'backend'} for i in range(20)]
 runtime=OfficeRuntime(store,st,now=at,seed=1);largeRoom=runtime.view()
 st['agents']=[st['agents'][0],st['agents'][-1]]
 runtime.ctx.eval("for(const [name,c] of Object.entries(scene._debug().chars)) if(name!=='master' && name!=='dev-19' && !c.guard) delete scene._debug().chars[name]")
 runtime.tick(st,at+50);smallRoom=runtime.view();runtime.close();store.close()
 print(json.dumps({'scene':scene,'stream':stream,'layouts':layouts,'exitScene':exitScene,'largeRoom':largeRoom,'smallRoom':smallRoom}))
`], {cwd:root, encoding:"utf8", maxBuffer:16*1024*1024}));
const {scene,stream,layouts,exitScene,largeRoom,smallRoom}=fixture;
assert(Object.values(scene.chars).some(c=>c.moving && c.motion),"fixture must include movement in progress");
assert(["phone","chin"].includes(scene.chars.thinker.thinkStyle),"backend must choose the thinking pose");
const browser=await chromium.launch({headless:true, ...(process.env.HUNTUN_CHROMIUM ? {executablePath:process.env.HUNTUN_CHROMIUM} : {})});
const errors=[], requests=[];
const html=`<div id="officebox" style="width:900px;height:750px"></div><script src="./office.js"></script><script>
  const $=selector=>document.querySelector(selector), tr=x=>x;
  function hash(name) {let h=2166136261;for(const c of name){h^=c.charCodeAt(0);h=Math.imul(h,16777619)>>>0;}return h;}
  const BLABEL={codex:"Codex"};
  function openAgent(wid,name) { window.opened=name; }
  function openWatchdog(wid) { window.opened="watchdog"; }
  const api=async(path,body)=>{const response=await fetch(path,body?{method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify(body)}:{});return response.json();};
  window.office=makeOffice("fixture");
  api("api/w/fixture/office").then(scene=>{
    Math.random=()=>{throw new Error("Browser must not choose Office behavior");};
    office.start({office:scene});window.ready=true;
  });
</script>`;
try {
  const pages=[];
  for(let i=0;i<2;i++) {
    const context=await browser.newContext();
    const page=await context.newPage();pages.push(page);
    page.on("pageerror",e=>errors.push(e.message));
    await page.route("http://huntun.test/**", route=>{
      const pathname=new URL(route.request().url()).pathname;
      requests.push(pathname);
      if(pathname==="/huntun/office.js") return route.fulfill({contentType:"text/javascript",body:source});
      if(pathname==="/huntun/api/w/fixture/office") return route.fulfill({contentType:"application/json",body:JSON.stringify(scene)});
      return route.fulfill({contentType:"text/html",body:html});
    });
    await page.goto("http://huntun.test/huntun/");
    await page.waitForFunction(()=>window.ready);
  }
  for(const page of pages) {
    assert.deepEqual(await page.evaluate(()=>JSON.parse(JSON.stringify(office._debug().chars))),scene.chars);
    await page.waitForTimeout(600);
    assert.deepEqual(await page.evaluate(()=>JSON.parse(JSON.stringify(office._debug().chars))),scene.chars,"rendering cannot mutate server state");
    await page.reload();await page.waitForFunction(()=>window.ready);
    assert.deepEqual(await page.evaluate(()=>JSON.parse(JSON.stringify(office._debug().chars))),scene.chars,"reload preserves motion, scripts and idle decisions");
    await page.evaluate(()=>{const scene=office.checkpoint();office.stop();office.start({office:scene});});
    assert.deepEqual(await page.evaluate(()=>JSON.parse(JSON.stringify(office._debug().chars))),scene.chars,"reopening preserves the received scene");
    await page.evaluate(()=>office.stop());
  }
  assert.deepEqual(errors,[]);
  assert(requests.filter(path=>path==="/huntun/api/w/fixture/office").length>=4,"snapshot polls use the reverse-proxy prefix");
  // Check what is actually painted, including occupied bunks and cached furniture.
  for(const layout of layouts) {
    const context=await browser.newContext(), page=await context.newPage();
    page.on("pageerror",e=>errors.push(e.message));
    await page.route("http://huntun.test/**",route=>{
      const pathname=new URL(route.request().url()).pathname;
      if(pathname==="/huntun/office.js") return route.fulfill({contentType:"text/javascript",body:source});
      if(pathname==="/huntun/api/w/fixture/office") return route.fulfill({contentType:"application/json",body:JSON.stringify(layout.scene)});
      return route.fulfill({contentType:"text/html",body:html});
    });
    await page.goto("http://huntun.test/huntun/"); await page.waitForFunction(()=>window.ready);
    for(const action of ["initial","reload","reopen"]) {
      if(action==="reload") {await page.reload();await page.waitForFunction(()=>window.ready);}
      if(action==="reopen") await page.evaluate(()=>{office.stop();office.start();});
      const actual=await page.evaluate(()=>{
        const d=office._debug(), image=d.background(), before=image.getContext("2d").getImageData(0,0,image.width,image.height);
        d.renderMap(); const repainted=d.background(), after=repainted.getContext("2d").getImageData(0,0,repainted.width,repainted.height);
        return {map:d.map, desks:d.DESKS, beds:d.BEDS,
          cacheMatches:before.width===after.width && before.height===after.height && before.data.every((value,i)=>value===after.data[i]),
          visible:Object.values(d.chars).map(c=>d.renderChar(c)).filter(c=>!c.hidden).map(c=>c.name)};
      });
      assert.deepEqual(actual.map,layout.map,layout.scene.theme+" "+action+": furniture must match the backend room");
      assert.deepEqual(actual.desks,layout.desks); assert.deepEqual(actual.beds,layout.beds);
      assert(actual.cacheMatches,layout.scene.theme+" "+action+": cached background must match the restored furniture and banners");
      for(const c of Object.values(layout.scene.chars)) if(!c.hidden) assert(actual.visible.includes(c.name),"refresh must retain visible characters");
    }
    await context.close();
  }
  // Changing the room's geometry must replace the old background and buffered
  // coordinates together, retaining everyone still present across reloads.
  const resizeContext=await browser.newContext(),resizePage=await resizeContext.newPage();
  let room=largeRoom;
  resizePage.on('pageerror',e=>errors.push(e.message));
  await resizePage.route('http://huntun.test/**',route=>{
    const pathname=new URL(route.request().url()).pathname;
    if(pathname==='/huntun/office.js')return route.fulfill({contentType:'text/javascript',body:source});
    if(pathname==='/huntun/api/w/fixture/office')return route.fulfill({contentType:'application/json',body:JSON.stringify(room)});
    return route.fulfill({contentType:'text/html',body:html});
  });
  await resizePage.goto('http://huntun.test/huntun/');await resizePage.waitForFunction(()=>window.ready);
  const beforeResize=await resizePage.evaluate(()=>{const d=office._debug();return {width:d.MW,height:d.MH,background:[d.background().width,d.background().height]};});
  room=smallRoom;
  await resizePage.waitForFunction(expected=>office.checkpoint().layout.revision===expected,smallRoom.layout.revision);
  for(const reload of [false,true]) {
    if(reload){await resizePage.reload();await resizePage.waitForFunction(()=>window.ready);}
    const actual=await resizePage.evaluate(()=>{
      const d=office._debug();
      return {width:d.MW,height:d.MH,seats:office.checkpoint().seats,
        chars:Object.values(d.chars).filter(c=>!c.hidden).map(c=>({name:c.name,rendered:d.renderChar(c)})),
        background:[d.background().width,d.background().height]};
    });
    assert.equal(actual.width,smallRoom.layout.width);assert.equal(actual.height,smallRoom.layout.height);
    assert.equal(actual.seats.length,2);assert.equal(actual.chars.length,3);
    assert(actual.background[0]*actual.background[1]<beforeResize.background[0]*beforeResize.background[1],'the cached background must shrink too');
    assert.deepEqual(actual.background,[actual.width*beforeResize.background[0]/beforeResize.width,actual.height*beforeResize.background[1]/beforeResize.height]);
    for(const c of actual.chars) {
      assert(!c.rendered.hidden,'resize/reload must retain visible '+c.name);
      assert(c.rendered.px>=0 && c.rendered.px<actual.width*32 && c.rendered.py>=0 && c.rendered.py<actual.height*32,'render within the compact room');
    }
  }
  await resizeContext.close();
  // The canonical boss is gone, but the final buffered walk must still render.
  assert(!exitScene.chars["#boss"]);assert(exitScene.animation_characters["#boss"]);
  const exitContext=await browser.newContext(), exitPage=await exitContext.newPage();
  exitPage.on("pageerror",e=>errors.push(e.message));
  await exitPage.route("http://huntun.test/**",route=>{
    const pathname=new URL(route.request().url()).pathname;
    if(pathname==="/huntun/office.js") return route.fulfill({contentType:"text/javascript",body:source});
    if(pathname==="/huntun/api/w/fixture/office") return route.fulfill({contentType:"application/json",body:JSON.stringify(exitScene)});
    return route.fulfill({contentType:"text/html",body:html});
  });
  await exitPage.goto("http://huntun.test/huntun/");await exitPage.waitForFunction(()=>window.ready);
  const departure=await exitPage.evaluate(()=>new Promise(resolve=>{
    const samples=[], started=performance.now();
    function sample(at) {
      const d=office._debug(), c=d.renderCharacters().find(c=>c.name==="#boss");
      samples.push(c&&!c.hidden ? {x:c.px,y:c.py,dir:c.dir} : null);
      if(at-started<1400) requestAnimationFrame(sample);
      else resolve({samples,door:d.DOOR,canonicalBoss:!!d.chars["#boss"]});
    }
    requestAnimationFrame(sample);
  }));
  assert(!departure.canonicalBoss,"rendering must not restore a departed boss to the backend's active team");
  assert(departure.samples[0],"the boss must not disappear on receiving its deletion while the last walk is buffered");
  const crossing=departure.samples.filter(c=>c&&c.y<departure.door[1]*32);
  assert(crossing.length>=8,"paint the boss walking past the doorway across several render frames");
  assert(crossing.every(c=>c.dir==="up"));assert.equal(departure.samples.at(-1),null,"hide only at the recorded exit time");
  await exitPage.reload();await exitPage.waitForFunction(()=>window.ready);
  assert(await exitPage.evaluate(()=>office._debug().renderCharacters().some(c=>c.name==="#boss"&&!c.hidden)),
    "a fresh browser must receive the removed boss's buffered appearance");
  await exitContext.close();
  // A newer speech snapshot must not replace the speech in buffered poses.
  const speechScene=structuredClone(scene), speaking=speechScene.chars.thinker;
  const at=speechScene.at;
  speaking.say="FUTURE utterance";speaking.sayStart=at;speaking.sayUntil=at+20000;
  const pose=speechScene.animation_frames.at(-1).chars.thinker.slice();
  const oldPose=pose.slice();oldPose[19]="old";pose[19]="new";
  speechScene.animation_frames=[{at:at-1600,theme:speechScene.theme,layout_revision:speechScene.layout.revision,chars:{thinker:oldPose}},
    {at,theme:speechScene.theme,layout_revision:speechScene.layout.revision,chars:{thinker:pose}}];
  speechScene.animation_speech={old:{say:"完整内容早已开始显示",sayStart:at-1600,sayUntil:at+20000},
    new:{say:speaking.say,sayStart:at,sayUntil:speaking.sayUntil}};
  const speechContext=await browser.newContext(), speechPage=await speechContext.newPage();
  speechPage.on("pageerror",e=>errors.push(e.message));
  await speechPage.route("http://huntun.test/**",route=>{
    const pathname=new URL(route.request().url()).pathname;
    if(pathname==="/huntun/office.js") return route.fulfill({contentType:"text/javascript",body:source});
    if(pathname==="/huntun/api/w/fixture/office") return route.fulfill({contentType:"application/json",body:JSON.stringify(speechScene)});
    return route.fulfill({contentType:"text/html",body:html});
  });
  await speechPage.goto("http://huntun.test/huntun/");await speechPage.waitForFunction(()=>window.ready);
  await speechPage.evaluate(()=>{
    window.paintedSpeech=[];
    const fill=CanvasRenderingContext2D.prototype.fillText;
    CanvasRenderingContext2D.prototype.fillText=function(text,...args){paintedSpeech.push(text);return fill.call(this,text,...args);};
  });
  await speechPage.waitForTimeout(60);
  const speech=await speechPage.evaluate(()=>{
    const d=office._debug(), rendered=d.renderChar(d.chars.thinker);
    const before=d.nowMs(), original=Date.now;Date.now=()=>original()+120000;
    const clockError=Math.abs(d.nowMs()-before);Date.now=original;
    return {canonical:d.chars.thinker.say,rendered:rendered.say,painted:paintedSpeech,
      future:d.bubblePage(d.chars.thinker),blank:d.bubblePage({say:"  ",sayStart:0,sayUntil:1e15}),clockError};
  });
  assert.equal(speech.canonical,"FUTURE utterance");assert.equal(speech.rendered,"完整内容早已开始显示");
  assert(speech.painted.includes("完整内容早已开始显示"),"paint the entire buffered page immediately, without an empty typewriter bubble");
  assert(!speech.painted.includes("FUTURE utterance"));assert.equal(speech.future,null);assert.equal(speech.blank,null);
  assert(speech.clockError<5,"wall-clock adjustments must not jump Office playback");
  await speechContext.close();
  // Replay real 20 Hz backend motion with polls delayed by as much as 600 ms.
  const context=await browser.newContext(), page=await context.newPage();
  page.on("pageerror",e=>errors.push(e.message));
  let started=0, polls=0;
  await page.route("http://huntun.test/**", async route=>{
    const pathname=new URL(route.request().url()).pathname;
    if(pathname==="/huntun/office.js") return route.fulfill({contentType:"text/javascript",body:source});
    if(pathname==="/huntun/api/w/fixture/office") {
      if(!started) started=Date.now();
      const packet=stream[Math.min(stream.length-1,Math.floor((Date.now()-started)/250))];
      if(polls++%3===1) await new Promise(resolve=>setTimeout(resolve,600));
      return route.fulfill({contentType:"application/json",body:JSON.stringify(packet)});
    }
    return route.fulfill({contentType:"text/html",body:html});
  });
  await page.goto("http://huntun.test/huntun/");await page.waitForFunction(()=>window.ready);
  const samples=await page.evaluate(()=>new Promise(resolve=>{
    const samples=[], began=performance.now();
    function sample(at) {
      const d=office._debug(), c=d.renderChar(d.chars.dev), elapsed=at-began;
      if(elapsed>1200) { const clock=d.nowMs(), brackets=d.animationSamples(clock); samples.push({elapsed,clock,x:c.px,y:c.py,
        before:brackets.before.at,after:brackets.after.at}); }
      if(elapsed<4500) requestAnimationFrame(sample);else resolve(samples);
    }
    requestAnimationFrame(sample);
  }));
  let stalls=0, maxSpeed=0, clockErrors=0;
  for(let i=1;i<samples.length;i++) {
    const a=samples[i-1], b=samples[i], dt=b.elapsed-a.elapsed;
    if(Math.abs((b.clock-a.clock)-dt)>=8) clockErrors++;
    const distance=Math.hypot(b.x-a.x,b.y-a.y);
    if(distance<.01) stalls++;
    maxSpeed=Math.max(maxSpeed,distance/dt*1000);
    if(distance/dt*1000>180) console.log("Playback jump:",JSON.stringify({a,b,speed:distance/dt*1000}));
  }
  console.log(`Office playback: ${samples.length} frames, ${stalls} stalls, ${clockErrors} clock jumps, peak ${maxSpeed.toFixed(1)} px/s.`);
  assert.equal(clockErrors,0,"snapshot arrival must not reset the animation clock");
  assert(samples.length>90,"the renderer must run independently of 4 Hz polling");
  assert(stalls/samples.length<.1,`movement stalled in ${stalls}/${samples.length} rendered frames`);
  assert(maxSpeed<180,`movement jumped at ${maxSpeed.toFixed(1)} px/s`);
  assert.deepEqual(errors,[]);
  await page.evaluate(()=>{
    const d=office._debug(), c=d.renderChar(d.chars.dev), view=d.view(), canvas=document.querySelector("canvas"), bounds=canvas.getBoundingClientRect();
    canvas.dispatchEvent(new MouseEvent("click",{clientX:bounds.left+(view.ox+(c.px+16)*view.scale)*bounds.width/canvas.width,
      clientY:bounds.top+(view.oy+(c.py+8)*view.scale)*bounds.height/canvas.height}));
  });
  assert.equal(await page.evaluate(()=>window.opened),"dev","clicks must use the position actually displayed");
  await page.evaluate(()=>office.stop());
  console.log(`Office browser checks passed: furniture/background/characters across ${layouts.length} themes and reload/reopen, persistent state, no browser randomness, proxy paths, and smooth 4 Hz sync (${samples.length} frames, ${stalls} stalls, peak ${maxSpeed.toFixed(1)} px/s).`);
} finally {await browser.close();}
