// Full app navigation, task editing and project tabs through a proxy prefix.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {execFileSync} from 'node:child_process';
import {createRequire} from 'node:module';
import {fileURLToPath} from 'node:url';
const require = createRequire(import.meta.url);
const {chromium} = require(process.env.HUNTUN_PLAYWRIGHT || 'playwright');
const root = fileURLToPath(new URL('../../', import.meta.url));
const source = readFileSync(new URL('../../huntun/office.js', import.meta.url), 'utf8');
const analytics = readFileSync(new URL('../../huntun/performance.js', import.meta.url), 'utf8');
const analyticsCSS = readFileSync(new URL('../../huntun/performance.css', import.meta.url), 'utf8');
const html = readFileSync(new URL('../../huntun/app.html', import.meta.url), 'utf8').replaceAll('"/api/', '"/huntun/api/').replaceAll("'/api/", "'/huntun/api/");
const office = JSON.parse(execFileSync(root + '.venv/bin/python', ['-c', `
import json,tempfile
from pathlib import Path
from huntun.office import OfficeRuntime
from huntun.store import Store
from tests.test_office_state import state
with tempfile.TemporaryDirectory() as d:
 s=Store(Path(d)/'board.db'); r=OfficeRuntime(s,state(),seed=1)
 print(json.dumps(r.view())); r.close(); s.close()
`], {cwd:root, encoding:'utf8'}));
const projects = ['aaaaaaaaaa','bbbbbbbbbb','cccccccccc'].map((id,i) => ({id, name:['Alpha','Beta','New project'][i], path:'/fixture/' + id, initialized:i < 2, approved:i < 2, state:i < 2 ? 'ready' : 'new'}));
const boards = Object.fromEntries(projects.slice(0,2).map(w => [w.id, {
  workspace:w, approved:true, goal_confirmed:true, goal:'Ship ' + w.name, running:false, backend:'codex',
  definition_of_done:'Ship ' + w.name, totals:{tokens:200,cost_usd:12.34,usage_cost:{kinds:['legacy','untracked'],complete:false}}, limits:{}, max_agents:0, attention_count:0,
  models:[], agents:[{name:'master',role:'master',title:'Master',brief:'Lead',status:'active',live:{status:'idle'},info:{backend:'codex',cycles:1,usage:{input:10,output:20,cache_read:30,cache_write:40,cost_usd:12.34},usage_cost:{kinds:['legacy','untracked'],complete:false}}}, {name:'dev',role:'backend',title:'Developer',brief:'Build',status:'active',live:{status:'idle'},info:{backend:'codex',model:'gpt-fixture',cycles:1,effort:'high',usage:{input:10,output:20,cache_read:30,cache_write:40,cost_usd:0},usage_cost:{kinds:['untracked'],complete:false}}}],
  tasks:[{id:1,title:'Ship ' + w.name,acceptance:'App runs',description:'',owner:'',status:'backlog',source_key:'dod:fixture',parent_id:null,evidence:''}],
  threads:[], events:[], office
}]));
const browser = await chromium.launch({headless:true, ...(process.env.HUNTUN_CHROMIUM ? {executablePath:process.env.HUNTUN_CHROMIUM} : {})});
const errors = [], writes = [];
const attentionReplies = [];
const attentionRows = [{id:1,agent:'dev',thread_id:99,comment_id:null,title:'Choose a hostname',
  text:'@human Please choose localhost or example.org for local testing. I recommend localhost. ' + 'Relevant background. '.repeat(70) + 'FINAL ASK: approve localhost?',created_at:new Date().toISOString()}];
