// Pi setup and project/agent harness selection using the actual app, behind a proxy prefix.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {createRequire} from 'node:module';
const require=createRequire(import.meta.url);
const {chromium}=require(process.env.HUNTUN_PLAYWRIGHT || 'playwright');
const html=readFileSync(new URL('../../huntun/app.html',import.meta.url),'utf8').replaceAll('"/api/','"/huntun/api/').replaceAll("'/api/","'/huntun/api/");
const projects=[{id:'aaaaaaaaaa',name:'Existing fixture',path:'/fixture/existing',initialized:true,approved:true,state:'ready'},
 {id:'bbbbbbbbbb',name:'New fixture',path:'/fixture/new',initialized:false,approved:false,state:'new'}];
const models=[{id:'pi-clm:fixture/model',backend:'pi-clm',vendor:'fixture',label:'Fixture · fixture',context:123456,efforts:['none','high']},
 {id:'pi-clm:default',backend:'pi-clm',vendor:'pi',label:'Pi configured default',context:200000},
 {id:'gpt-fixture',backend:'codex',vendor:'openai',label:'GPT fixture',context:400000,efforts:['high']}];
const board={workspace:projects[0],approved:true,goal_confirmed:true,goal:'Fixture',backend:'codex',default_model:'',running:false,
 totals:{tokens:0,cost_usd:0},limits:{},attention_count:0,max_agents:0,models,threads:[],events:[],tasks:[],
 agents:[{name:'dev',role:'backend',title:'Developer',brief:'Implement',status:'active',backend:'codex',model:'gpt-fixture',effort:'high',live:{status:'idle'},info:{context_tokens:0,context_limit:400000}}]};
const writes=[],errors=[];
const browser=await chromium.launch({headless:true,...(process.env.HUNTUN_CHROMIUM ? {executablePath:process.env.HUNTUN_CHROMIUM} : {})});
try {
 const page=await browser.newPage({viewport:{width:1440,height:1000}});
 page.on('pageerror',e=>errors.push(e.message));
 await page.route('**/*',async route=>{
  const req=route.request(),url=new URL(req.url());
  if(url.host !== 'huntun.test')return route.abort();
  if(url.pathname === '/huntun/')return route.fulfill({contentType:'text/html',body:html});
  const asset=url.pathname.match(/^\/huntun\/(office\.js|performance\.js|performance\.css)$/);
  if(asset)return route.fulfill({contentType:asset[1].endsWith('.css') ? 'text/css':'text/javascript',body:readFileSync(new URL('../../huntun/'+asset[1],import.meta.url),'utf8')});
  const path=url.pathname.replace('/huntun','');
  let result;
  if(path === '/api/workspaces')result={workspaces:projects,home:'/fixture'};
  else if(path === '/api/models')result={models,backends:{codex:'OpenAI Codex','pi-clm':'Pi + CLM'}};
  else if(path === '/api/workspaces/bbbbbbbbbb')result=projects[1];
  else if(path === '/api/w/aaaaaaaaaa/state')result=board;
  else if(path === '/api/w/aaaaaaaaaa/agents/dev')result={spec:board.agents[0],info:board.agents[0].info,notes:'',journal:[],files:[],activity:[],cursor:0};
  else if(path === '/api/w/aaaaaaaaaa/agents/dev/activity')result={entries:[],cursor:0,journal:[],agent:{...board.agents[0],...board.agents[0].info,cycles:0,usage:{}},live:{status:'idle'}};
  else if(req.method() === 'POST'){
   const body=req.postDataJSON();writes.push({path,body});
   if(path.endsWith('/harness')){
    board.backend=body.backend;board.default_model=body.model;
    result={ok:true,message:'Project harness updated. Takes effect from each agent\'s next cycle.'};
   }else if(path.endsWith('/model'))result={ok:true,message:'Model updated.'};
   else if(path.endsWith('/init'))result={...projects[1],state:'clarifying'};
  }
  if(!result){errors.push('Unexpected request: '+path);return route.fulfill({status:404,json:{error:'Missing fixture'}});}
  return route.fulfill({json:result});
 });
 await page.goto('http://huntun.test/huntun/#/w/bbbbbbbbbb/setup');
 await page.locator('#backend').waitFor();
 assert.equal(await page.locator('#backend option[value="pi-clm"]').count(),1);
 await page.locator('#backend').selectOption('pi-clm');
 await page.locator('#goal').fill('Use Pi for this project');
 await page.locator('#mastermodel').selectOption('pi-clm:fixture/model');
 await page.locator('#setupform button').first().click();
 await page.waitForTimeout(100);
 const init=writes.find(w=>w.path.endsWith('/init'));
 assert.equal(init.body.backend,'pi-clm');assert.equal(init.body.master_model,'pi-clm:fixture/model');
 await page.goto('http://huntun.test/huntun/#/w/aaaaaaaaaa/tasks');
 await page.locator('#harnessbtn').waitFor();
 await page.locator('#harnessbtn').click();
 await page.locator('#harnessbackend').selectOption('pi-clm');
 await page.locator('#harnessmodel').selectOption('pi-clm:fixture/model');
 await page.locator('#harnessapply').click();
 await page.waitForTimeout(100);
 assert.deepEqual(writes.find(w=>w.path.endsWith('/harness')).body,{backend:'pi-clm',model:'pi-clm:fixture/model',all_agents:false});
 await page.locator('#harnessall').check();await page.locator('#harnessapply').click();
 await page.waitForTimeout(100);assert.equal(writes.filter(w=>w.path.endsWith('/harness')).at(-1).body.all_agents,true);
 await page.locator('#harnessclose').click();
 await page.locator('#projectnav a').first().click();
 await page.locator('#agents [data-agent="dev"]').click();
 await page.locator('#dlgmodel').selectOption('pi-clm:fixture/model');
 assert.equal(await page.locator('#dlgeffort option').count(),2);
 await page.locator('#dlgapply').click();
 await page.waitForTimeout(100);
 assert.equal(writes.find(w=>w.path.endsWith('/agents/dev/model')).body.model,'pi-clm:fixture/model');
 await page.locator('#dlgclose').click();
 await page.setViewportSize({width:390,height:844});
 assert.equal(await page.locator('#harnessbtn').count(),1);
 assert.deepEqual(errors,[]);
 console.log('Harness browser checks passed: new project, existing project defaults/all agents, namespaced Pi models, reasoning options and proxy routing.');
}finally{await browser.close();}