let stateOffline=false;
let tokenScale=1;
let performanceEmpty=false,reviewFailure=false,reviewDelay=0;
try {
  const page = await browser.newPage({viewport:{width:1440,height:1000}});
  page.on('pageerror', e => errors.push(e.message));
  await page.route('**/*', async route => {
    const req = route.request(), url = new URL(req.url());
    if (url.host !== 'huntun.test') return route.abort();
    if (url.pathname === '/huntun/') return route.fulfill({contentType:'text/html', body:html});
    if (url.pathname === '/huntun/performance.js') return route.fulfill({contentType:'text/javascript',body:analytics});
    if (url.pathname === '/huntun/performance.css') return route.fulfill({contentType:'text/css',body:analyticsCSS});
    if (url.pathname === '/huntun/office.js') return route.fulfill({contentType:'text/javascript', body:source});
    const path = url.pathname.replace('/huntun', '');
    let result;
    if (path === '/api/workspaces') result = {workspaces:projects, home:'/fixture'};
    else if (path === '/api/fs') result = {path:'/fixture',exists:true,dirs:[],files:0,home:'/fixture'};
    else if (path === '/api/models') result = {models:[],backends:{}};
    else if (path.startsWith('/api/workspaces/')) result = projects.find(w => path.endsWith(w.id));
    else {
      const m = path.match(/^\/api\/w\/([^/]+)(\/.*)$/), board = m && boards[m[1]], sub = m?.[2];
      if (board && sub === '/state') {
        if(stateOffline)return route.fulfill({status:503,json:{error:'State temporarily unavailable'}});
        result = board;
      }
      else if (board && sub === '/agents/dev/activity') result = {agent:board.agents[1].info,live:{status:'idle'},entries:[],cursor:0,journal:[],notes:''};
      else if (board && sub === '/attention') result = {items:attentionRows,pending_ids:attentionRows.map(it=>it.id),count:attentionRows.length};
      else if (board && sub === '/attention/1/reply') {
        attentionReplies.push({project:m[1],body:req.postDataJSON().body});
        attentionRows.length=0;board.attention_count=0;
        result={id:1,thread_id:99,author:'human',body:'@dev '+attentionReplies.at(-1).body};
      }
      else if (board && sub === '/performance') {
        if(reviewDelay)await new Promise(resolve=>setTimeout(resolve,reviewDelay));
        if(reviewFailure)return route.fulfill({status:503,json:{error:'Performance data temporarily unavailable'}});
        const n = m[1] === 'aaaaaaaaaa' ? 321 : 654, range = url.searchParams.get('range'), bin=url.searchParams.get('bin') || 'auto';
        const seconds={'1m':60,'5m':300,'15m':900,'30m':1800,'1h':3600,'3h':10800,'6h':21600,'12h':43200,'1d':86400,'1w':604800,'30d':2592000};
        const widths={'1h':60,'6h':300,'24h':3600,'7d':21600,'30d':86400,'all':86400},durations={'1h':3600,'6h':21600,'24h':86400,'7d':604800,'30d':2592000,'all':604800};
        const width=seconds[bin] || widths[range],end=Date.UTC(2026,9,2,12,30,1),start=end-durations[range]*1000;
        const left=Math.floor(start/(width*1000))*width*1000,count=Math.ceil((end-left)/(width*1000));
        const buckets=Array.from({length:count},(_,i)=>new Date(left+i*width*1000).toISOString());
        const coverage=buckets.map(at=>Math.max(0,(Math.min(end,Date.parse(at)+width*1000)-Math.max(start,Date.parse(at)))/1000));
        const j=Math.floor((Date.UTC(2026,9,2,11,5)-left)/(width*1000));
        const agent=(name,title,tokens,messages,commits)=>{if(performanceEmpty)tokens=messages=commits=0;const series=amount=>Array.from({length:count},(_,i)=>i===j?amount:0);return {name,title,role:'backend',retired:false,tokens:series(tokens),messages:series(messages),commits:series(commits),totals:{tokens,messages,commits,threads:messages?2:0,replies:Math.max(0,messages-2),received:messages?3:0}};};
        result={range,bin,bucket_seconds:width,buckets,window_start:new Date(start).toISOString(),window_end:new Date(end).toISOString(),bucket_coverage_seconds:coverage,partial_buckets:coverage.map(v=>v<width),updated_at:new Date(end-1).toISOString(),history_import_pending:false,max_bins:512,
          bin_options:[{id:'auto',seconds:widths[range],available:true},...Object.entries(seconds).map(([id,seconds])=>({id,seconds,available:Math.ceil(durations[range]/seconds)+1<=512}))],
          definitions:{tokens:'Recorded tokens',messages:'Actual board posts',commits:'Distinct Git SHAs',history:'Imported history'},
          agents:[agent('dev',board.workspace.name+' Developer',n*tokenScale,6,1),agent('qa',board.workspace.name+' QA',120*tokenScale,3,2),...Array.from({length:18},(_,i)=>({...agent('historical-'+i,'Historical agent '+i,0,0,0),retired:true}))]};
      }
      else if (board && (sub === '/office' || sub === '/office/theme')) result = {...office,theme_initialized:true};
      else if (board && sub.startsWith('/tasks') && req.method() === 'POST') {
        const body = req.postDataJSON(); writes.push({project:m[1], body});
        const id = Number(sub.split('/')[2]);
        if (id) {
          const task = board.tasks.find(t => t.id === id);
          if (body.status === 'done' && !body.evidence?.trim()) return route.fulfill({status:400, json:{error:'Completion evidence is required'}});
          Object.assign(task,body); result = task;
        } else {
          result = {id:board.tasks.length+1, status:'backlog', source_key:null, evidence:'', thread_id:99, ...body};
          board.tasks.push(result);
        }
      } else if (board && sub.startsWith('/threads/')) result = {thread:{id:99,author:'master',title:'API conversation',body:'@dev Build the API',created_at:new Date().toISOString()},comments:[],first_id:0,last_id:0,has_more:false};
    }
    if (!result) { errors.push('Unexpected request: ' + path); return route.fulfill({status:404,json:{error:'Missing fixture'}}); }
    return route.fulfill({json:result});
  });
  await page.goto('http://huntun.test/huntun/#/w/aaaaaaaaaa/tasks');
  await page.locator('#newtask').waitFor();
  assert.equal(await page.locator('#projecttabs a').count(),3);
  assert.equal(await page.locator('#projecttabs [aria-current="page"]').textContent(),'Alpha');
  assert.equal(await page.locator('.taskcolumn').count(),3);
  // Needs You remains interactive during the full app's board refresh, through the proxy prefix.
  await page.locator('#needs').click();
  await page.locator('#attreply-1').fill('Approved, use localhost.');
  assert.match(await page.locator('.att-request').innerText(), /FINAL ASK/);
  await page.evaluate(()=>boardRefresh());
  assert.equal(await page.locator('#attreply-1').inputValue(),'Approved, use localhost.');
  assert.equal(await page.evaluate(()=>document.activeElement.id),'attreply-1');
  assert(!await page.locator('body').innerText().then(text=>text.includes('drawAttention is not defined')));
  await page.locator('#lang').selectOption('zh-CN');
  await page.waitForFunction(()=>document.querySelector('.attsend').textContent==='发送回复');
  await page.locator('.attsend').click();
  await page.waitForFunction(()=>document.querySelector('.att-empty'));
  assert.deepEqual(attentionReplies,[{project:'aaaaaaaaaa',body:'Approved, use localhost.'}]);
  await page.locator('#attclose').click();
  await page.locator('#lang').selectOption('en');
  assert((await page.locator('header').textContent()).includes('$12.34 · Partial cost'), 'mixed legacy/subscription totals are partial');
  await page.evaluate(()=>openAgent('aaaaaaaaaa','dev'));
  await page.locator('#tiles .tile').filter({hasText:'Cache writes'}).waitFor();
  assert.equal(await page.locator('#tiles .tile').filter({hasText:/^Tokens/}).locator('b').textContent(),'100');
  assert.equal(await page.locator('#tiles .tile').filter({hasText:/^Cache writes/}).locator('b').textContent(),'40');
  assert.equal(await page.locator('#tiles .tile').filter({hasText:/^Cost/}).locator('b').textContent(),'Untracked');
  assert((await page.evaluate(()=>usageCost(0,{kinds:['local'],complete:true}))).includes('$0.00 · Local'));
  assert((await page.evaluate(()=>usageCost(2,{kinds:['estimated'],complete:true}))).includes('~$2.00'));
  await page.locator('#dlgclose').click();
  await page.locator('#newtask').click();
  await page.locator('#tasktitle').fill('API endpoint');
  await page.locator('#taskacceptance').fill('GET returns 200');
  await page.locator('#taskowner').selectOption('dev');
  await page.locator('#taskparent').selectOption('1');
  await page.locator('#taskform button').click();
  await page.locator('[data-task="2"]').waitFor();
  assert.equal(boards.aaaaaaaaaa.tasks[1].status,'backlog');
  assert.equal(boards.aaaaaaaaaa.tasks[1].parent_id,1);
  await page.locator('[data-task="2"]').dragTo(page.locator('.taskcolumn[data-status="in_process"]'));
  assert.equal(await page.locator('#taskstatus').inputValue(),'in_process');
  await page.locator('#taskform button').click();
  await page.locator('.taskcolumn[data-status="in_process"] [data-task="2"]').waitFor();
  await page.locator('[data-edit="2"]').click();
  await page.locator('#taskstatus').selectOption('done');
  await page.locator('#taskform button').click();
  await page.locator('#taskerror').filter({hasText:'Completion evidence'}).waitFor();
  await page.locator('#taskevidence').fill('abc123; 12 tests passed');
  await page.locator('#taskform button').click();
  await page.locator('.taskcolumn[data-status="done"] [data-task="2"]').waitFor();
  await page.reload();
  await page.locator('.taskcolumn[data-status="done"] [data-task="2"]').waitFor();
  await page.locator('#projecttabs a').filter({hasText:'Beta'}).click();
  await page.locator('.taskcard h3').filter({hasText:'Ship Beta'}).waitFor();
  assert.equal(await page.locator('[data-task="2"]').count(),0,'another project has its own cards');
  assert.equal(await page.locator('#projecttabs [aria-current="page"]').textContent(),'Beta');
  await page.locator('#projectnav [data-project-view="discussion"]').click();
  assert.equal(await page.locator('#main #viewtoggle').count(),0,'view switching belongs to project navigation');
  await page.locator('#projectnav [data-project-view="office"]').click();
  assert.equal(await page.locator('#projectnav [aria-current="page"]').getAttribute('data-project-view'),'office');
  const navGeometry=await page.locator('#projectnav [data-project-view="office"],#projectnav .task-link').evaluateAll(nodes=>nodes.map(n=>n.getBoundingClientRect().y));
  assert(Math.abs(navGeometry[0]-navGeometry[1])<1,'Office and task board share the same navigation row');
  await page.locator('#office').waitFor({state:'visible'});
  assert(await page.locator('#projecttabs').isVisible(),'tabs stay available in Office');
  const canvas = await page.locator('#office').elementHandle();
  await page.locator('#projectnav [data-project-view="office"]').click();
  assert(await canvas.evaluate(n=>n.isConnected),'selecting the active Office entry keeps the renderer');
  stateOffline=true;
  await page.evaluate(()=>boardRefresh());
  assert(await canvas.evaluate(n=>n.isConnected),'a failed state refresh must retain the active renderer');
  stateOffline=false;
  boards.bbbbbbbbbb.threads = Array.from({length:100},(_,i)=>({id:i+1,author:'dev',title:'Thread '+i,
    created_at:'2026-01-01',last_activity:'2026-01-01',comment_count:0,snippet:'Preview',last_comments:[]}));
  const officeRefresh = await page.evaluate(async()=>{
    const agent=document.querySelector('#agents .agent'), event=document.querySelector('#events').firstChild;
    let hiddenChanges=0;
    const observer=new MutationObserver(records=>{hiddenChanges+=records.length;});
    observer.observe(document.querySelector('#threadsbox'),{childList:true,subtree:true});
    await boardRefresh();await new Promise(r=>setTimeout(r,0));observer.disconnect();
    return {hiddenChanges,threads:document.querySelectorAll('#threadsbox .thread').length,
      agentSame:agent===document.querySelector('#agents .agent'),eventSame:event===document.querySelector('#events').firstChild};
  });
  assert.equal(officeRefresh.hiddenChanges,0,'Office refreshes defer the hidden discussion tree');
  assert.equal(officeRefresh.threads,0,'new thread previews are deferred until the discussion becomes visible');
  assert(officeRefresh.agentSame && officeRefresh.eventSame,'unchanged sidebars retain their SVG and text nodes');
  await page.locator('#projectnav [data-project-view="discussion"]').click();
  await page.locator('#threadsbox .thread').last().waitFor({state:'visible'});
  assert.equal(await page.locator('#threadsbox .thread').count(),100,'switching to Threads shows current data immediately');
  boards.bbbbbbbbbb.threads=[];
  await page.locator('#projectnav a').filter({hasText:'Task board'}).click();
  await page.locator('#newtask').waitFor();
  assert(await page.locator('#projectnav .task-link').isVisible(),'task board is a prominent project entry');
  assert.equal(await page.locator('#projectnav .project-name strong').textContent(),'Beta');
  await page.locator('#projectnav [data-project-view="office"]').click();
  await page.locator('#office').waitFor({state:'visible'});
  assert.equal(new URL(page.url()).hash,'#/w/bbbbbbbbbb','Office is reachable directly from the task board');
  await page.reload();
  await page.locator('#office').waitFor({state:'visible'});
  assert.equal(await page.locator('#projectnav [aria-current="page"]').getAttribute('data-project-view'),'office','reload preserves the chosen project view');
  await page.locator('#projectnav .task-link').click();
  await page.locator('#newtask').waitFor();
  await page.locator('#projectnav .review-link').click();
  await page.locator('.review-cell').first().waitFor();
  await page.locator('#projectnav [data-project-view="office"]').click();
  await page.locator('#office').waitFor({state:'visible'});
  await page.locator('#projectnav [data-project-view="discussion"]').click();
  await page.locator('#threadsbox').waitFor({state:'visible'});
  assert.equal(await page.locator('#projectnav [aria-current="page"]').getAttribute('data-project-view'),'discussion','Discussion can be selected explicitly after leaving Performance Review');
  await page.locator('#projectnav .review-link').click();
  await page.locator('.review-cell').first().waitFor();
  assert.equal(await page.locator('.review-stat').count(),3,'all metrics are visible simultaneously');
  assert.equal(await page.locator('.review-cell').first().locator('[data-metric]').count(),3,'every agent bin shows all three metrics');
  assert.equal(await page.locator('[data-review-total="tokens"]').textContent(),'774');
  assert.equal(await page.locator('[data-review-total="messages"]').textContent(),'9');
  assert((await page.locator('#reviewdetail').textContent()).includes('774'),'initial selection finds the latest bin with activity');
  assert.equal(await page.locator('[data-review-bin][tabindex="0"]').count(),1,'one keyboard entry into overview bins');
  assert.equal(await page.locator('.review-cell[tabindex="0"]').count(),1,'one keyboard entry into the agent table');
  await page.locator('#reviewactive').check();
  assert.equal(await page.locator('.review-cell').count(),2*29,'activity filter removes empty agent rows across every bin');
  assert.equal(await page.locator('[data-review-total="tokens"]').textContent(),'774','activity filter preserves metric totals');
  await page.locator('#reviewactive').uncheck();
  assert.equal(await page.locator('.review-cell').count(),20*29,'all agents and all bins are rendered together');
  assert.equal(await page.locator('#reviewagentnext,#reviewagentprev,#reviewbinprev,#reviewbinnext').count(),0,'matrix has no pagination');
  const geometry=await page.locator('.review-cell').first().evaluate(n=>({width:n.closest('td').getBoundingClientRect().width,height:n.getBoundingClientRect().height}));
  assert(geometry.width>=54 && geometry.width<=58,'time columns are about one third of their former width');
  assert(geometry.height>=78 && geometry.height<=82,'cell height is two thirds of its former height');
  assert.equal(await page.locator('.review-histogram').count(),1,'overview merges all metrics into a single chart');
  assert.equal(await page.locator('.review-histogram .review-bar').count(),3*29,'every time bin contains three adjacent metric bars');
  const groupedBars=await page.locator('[data-review-bar-bin="27"]').evaluateAll(nodes=>nodes.map(n=>({key:n.dataset.reviewSeries,x:Number(n.getAttribute('x')),width:Number(n.getAttribute('width')),baseline:Number(n.getAttribute('y'))+Number(n.getAttribute('height')),height:Number(n.getAttribute('height'))})));
  assert.deepEqual(groupedBars.map(b=>b.key),['tokens','messages','commits']);
  assert(groupedBars.every((b,i)=>!i || b.x>groupedBars[i-1].x+groupedBars[i-1].width),'metric bars are adjacent without overlapping');
  assert(groupedBars.every(b=>Math.abs(b.baseline-74)<.002),'all metrics share one baseline');
  assert(groupedBars.every(b=>b.height>0 && b.height<=66),'separate scales keep all nonzero metrics visible');
  if(process.env.HUNTUN_REVIEW_SCREENSHOT)await page.screenshot({path:process.env.HUNTUN_REVIEW_SCREENSHOT+'.overview.png'});
  const overviewBox=await page.locator('.review-overview').boundingBox();
  assert(overviewBox.height<300,'desktop overview has a compact fixed height: '+JSON.stringify(overviewBox));
  const firstBin=page.locator('[data-review-bin]').first();
  await firstBin.hover();await page.locator('#reviewtooltip').waitFor({state:'visible'});
  assert.equal(await page.locator('#reviewtooltip>div').count(),3);
  await page.locator('[data-review-bin]').nth(27).click();
  const cell=page.locator('[data-review-agent="dev"][data-review-bucket="27"]');
  await cell.click();assert((await page.locator('#reviewdetail').textContent()).includes('654'));
  const bars=await cell.locator('.review-bin-metric').evaluateAll(nodes=>nodes.map(n=>{
    const bar=n.querySelector('i>b').getBoundingClientRect(),plot=n.querySelector('i').getBoundingClientRect();
    return {height:bar.height,width:bar.width,bottom:bar.bottom,baseline:plot.bottom,label:n.querySelector('em').textContent,value:n.querySelector('strong').textContent};
  }));
  assert.deepEqual(bars.map(b=>b.label),['T','M','C']);
  assert(bars[0].height>bars[0].width,'token bars rise vertically');
  assert(Math.abs(bars[0].bottom-bars[1].bottom)<1 && Math.abs(bars[0].bottom-bars[2].bottom)<1,'all three metrics share a baseline in each bin');
  const qaMessageHeight=await page.locator('[data-review-agent="qa"][data-review-bucket="27"] [data-metric="messages"] i>b').evaluate(n=>n.getBoundingClientRect().height);
  assert(Math.abs(qaMessageHeight/bars[1].height-.5)<.02,'message heights share the same scale across agents');
  const inspector=await page.locator('#reviewdetail').boundingBox(),matrix=await page.locator('.review-matrix').boundingBox();
  assert(inspector.y<matrix.y && inspector.width>matrix.width*.9,'inspector sits above full-width agent bins');
  await cell.focus();await page.keyboard.press('ArrowDown');
  assert.equal(await page.evaluate(()=>document.activeElement.dataset.reviewAgent),'qa','up/down keys compare agents');
  await page.keyboard.press('ArrowUp');await page.keyboard.press('Home');
  assert.equal(await page.evaluate(()=>document.activeElement.dataset.reviewBucket),'0','Home reveals the first time bin');
  await page.keyboard.press('End');await page.keyboard.press('ArrowLeft');
  assert.equal(await page.evaluate(()=>document.activeElement.dataset.reviewBucket),'27');
  const middleCell=page.locator('[data-review-bucket="27"][data-review-agent]').nth(9);
  const middleAgent=await middleCell.getAttribute('data-review-agent');
  await middleCell.focus();await page.keyboard.press('ArrowDown');
  assert.notEqual(await page.evaluate(()=>document.activeElement.dataset.reviewAgent),middleAgent,'arrow navigation reaches all agents without pagination');
  await page.keyboard.press('ArrowUp');assert.equal(await page.evaluate(()=>document.activeElement.dataset.reviewAgent),middleAgent);
  await cell.click();
  await page.locator('#reviewdetailprev').click();await page.locator('#reviewdetailnext').click();
  assert((await page.locator('#reviewdetail').textContent()).includes('654'));
  const pageScroll=await page.locator('#view').evaluate(n=>n.scrollTop);
  await page.locator('[data-review-bin="27"]').first().evaluate(n=>n.dispatchEvent(new MouseEvent('click',{bubbles:true})));
  assert.equal(await page.locator('#view').evaluate(n=>n.scrollTop),pageScroll,'chart selection moves only the table viewport');
  await cell.click();
  if(process.env.HUNTUN_REVIEW_SCREENSHOT) {await cell.scrollIntoViewIfNeeded();await page.screenshot({path:process.env.HUNTUN_REVIEW_SCREENSHOT+'.bins.png'});}

  assert((await page.locator('#reviewdetail').textContent()).includes('1 / hour'));
  const rebin=page.waitForResponse(r=>r.url().includes('&bin=1h'));
  await page.locator('#reviewbin').selectOption('1h');await rebin;
  assert.equal(await page.locator('[data-review-total="tokens"]').textContent(),'774','bin size does not change totals');
  assert.equal(await page.locator('[data-review-bin]').count(),169,'hourly bins across seven days');
  assert.equal(await page.locator('#reviewbin option[value="15m"]').evaluate(el=>el.disabled),true,'excessively fine bins are visibly unavailable');
  assert.equal(await page.locator('.review-cell').count(),20*169,'a long series renders every agent and every time bin');
  assert.equal(await page.locator('#reviewmatrix table').count(),1,'all time bins share one continuous table');
  assert.equal(await page.locator('.review-matrix-block').count(),0,'time bins are never split into blocks');
  assert.equal(await page.locator('#reviewmatrix tbody tr').count(),20,'each agent occupies exactly one row');
  assert(await page.locator('.review-matrix').evaluate(n=>n.scrollHeight===n.clientHeight && n.scrollWidth>n.clientWidth),'only the time axis scrolls inside the matrix');
  await page.locator('#reviewagent').selectOption('dev');
  assert.equal(await page.locator('[data-review-total="tokens"]').textContent(),'654','scope updates the overview');
  assert.equal(await page.locator('.review-cell').count(),169);
  assert.equal(await page.locator('#reviewdetail h3').textContent(),'@dev','single-agent scope identifies the selected agent');
  assert.equal(await page.locator('#reviewclearselection').count(),0,'single-agent scope avoids an ineffective compare-all action');
  await page.locator('#reviewzone').selectOption('utc');assert((await page.locator('#reviewwindow').textContent()).includes('UTC'));
  await page.locator('[data-review-bin]').last().focus();await page.keyboard.press('ArrowLeft');
  assert((await page.locator('#reviewdetail').textContent()).includes('SELECTED TIME BIN'));
  const downloadEvent=page.waitForEvent('download');await page.locator('#reviewexport').click();const download=await downloadEvent;
  assert(download.suggestedFilename().includes('7d-1h.csv'));
  const stream=await download.createReadStream();let exported='';for await(const chunk of stream)exported+=chunk.toString();
  assert(exported.includes('"tokens","messages","commits"'));assert(exported.includes('"dev"'));assert(!exported.includes('"qa"'));
  const changedRange=page.waitForResponse(r=>r.url().includes('/performance?range=24h'));
  await page.locator('#reviewrange').selectOption('24h');await changedRange;
  const finer=page.waitForResponse(r=>r.url().includes('&bin=5m'));await page.locator('#reviewbin').selectOption('5m');await finer;
  assert.equal(await page.locator('[data-review-bin]').count(),289,'five-minute bins across 24 hours');
  await page.reload();await page.locator('.review-cell').first().waitFor();
  assert.equal(await page.locator('#reviewbin').inputValue(),'5m','granularity survives reload');
  assert.equal(await page.locator('#reviewrange').inputValue(),'24h');
  assert(await page.locator('#projectnav [aria-current="page"]').getAttribute('href') === '#/w/bbbbbbbbbb/performance');
  await page.locator('#projecttabs a').filter({hasText:'Alpha'}).click();
  await page.locator('.agent-label small').filter({hasText:'Alpha Developer'}).first().waitFor();
  assert.equal(new URL(page.url()).hash,'#/w/aaaaaaaaaa/performance','project switching keeps the review section');
  assert.equal(await page.locator('[data-review-total="tokens"]').textContent(),'441','another project has its own series');
  await page.locator('#lang').selectOption('zh-CN');
  assert.equal(await page.locator('.review-heading h2').textContent(),'性能评估');
  await page.locator('#lang').selectOption('en');assert.equal(await page.locator('.review-heading h2').textContent(),'Performance Review');
  await page.setViewportSize({width:650,height:850});
  assert(await page.locator('#projectnav .task-link').isVisible());
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth > innerWidth),false,'charts and complete matrix fit the project view');
  await page.locator('.review-matrix').evaluate(el=>{el.scrollLeft=350;});
  const reviewScroll=await page.locator('.review-matrix').evaluate(el=>el.scrollLeft);
  assert(reviewScroll>0,'the timeline can scroll horizontally');
  const fixedLabels=await page.locator('#reviewmatrix').evaluate(matrix=>{
    const left=matrix.getBoundingClientRect().left+matrix.clientLeft;
    return [...matrix.querySelectorAll('.agent-label')].every(label=>Math.abs(label.getBoundingClientRect().left-left)<1);
  });
  assert(fixedLabels,'agent labels stay fixed while time columns move');
  const refreshedReview=page.waitForResponse(r=>r.url().includes('/performance?range='));
  await page.locator('#reviewrefresh').click();await refreshedReview;await page.waitForTimeout(100);
  assert.equal(await page.locator('.review-matrix').evaluate(el=>el.scrollLeft),reviewScroll,'refresh preserves the horizontal time position');
  await page.locator('#performancebox').evaluate(box=>{
    box.querySelector('.review-method').open=true;
    box.querySelector('#reviewparticipants').open=true;
    box.closest('section').scrollTop=800;
    document.scrollingElement.scrollTop=800;
    box.querySelector('.review-matrix').scrollTop=180;
    box.querySelector('.review-plot-wrap').scrollLeft=50;
  });
  const viewport=()=>page.evaluate(()=>{
    const box=document.querySelector('#performancebox');
    return {page:box.closest('section').scrollTop,document:document.scrollingElement.scrollTop,
      matrixLeft:box.querySelector('.review-matrix').scrollLeft,matrixTop:box.querySelector('.review-matrix').scrollTop,
      plotLeft:box.querySelector('.review-plot-wrap').scrollLeft,method:box.querySelector('.review-method').open,participants:box.querySelector('#reviewparticipants').open,focus:document.activeElement.id};
  });
  await page.locator('#reviewactive').evaluate(n=>n.focus({preventScroll:true}));
  const beforeRefresh=await viewport();assert(beforeRefresh.page>0 || beforeRefresh.document>0,'review is read below the top');
  const unchangedCell=await page.locator('.review-cell').first().elementHandle();
  const backgroundRefresh=page.waitForResponse(r=>r.url().includes('/performance?range='));
  await backgroundRefresh;await page.waitForTimeout(150);
  assert.deepEqual(await viewport(),beforeRefresh,'automatic refresh preserves the complete reading viewport');
  assert(await unchangedCell.evaluate(n=>n.isConnected),'unchanged cells are reused across background refreshes');
  await page.setViewportSize({width:1440,height:1000});
  await page.locator('#performancebox').evaluate(box=>{box.closest('section').scrollTop=800;});
  const desktopViewport=await viewport();assert(desktopViewport.page>0,'desktop uses the project view scroll container');
  await page.evaluate(()=>drawPerformance(boardId,true));await page.waitForTimeout(150);
  assert.deepEqual(await viewport(),desktopViewport,'desktop refresh preserves the project view and nested scroll positions');
  for(const [scale,expected,total] of [[10,'3.2K','4.4K'],[10000,'3.2M','4.4M'],[10000000,'3.2B','4.4B']]) {
    tokenScale=scale;
    await page.evaluate(()=>drawPerformance(boardId,true));
    const largeCell=page.locator('[data-review-agent="dev"][data-review-bucket="271"]');
    assert.equal(await largeCell.locator('[data-metric="tokens"] strong').textContent(),expected);
    assert((await largeCell.getAttribute('title')).includes((321*scale).toLocaleString('en')),'hover retains exact tokens');
    assert((await page.locator('.review-agent-totals').first().textContent()).includes(expected),'agent totals use compact tokens');
    assert.equal(await page.locator('[data-review-total="tokens"]').textContent(),total,'summary uses compact tokens');
    await largeCell.click();
    assert.equal(await page.locator('.review-detail-values>div').first().locator('strong').textContent(),expected,'selected agent detail uses compact tokens');
    assert.equal(await page.locator('.review-detail-values>div').first().locator('strong').getAttribute('title'),(321*scale).toLocaleString('en'));
    assert((await page.locator('.review-contributors').textContent()).includes(expected),'contributor labels use compact tokens');
  }
  await page.locator('#lang').selectOption('zh-CN');
  assert.equal(await page.locator('[data-review-agent="dev"][data-review-bucket="271"] [data-metric="tokens"] strong').textContent(),'3.2B','token units stay K/M/B in Chinese');
  await page.locator('#lang').selectOption('en');
  const exactDownload=page.waitForEvent('download');await page.locator('#reviewexport').click();
  const exactStream=await (await exactDownload).createReadStream();let exactCSV='';for await(const chunk of exactStream)exactCSV+=chunk.toString();
  assert(exactCSV.includes('"3210000000"'),'CSV retains exact token counts');
  performanceEmpty=true;await page.evaluate(()=>drawPerformance(boardId,true));
  assert.equal(await page.locator('[data-review-total="tokens"]').textContent(),'0');
  assert(await page.locator('.review-no-activity').isVisible(),'empty ranges have a clear chart state');
  await page.locator('#reviewactive').check();
  assert.equal(await page.locator('.review-cell').count(),0);
  await page.locator('#reviewshowall').click();
  assert.equal(await page.locator('.review-cell').count(),20*289,'empty activity filter can be cleared without hiding agents or bins');
  performanceEmpty=false;await page.evaluate(()=>drawPerformance(boardId,true));
  const retainedTotal=await page.locator('[data-review-total="tokens"]').textContent();
  reviewFailure=true;await page.evaluate(()=>drawPerformance(boardId,true));
  assert.equal(await page.locator('[data-review-total="tokens"]').textContent(),retainedTotal,'failed refresh retains the last good data');
  assert.equal(await page.locator('#reviewupdated').getAttribute('data-state'),'error');
  assert((await page.locator('#reviewerror').textContent()).includes('temporarily unavailable'));
  reviewFailure=false;reviewDelay=300;
  await page.evaluate(()=>{void drawPerformance(boardId,true);});
  assert.equal(await page.locator('#performancebox').getAttribute('aria-busy'),'true');
  assert.equal(await page.locator('#reviewupdated').getAttribute('data-state'),'loading');
  await page.waitForFunction(()=>document.querySelector('#performancebox').getAttribute('aria-busy')===null);
  reviewDelay=0;
  assert.equal(await page.locator('#reviewupdated').getAttribute('data-state'),'ready');
  await page.setViewportSize({width:390,height:850});
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false,'phone has no page-level horizontal overflow');
  await page.locator('[data-review-bin="271"]').first().evaluate(n=>n.dispatchEvent(new MouseEvent('click',{bubbles:true})));
  await page.locator('.review-inspector').scrollIntoViewIfNeeded();
  const phoneInspector=await page.locator('#reviewdetail').boundingBox(),phoneMatrix=await page.locator('.review-matrix').boundingBox();
  assert(phoneInspector.width<=350 && phoneInspector.y<phoneMatrix.y,'phone inspector appears above the continuous agent bins');
  const mobileHeader=await page.locator('header').boundingBox(),mobileNav=await page.locator('#projectnav').boundingBox();
  assert.equal(mobileHeader.y,0,'header remains visible while reading a long phone page');
  assert.equal(mobileNav.y+mobileNav.height,192,'phone navigation uses the actual two-row header height');
  const labelWidth=await page.locator('.review-matrix thead .agent-label').first().evaluate(n=>n.getBoundingClientRect().width);
  const binWidth=await page.locator('.review-matrix tbody td').first().evaluate(n=>n.getBoundingClientRect().width);
  assert(phoneMatrix.width-labelWidth>=binWidth,'compact time bins fit beside the fixed phone agent label');
  if(process.env.HUNTUN_REVIEW_SCREENSHOT)await page.screenshot({path:process.env.HUNTUN_REVIEW_SCREENSHOT+'.phone.png'});
  await page.locator('.review-matrix').evaluate(n=>{const nav=document.querySelector('#projectnav').getBoundingClientRect();document.scrollingElement.scrollTop+=n.getBoundingClientRect().top-nav.bottom-8;});
  await page.locator('#reviewshowdetail').click();
  const restoredInspector=await page.locator('#reviewdetail').boundingBox();
  assert(restoredInspector.y>=192 && restoredInspector.y<215,'phone Details control reveals the inspector below sticky navigation');
  assert.equal(await page.evaluate(()=>document.activeElement.parentElement.id),'reviewdetail','details action moves keyboard focus into the inspector');
  const detailsScroll=await page.evaluate(()=>document.scrollingElement.scrollTop);
  await page.evaluate(()=>drawPerformance(boardId,true));
  assert.equal(await page.evaluate(()=>document.scrollingElement.scrollTop),detailsScroll,'refresh preserves the details reading position');
  assert(await page.evaluate(()=>document.activeElement.matches('#reviewdetail h3')),'refresh preserves inspector keyboard focus');
  await page.setViewportSize({width:1440,height:1000});
  if(process.env.HUNTUN_REVIEW_SCREENSHOT)await page.locator('#reviewactive').check();await page.locator('.review-heading').scrollIntoViewIfNeeded();await page.screenshot({path:process.env.HUNTUN_REVIEW_SCREENSHOT,fullPage:false});
  await page.locator('#projecttabs a').filter({hasText:'New project'}).click();
  await page.locator('#setupform').waitFor();
  assert(await page.locator('#projecttabs').isVisible(),'tabs stay available in setup');
  await page.locator('header h1 a').click();
  await page.locator('#openform').waitFor();
  assert.equal(await page.locator('#projecttabs').isVisible(),false,'home keeps its project picker');
  assert.equal(await page.locator('#projectnav').isVisible(),false,'project navigation does not leak onto home');
  assert.equal(errors.length,0,errors.join('\n'));
  assert(writes.every(w => w.project === 'aaaaaaaaaa'));
  console.log('Project browser checks passed: task editing/navigation, a single grouped histogram and vertical agent bins with shared per-metric scales, compact desktop/phone inspectors, activity filter and latest-active selection, roving keyboard navigation, a continuous horizontal time axis with fixed agent labels and no pagination, compact geometry and unchanged-cell reuse, range/scope/timezone/CSV/localization, automatic scroll/focus retention, K/M/B tokens, loading/empty/error recovery, 390px navigation and bin fit, Office/setup/project switching and proxy paths.');
} finally { await browser.close(); }
