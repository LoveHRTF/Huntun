// Project analytics. All series are aggregated by the server; SVG only presents them.
function makePerformanceReview(options) {
  const METRICS = [{key:'tokens',label:'Tokens',short:'T',color:'#56d4ed',note:'Reported token consumption'},
    {key:'messages',label:'Messages',short:'M',color:'#a99bff',note:'Actual conversations'},
    {key:'commits',label:'Commits',short:'C',color:'#f4bd62',note:'Distinct Git contributions'}];
  const BINS = {'auto':'Auto','1m':'1 minute','5m':'5 minutes','15m':'15 minutes','30m':'30 minutes',
    '1h':'1 hour','3h':'3 hours','6h':'6 hours','12h':'12 hours','1d':'1 day','1w':'1 week','30d':'30 days'};
  const RANGES = {'1h':'Last hour','6h':'Last 6 hours','24h':'Last 24 hours','7d':'Last 7 days','30d':'Last 30 days','all':'All history'};
  const DURATIONS = {'1h':3600,'6h':21600,'24h':86400,'7d':604800,'30d':2592000};
  const BIN_WIDTH=56;
  const escape = text => String(text ?? '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const t = text => options.translate?.(text) || text;
  const pref={range:'7d',bin:'auto',zone:'local',agent:'all',sort:'tokens',activeOnly:false};
  try {const saved=JSON.parse(localStorage.getItem('huntun.review') || '{}');for(const k of ['range','bin','zone','sort']) if(typeof saved[k]==='string') pref[k]=saved[k];if(typeof saved.activeOnly==='boolean')pref.activeOnly=saved.activeOnly;} catch(e) {}
  if(!RANGES[pref.range])pref.range='7d';if(!BINS[pref.bin])pref.bin='auto';if(!['local','utc'].includes(pref.zone))pref.zone='local';
  if(!['tokens','messages','commits','name'].includes(pref.sort))pref.sort='tokens';
  let request=0, project=null, next=0;
  const $ = (selector,root=document) => root.querySelector(selector);
  const save = () => {try {localStorage.setItem('huntun.review',JSON.stringify(pref));} catch(e) {}};
  const numberFormats=new Map(),dateFormats=new Map();
  // Keep K/M/B units consistent even when the interface uses Chinese or Japanese.
  const number = (v,compact=false) => {
    const locale=compact?'en':options.locale?.() || undefined,key=(locale || 'default')+':'+compact;
    if(!numberFormats.has(key))numberFormats.set(key,new Intl.NumberFormat(locale,compact?{notation:'compact',maximumFractionDigits:1}:{maximumFractionDigits:0}));
    return numberFormats.get(key).format(v);
  };
  const metricNumber = (key,value) => number(value,key==='tokens' || Math.abs(value)>=1000);
  function preserveViewport(box) {
    // Replacing charts temporarily shrinks the page. Keep the reading viewport.
    const parents=new Set();
    for(let node=box.parentElement;node;node=node.parentElement)parents.add(node);
    parents.add(document.scrollingElement);
    const scrolls=[...parents].filter(Boolean).map(node=>({node,left:node.scrollLeft,top:node.scrollTop}));
    const nested=['#reviewmatrix','.review-plot-wrap'].map(selector=>{
      const node=$(selector,box);return node?{selector,left:node.scrollLeft,top:node.scrollTop}:null;
    }).filter(Boolean);
    const expanded=$('.review-method',box)?.open || false;
    return ()=>{
      const method=$('.review-method',box);if(method)method.open=expanded;
      for(const {node,left,top} of scrolls){node.scrollLeft=left;node.scrollTop=top;}
      for(const {selector,left,top} of nested){const node=$(selector,box);if(node){node.scrollLeft=left;node.scrollTop=top;}}
    };
  }
  const datetime = (at,format='full') => {
    const locale=options.locale?.() || undefined,key=(locale || 'default')+':'+pref.zone+':'+format;
    if(!dateFormats.has(key))dateFormats.set(key,new Intl.DateTimeFormat(locale,{
      ...(format==='clock'?{hour:'2-digit',minute:'2-digit',hourCycle:'h23'}:format==='day'?{month:'short',day:'numeric'}:
        {year:'numeric',month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}),
      ...(pref.zone==='utc'?{timeZone:'UTC'}:{})}));
    return dateFormats.get(key).format(new Date(at));
  };
  const timezone = () => pref.zone==='utc'?'UTC':Intl.DateTimeFormat().resolvedOptions().timeZone;
  const unit = seconds => seconds<3600 ? number(seconds/60)+' '+t('min') : seconds<86400 ? number(seconds/3600)+' '+t('hr') : number(seconds/86400)+' '+t('days');
  const bounds = (data,j) => [Math.max(Date.parse(data.buckets[j]),Date.parse(data.window_start)),
    Math.min(Date.parse(data.buckets[j])+data.bucket_seconds*1000,Date.parse(data.window_end))];
  const totals = rows => Object.fromEntries(METRICS.map(m=>[m.key,rows.reduce((sum,a)=>sum+a.totals[m.key],0)]));
  const series = (rows,count) => Object.fromEntries(METRICS.map(m=>[m.key,Array.from({length:count},(_,j)=>rows.reduce((sum,a)=>sum+a[m.key][j],0))]));
  const niceMax = value => {if(value<=1)return 1;const power=10**Math.floor(Math.log10(value)),x=value/power;return (x<=2?2:x<=5?5:10)*power;};
  function build(box,id) {
    box.dataset.reviewProject=id;
    box.innerHTML=`<div class="review-heading"><div><div class="review-eyebrow">${t('PROJECT ANALYTICS')}</div><h2>${t('Performance Review')}</h2><p>${t('Work, communication and delivery — measured over time.')}</p></div><div class="review-actions"><span id="reviewupdated" class="review-live" aria-live="off"></span><button id="reviewexport" class="secondary">${t('Export CSV')}</button><button id="reviewrefresh" class="secondary">${t('Refresh')}</button></div></div>
      <div class="review-controls"><label>${t('Time range')}<select id="reviewrange">${Object.entries(RANGES).map(([k,label])=>`<option value="${k}">${t(label)}</option>`).join('')}</select></label>
      <label>${t('Bin interval')}<select id="reviewbin">${Object.entries(BINS).map(([k,label])=>`<option value="${k}">${t(label)}</option>`).join('')}</select></label>
      <label>${t('Agent scope')}<select id="reviewagent"><option value="all">${t('All agents')}</option></select></label>
      <label>${t('Time zone')}<select id="reviewzone"><option value="local">${t('Local time')}</option><option value="utc">UTC</option></select></label>
      <div class="review-window" id="reviewwindow"></div></div><p id="reviewintervalhint" class="review-control-hint"></p><p id="reviewerror" class="error" role="alert"></p>
      <div id="reviewselectionstatus" class="review-sr-only" role="status"></div><div id="reviewcharts"><div class="review-loading" role="status">${t('Loading performance…')}</div></div>`;
    $('#reviewrange',box).value=pref.range;$('#reviewbin',box).value=pref.bin;$('#reviewzone',box).value=pref.zone;
    $('#reviewrange',box).onchange=e=>{
      pref.range=e.target.value;
      const binOption=box.review?.bin_options?.find(o=>o.id===pref.bin);
      if(pref.range==='all' || (pref.bin!=='auto' && (!binOption || Math.ceil(DURATIONS[pref.range]/binOption.seconds)+1>512))) pref.bin='auto';
      $('#reviewbin',box).value=pref.bin;box.selection=null;save();update(id,true);
    };
    $('#reviewbin',box).onchange=e=>{pref.bin=e.target.value;box.selection=null;save();update(id,true);};
    $('#reviewagent',box).onchange=e=>{pref.agent=e.target.value;box.selection=null;render(box);};
    $('#reviewzone',box).onchange=e=>{pref.zone=e.target.value;save();render(box);};
    $('#reviewrefresh',box).onclick=()=>update(id,true);
    $('#reviewexport',box).onclick=()=>exportCSV(box);
  }
  async function update(id,force=false) {
    const box=$('#performancebox');if(!box || options.currentProject()!==id)return;
    if(project!==id) {project=id;pref.agent='all';next=0;}
    if(box.dataset.reviewProject!==id)build(box,id);
    if(!force && Date.now()<next)return;next=Date.now()+10000;
    const seq=++request,range=pref.range,bin=pref.bin;
    box.setAttribute('aria-busy','true');$('#reviewrefresh',box).disabled=true;
    $('#reviewupdated',box).textContent=t('Updating…');
    $('#reviewupdated',box).dataset.state='loading';
    try {
      // Relative URL also works behind /huntun/ without proxy rewriting of JS.
      const data=await options.api('api/w/'+id+'/performance?range='+range+'&bin='+bin);
      if(seq!==request || options.currentProject()!==id || box!==$('#performancebox'))return;
      box.review=data;$('#reviewerror',box).textContent='';render(box);
    } catch(error) {
      if(seq===request && box===$('#performancebox')) {$('#reviewerror',box).textContent=error.message;$('#reviewupdated',box).textContent=t('Refresh failed');$('#reviewupdated',box).dataset.state='error';next=0;}
    } finally {
      if(seq===request && box===$('#performancebox')) {box.removeAttribute('aria-busy');$('#reviewrefresh',box).disabled=false;}
    }
  }
  function controls(box,data) {
    const bins=data.bin_options.map(o=>`<option value="${o.id}"${o.available?'':' disabled'}>${t(BINS[o.id])}${o.id==='auto'?' · '+unit(o.seconds):''}</option>`).join('');
    const binSelect=$('#reviewbin',box);if(binSelect.dataset.key!==bins){binSelect.innerHTML=bins;binSelect.dataset.key=bins;}binSelect.value=pref.bin;
    const agents='<option value="all">'+t('All agents')+'</option>'+data.agents.map(a=>`<option value="${escape(a.name)}">@${escape(a.name)}${a.retired?' · '+t('Retired'):''}</option>`).join('');
    const select=$('#reviewagent',box);if(select.dataset.key!==agents){select.innerHTML=agents;select.dataset.key=agents;}
    if(pref.agent!=='all' && !data.agents.some(a=>a.name===pref.agent))pref.agent='all';select.value=pref.agent;
    $('#reviewwindow',box).innerHTML=`<strong>${datetime(data.window_start)} — ${datetime(data.window_end)}</strong><span>${escape(timezone())} · ${number(data.buckets.length)} ${t('bins')} · ${unit(data.bucket_seconds)} / ${t('bin')}</span>`;
    $('#reviewupdated',box).textContent=t('Updated')+' '+datetime(data.updated_at,'clock');
    $('#reviewupdated',box).dataset.state='ready';
    $('#reviewintervalhint',box).textContent=data.bin_options.some(o=>!o.available)?t('Choose a shorter time range to enable finer bin intervals.'):'';
    $('#reviewexport',box).disabled=!data.agents.length;
  }
  function overviewWidth(box) {
    return Math.max(220,Math.floor($('#reviewcharts',box).clientWidth-30));
  }
  function histogram(data,values,sum,W) {
    const count=data.buckets.length,H=108,left=38,right=8,plot=W-left-right,cell=plot/count,top=8,bottom=74;
    const scales=Object.fromEntries(METRICS.map(m=>[m.key,niceMax(Math.max(1,...values[m.key]))]));
    // Different units share a single plot. The legend gives each metric's full
    // scale; the common height axis is the percentage of that metric's scale.
    const summary='<div class="review-summary">'+METRICS.map(m=>{
      const peak=Math.max(0,...values[m.key]),peakIndex=values[m.key].indexOf(peak);
      const peakText=peak?`${t('Peak bin')}: ${metricNumber(m.key,peak)} · ${datetime(bounds(data,peakIndex)[0])}`:t('No recorded activity in this bin.');
      return `<div class="review-stat review-trend" style="--metric:${m.color}"><div class="review-stat-label"><span class="review-dot"></span>${m.short} · ${t(m.label)}</div><strong data-review-total="${m.key}" title="${number(sum[m.key])}">${metricNumber(m.key,sum[m.key])}</strong><div class="review-stat-note" title="${escape(peakText)}">${t('Scale')}: 0–${metricNumber(m.key,scales[m.key])}</div></div>`;
    }).join('')+'</div>';
    let svg=`<svg class="review-histogram" viewBox="0 0 ${W} ${H}" role="group" aria-label="${escape(t('Tokens, messages and commits by time bin'))}"><title>${t('Activity over time')} · ${t('Independent scale')}</title>`;
    for(let k=0;k<=2;k++) {const y=bottom-k*(bottom-top)/2;
      svg+=`<line x1="${left}" x2="${W-right}" y1="${y}" y2="${y}" class="review-gridline"/><text x="${left-5}" y="${y+3}" text-anchor="end" class="review-axis">${k*50}%</text>`;}
    data.buckets.forEach((at,j)=>METRICS.forEach((m,i)=>{
      const height=values[m.key][j]/scales[m.key]*(bottom-top),width=cell*.24,x=left+j*cell+cell*(.1+i*.27);
      svg+=`<rect class="review-bar${data.partial_buckets[j]?' partial':''}" data-review-series="${m.key}" data-review-bar-bin="${j}" x="${x.toFixed(3)}" y="${(bottom-height).toFixed(3)}" width="${width.toFixed(3)}" height="${height.toFixed(3)}" rx="${Math.min(2,width/3)}" fill="${m.color}"/>`;
    }));
    const ticks=Math.max(2,Math.min(7,Math.floor(plot/130)+1));
    for(const j of [...new Set(Array.from({length:ticks},(_,i)=>Math.round(i*(count-1)/(ticks-1))))]) {
      const at=bounds(data,j)[0],x=left+(j+.5)*cell,anchor=j===0?'start':j===count-1?'end':'middle';
      svg+=`<text x="${x.toFixed(2)}" y="90" text-anchor="${anchor}" class="review-axis">${datetime(at,data.bucket_seconds<86400?'clock':'day')}</text>`;
      if(data.bucket_seconds<86400)svg+=`<text x="${x.toFixed(2)}" y="104" text-anchor="${anchor}" class="review-axis secondary">${datetime(at,'day')}</text>`;
    }
    svg+=`<rect x="${left}" y="${top}" width="${cell}" height="${bottom-top}" class="review-crosshair" pointer-events="none" hidden/>`;
    data.buckets.forEach((at,j)=>{
      const [from,to]=bounds(data,j),label=datetime(from)+' — '+datetime(to)+' · '+METRICS.map(m=>t(m.label)+': '+number(values[m.key][j])).join(' · ');
      svg+=`<rect data-review-bin="${j}" data-review-at="${at}" x="${(left+j*cell).toFixed(2)}" y="${top}" width="${cell.toFixed(2)}" height="${bottom-top}" fill="transparent" tabindex="-1" role="button" aria-label="${escape(label)}"><title>${escape(label)}</title></rect>`;
    });
    return summary+svg+'</svg>';
  }
  function render(box) {
    const data=box.review;if(!data || box!==$('#performancebox'))return;
    const restoreViewport=preserveViewport(box);controls(box,data);
    const chart=$('#reviewcharts',box);
    const focused=document.activeElement,focusCell=focused?.dataset.reviewAgent != null ? {name:focused.dataset.reviewAgent,at:focused.dataset.reviewAt}:null;
    const focusedBin=focused?.dataset.reviewBin!=null?focused.dataset.reviewAt:null;
    const rows=pref.agent==='all'?data.agents.filter(a=>!pref.activeOnly || METRICS.some(m=>a.totals[m.key])):data.agents.filter(a=>a.name===pref.agent),sum=totals(rows),values=series(rows,data.buckets.length);
    box.filtered=rows;box.series=values;
    let selected=box.selection && data.buckets.indexOf(box.selection.at);
    if(selected==null || selected<0){selected=values.messages.findLastIndex((v,j)=>v || values.tokens[j] || values.commits[j]);if(selected<0)selected=data.buckets.length-1;}
    box.selection={at:data.buckets[selected],agent:box.selection?.agent || (pref.agent==='all'?null:pref.agent)};
    const active=values.messages.filter((v,j)=>v || values.tokens[j] || values.commits[j]).length;
    // Reuse the complete matrix across polling updates. Unchanged table markup
    // keeps thousands of cells intact rather than reparsing them every ten seconds.
    box.overviewWidth=overviewWidth(box);
    const previousMatrix=$('#reviewmatrix',box);
    chart.innerHTML=`<section class="review-panel review-overview"><div class="review-panel-heading"><div><h3>${t('Activity over time')}</h3><p>${t('T/M/C bars share one time axis. Heights use each metric’s scale shown above.')}</p></div></div><div class="review-plot-wrap">${histogram(data,values,sum,box.overviewWidth)}${active ? '' : '<div class="review-no-activity">'+t('No recorded activity in this window. Try a wider time range.')+'</div>'}<div id="reviewtooltip" class="review-tooltip" role="tooltip" hidden></div></div><div class="review-plot-footer"><span>${number(active)} / ${number(data.buckets.length)} ${t('bins with activity')}</span><span>${t('Click a time bin to inspect its agents')} · <span class="review-partial-key"></span> ${t('Partial bin')}</span></div></section>
      <section class="review-panel review-agent-panel" id="reviewagents"><div class="review-panel-heading"><div><h3>${t('Agent activity bins')}</h3><p>${t('T = Tokens · M = Messages · C = Commits. Scroll horizontally to explore all time bins.')}</p></div><div class="review-agent-tools"><label class="review-active-filter"><input type="checkbox" id="reviewactive"${pref.activeOnly?' checked':''}>${t('Only agents with activity')}</label><label class="review-sort">${t('Sort agents')}<select id="reviewsort">${['tokens','messages','commits','name'].map(k=>`<option value="${k}">${t(k==='name'?'Name':METRICS.find(m=>m.key===k).label)}</option>`).join('')}</select></label></div></div><div class="review-agent-layout"><div class="review-agent-chart"><div id="reviewscales" class="review-scale-legend"></div><div class="review-matrix" id="reviewmatrix" role="region" aria-label="${t('Agent activity bins')}" tabindex="0"></div></div><div class="review-inspector" id="reviewdetail" role="region" aria-label="${t('Selected interval details')}"></div></div></section>
      <details class="review-method"><summary>${t('How these metrics are measured')}</summary>${METRICS.map(m=>`<p><strong style="color:${m.color}">${t(m.label)}</strong> ${escape(data.definitions[m.key])}</p>`).join('')}<p>${escape(data.definitions.history)}</p><p>${t('Times follow the selected time zone. Frequencies use the observed duration of each bin. Partial bins cover only the portion inside the selected window.')}</p></details>
      ${data.usage_history_incomplete?'<p class="review-import">'+t('Legacy usage lacks complete model/cache records. Identifiable duplicates were corrected; remaining historical token totals may be incomplete.')+'</p>':''}
      ${data.history_import_pending?'<p class="review-import">'+t('Historical import is continuing; more recorded activity will appear on refresh.')+'</p>':''}`;
    if(previousMatrix)$('#reviewmatrix',box).replaceWith(previousMatrix);
    $('#reviewsort',box).value=pref.sort;$('#reviewsort',box).onchange=e=>{pref.sort=e.target.value;save();drawMatrix(box);};
    $('#reviewactive',box).onchange=e=>{pref.activeOnly=e.target.checked;box.selection=null;save();render(box);};
    $('#reviewactive',box).disabled=pref.agent!=='all';$('#reviewactive',box).parentElement.title=t('Activity filter applies when viewing all agents.');
    const plot=$('.review-plot-wrap',box),tip=$('#reviewtooltip',box);
    plot.querySelectorAll('[data-review-bin]').forEach(hit=>{
      const j=Number(hit.dataset.reviewBin);
      const hover=()=>{const [from,to]=bounds(data,j);tip.innerHTML=`<strong>${datetime(from)} — ${datetime(to)}</strong>${METRICS.map(m=>`<div><i style="background:${m.color}"></i><span>${t(m.label)}</span><b>${number(values[m.key][j])}</b></div>`).join('')}${data.partial_buckets[j]?'<small>'+t('Partial bin')+'</small>':''}`;tip.hidden=false;tip.dataset.bin=String(j);box.querySelectorAll('.review-crosshair').forEach(line=>{line.removeAttribute('hidden');line.setAttribute('x',hit.getAttribute('x'));});};
      hit.onpointerenter=hover;hit.onfocus=hover;hit.onpointermove=e=>{const r=plot.getBoundingClientRect();tip.style.left=Math.max(8,Math.min(e.clientX-r.left+14,r.width-tip.offsetWidth-8))+'px';tip.style.top=Math.max(8,Math.min(e.clientY-r.top+12,r.height-tip.offsetHeight-8))+'px';};
      hit.onpointerleave=()=>{tip.hidden=true;highlight(box);};hit.onblur=()=>{tip.hidden=true;highlight(box);};hit.onclick=()=>selectBin(box,j,null,true);
      hit.onkeydown=e=>{if(['Enter',' '].includes(e.key)){e.preventDefault();selectBin(box,j,null,true);}else if(['ArrowLeft','ArrowRight','Home','End'].includes(e.key)){e.preventDefault();const to=e.key==='Home'?0:e.key==='End'?data.buckets.length-1:Math.max(0,Math.min(data.buckets.length-1,j+(e.key==='ArrowRight'?1:-1)));selectBin(box,to,null,true);hit.closest('svg').querySelector(`[data-review-bin="${to}"]`)?.focus({preventScroll:true});}};
    });
    box.reviewObserver?.disconnect();
    drawMatrix(box);drawDetail(box,selected);
    box.reviewObserver=new ResizeObserver(()=>{
      if(!box.isConnected)return;
      if(overviewWidth(box)!==box.overviewWidth){render(box);return;}
      if(matrixLabelWidth(box)===box.matrixLabelWidth)return;
      const restore=preserveViewport(box),focused=document.activeElement;
      const cell=focused?.dataset.reviewAgent?{name:focused.dataset.reviewAgent,j:focused.dataset.reviewBucket}:null;
      drawMatrix(box);
      if(cell)box.querySelector(`[data-review-agent="${CSS.escape(cell.name)}"][data-review-bucket="${cell.j}"]`)?.focus({preventScroll:true});
      restore();
    });
    box.reviewObserver.observe($('#reviewmatrix',box));
    if(focusCell) {const index=data.buckets.indexOf(focusCell.at);box.querySelector(`[data-review-agent="${CSS.escape(focusCell.name)}"][data-review-bucket="${index}"]`)?.focus({preventScroll:true});}
    if(focusedBin)plot.querySelector(`[data-review-bin="${data.buckets.indexOf(focusedBin)}"]`)?.focus({preventScroll:true});
    if(focused?.id==='reviewsort')$('#reviewsort',box).focus({preventScroll:true});
    if(focused?.id==='reviewactive')$('#reviewactive',box).focus({preventScroll:true});
    if(['reviewdetailprev','reviewdetailnext','reviewclearselection'].includes(focused?.id))$('#'+focused.id,box)?.focus({preventScroll:true});
    restoreViewport();
    if(focused?.matches('#reviewdetail h3'))$('#reviewdetail h3',box).focus({preventScroll:true});
    if(focused?.matches('.review-method>summary'))$('.review-method>summary',box).focus({preventScroll:true});
    if(focused?.matches('#reviewparticipants>summary'))$('#reviewparticipants>summary',box).focus({preventScroll:true});
  }
  const matrixLabelWidth = box => $('#reviewmatrix',box).clientWidth<450?110:140;
  function drawMatrix(box) {
    const data=box.review,rows=[...box.filtered],count=data.buckets.length,matrix=$('#reviewmatrix',box);
    rows.sort((a,b)=>pref.sort==='name'?a.name.localeCompare(b.name):b.totals[pref.sort]-a.totals[pref.sort] || a.name.localeCompare(b.name));
    const label=matrixLabelWidth(box);
    box.matrixLabelWidth=label;
    const maxima=Object.fromEntries(METRICS.map(m=>[m.key,rows.reduce((max,a)=>a[m.key].reduce((max,value)=>Math.max(max,value),max),1)]));
    $('#reviewscales',box).innerHTML=`<span title="${t('Bar heights compare the same metric across agents. Different metrics have separate scales.')}">${t('Shared scale per metric')}</span>${METRICS.map(m=>`<span style="--metric:${m.color}"><i></i>${m.short} · ${t(m.label)} <b title="${number(maxima[m.key])}">0–${metricNumber(m.key,maxima[m.key])}</b></span>`).join('')}`;
    let header='';for(let j=0;j<count;j++){const [from,to]=bounds(data,j);header+=`<th scope="col" data-review-column="${j}" title="${escape(datetime(from)+' — '+datetime(to))}"><span>${datetime(from,'day')}</span><b>${data.bucket_seconds<86400?datetime(from,'clock'):unit(data.bucket_seconds)}</b>${data.partial_buckets[j]?'<small title="'+t('Partial bin')+'">◐</small>':''}</th>`;}
    const body=rows.map(a=>`<tr><th scope="row" class="agent-label"><strong class="review-agent-name" title="@${escape(a.name)}">@${escape(a.name)}</strong><small title="${escape(a.title)}"><i class="review-agent-status${a.retired?' retired':''}"></i>${escape(a.title)}${a.retired?' · '+t('Retired'):''}</small><div class="review-agent-totals">${METRICS.map(m=>`<span title="${t(m.label)}: ${number(a.totals[m.key])}" style="color:${m.color}">${m.short} ${metricNumber(m.key,a.totals[m.key])}</span>`).join('')}</div></th>`+
      Array.from({length:count},(_,j)=>{const [from,to]=bounds(data,j),label='@'+a.name+' · '+datetime(from)+' — '+datetime(to)+' · '+METRICS.map(m=>t(m.label)+': '+number(a[m.key][j])).join(' · ');
        const empty=METRICS.every(m=>!a[m.key][j]);
        return `<td data-review-column="${j}"><button class="review-cell${data.partial_buckets[j]?' partial':''}${empty?' empty':''}" data-review-agent="${escape(a.name)}" data-review-bucket="${j}" data-review-at="${data.buckets[j]}" tabindex="-1" aria-label="${escape(label)}" title="${escape(label)}">${METRICS.map((m,index)=>`<span class="review-bin-metric" data-metric="${m.key}" style="--metric:${m.color};--metric-index:${index+1}"><strong>${metricNumber(m.key,a[m.key][j])}</strong><i aria-hidden="true"><b style="height:${(a[m.key][j]/maxima[m.key]*100).toFixed(3)}%"></b></i><em title="${t(m.label)}">${m.short}</em></span>`).join('')}</button></td>`;}).join('')+'</tr>').join('');
    const html=`<table style="width:${label+count*BIN_WIDTH}px;--agent-width:${label}px" aria-label="${escape(t('Tokens, messages and commits for every agent and time bin'))}"><colgroup><col style="width:${label}px">${'<col style="width:56px">'.repeat(count)}</colgroup><thead><tr><th class="agent-label" scope="col" title="${escape(timezone())}">${t('Agent / time')}<small>${escape(timezone())}</small><button id="reviewshowdetail" class="secondary review-detail-link">${t('Details')} ↑</button></th>${header}</tr></thead><tbody>${body}</tbody></table>`;
    const markup=rows.length?html:`<div class="review-empty"><h3>${t(data.agents.length?'No agents with activity in this window.':'No recorded agents yet')}</h3><p>${t('Recorded activity will appear as this project’s agents work and communicate.')}</p>${data.agents.length?'<button id="reviewshowall" class="secondary">'+t('Show all agents')+'</button>':''}</div>`;
    if(matrix.reviewMarkup!==markup){matrix.innerHTML=markup;matrix.reviewMarkup=markup;}
    if($('#reviewshowdetail',box))$('#reviewshowdetail',box).onclick=()=>{const detail=$('#reviewdetail',box);detail.scrollIntoView({block:'start',inline:'nearest'});$('h3',detail)?.focus({preventScroll:true});};
    if($('#reviewshowall',box))$('#reviewshowall',box).onclick=()=>{pref.activeOnly=false;save();render(box);};
    matrix.onclick=e=>{const cell=e.target.closest('[data-review-agent]');if(cell)selectBin(box,Number(cell.dataset.reviewBucket),cell.dataset.reviewAgent);};
    matrix.onkeydown=e=>{
      const cell=e.target.closest('[data-review-agent]');
      if(!cell || !['ArrowLeft','ArrowRight','ArrowUp','ArrowDown','Home','End'].includes(e.key))return;
      e.preventDefault();let j=Number(cell.dataset.reviewBucket),name=cell.dataset.reviewAgent;
      if(e.key==='Home')j=0;else if(e.key==='End')j=count-1;
      else if(['ArrowLeft','ArrowRight'].includes(e.key))j=Math.max(0,Math.min(count-1,j+(e.key==='ArrowRight'?1:-1)));
      else {const index=rows.findIndex(a=>a.name===name),to=Math.max(0,Math.min(rows.length-1,index+(e.key==='ArrowDown'?1:-1)));name=rows[to].name;}
      selectBin(box,j,name,true);matrix.querySelector(`[data-review-agent="${CSS.escape(name)}"][data-review-bucket="${j}"]`)?.focus();
    };
    highlight(box);
  }
  function highlight(box) {
    const data=box.review,j=data.buckets.indexOf(box.selection.at),agent=box.selection.agent;
    const first=$(`[data-review-bin="${j}"]`,box);
    box.querySelectorAll('[data-review-bin]').forEach(c=>{const selected=Number(c.dataset.reviewBin)===j;c.setAttribute('tabindex',c===first?'0':'-1');c.setAttribute('aria-pressed',String(selected));});
    box.querySelectorAll('[data-review-column]').forEach(c=>c.classList.toggle('selected-col',Number(c.dataset.reviewColumn)===j));
    box.querySelectorAll('[data-review-agent]').forEach(c=>{const selected=Number(c.dataset.reviewBucket)===j && c.dataset.reviewAgent===agent;c.classList.toggle('selected',selected);c.setAttribute('aria-pressed',String(selected));});
    const matrix=$('#reviewmatrix',box);let entry=matrix.querySelector('.review-cell.selected');
    if(!entry)entry=matrix.querySelector(`[data-review-bucket="${j}"]`) || matrix.querySelector('.review-cell');
    matrix.querySelectorAll('.review-cell').forEach(c=>c.tabIndex=c===entry?0:-1);
    box.querySelectorAll('.review-crosshair').forEach(line=>{line.removeAttribute('hidden');line.setAttribute('x',first?.getAttribute('x') || '38');});
  }
  function revealBin(box,j) {
    const matrix=$('#reviewmatrix',box),column=matrix.querySelector(`thead [data-review-column="${j}"]`);
    if(!column)return;
    const viewport=matrix.getBoundingClientRect(),cell=column.getBoundingClientRect();
    const left=viewport.left+matrix.clientLeft+matrixLabelWidth(box),right=viewport.left+matrix.clientLeft+matrix.clientWidth;
    if(cell.left<left)matrix.scrollLeft-=left-cell.left;
    else if(cell.right>right)matrix.scrollLeft+=cell.right-right;
  }
  function selectBin(box,j,agent,reveal=false) {
    box.selection={at:box.review.buckets[j],agent};
    highlight(box);drawDetail(box,j);
    if(reveal)revealBin(box,j);
    $('#reviewselectionstatus',box).textContent=(agent?'@'+agent:t('All agents in scope'))+' · '+bounds(box.review,j).map(at=>datetime(at)).join(' — ');
  }
  function drawDetail(box,j) {
    const data=box.review,selected=box.selection.agent,rows=selected?box.filtered.filter(a=>a.name===selected):box.filtered,[from,to]=bounds(data,j);
    const values=Object.fromEntries(METRICS.map(m=>[m.key,rows.reduce((sum,a)=>sum+a[m.key][j],0)]));
    const hours=data.bucket_coverage_seconds[j]/3600,ranked=[...rows].sort((a,b)=>b.tokens[j]-a.tokens[j] || b.messages[j]-a.messages[j]).filter(a=>a.tokens[j] || a.messages[j] || a.commits[j]);
    const focused=document.activeElement?.id,headingFocused=document.activeElement?.matches('#reviewdetail h3');
    $('#reviewdetail',box).innerHTML=`<div class="review-inspector-heading"><div class="review-eyebrow">${t('SELECTED TIME BIN')}</div><div class="review-interval-nav"><button id="reviewdetailprev" class="secondary" aria-label="${t('Previous time bin')}"${j===0?' disabled':''}>←</button><span>${j+1} / ${data.buckets.length}</span><button id="reviewdetailnext" class="secondary" aria-label="${t('Next time bin')}"${j===data.buckets.length-1?' disabled':''}>→</button></div></div>
      <h3 tabindex="-1">${selected?'@'+escape(selected):t('All agents in scope')}</h3><p class="review-selected-period">${datetime(from)}<br>— ${datetime(to)}<span>${escape(timezone())}${data.partial_buckets[j]?' · '+t('Partial bin'):''}</span></p>
      <div class="review-detail-values">${METRICS.map(m=>`<div style="--metric:${m.color}"><span>${t(m.label)}</span><strong title="${number(values[m.key])}">${metricNumber(m.key,values[m.key])}</strong><small title="${(values[m.key]/hours).toLocaleString(options.locale?.() || undefined,{maximumFractionDigits:2})} / ${t('hour')}">${m.key==='tokens'?number(values[m.key]/hours,true):(values[m.key]/hours).toLocaleString(options.locale?.() || undefined,{maximumFractionDigits:2})} / ${t('hour')}</small></div>`).join('')}</div>
      <details id="reviewparticipants"${box.participantsOpen?' open':''}><summary>${t('Contributing agents')} · ${ranked.length}</summary><div class="review-contributors">${ranked.length?ranked.slice(0,8).map(a=>`<div><b>@${escape(a.name)}</b>${METRICS.map(m=>`<span title="${t(m.label)}: ${number(a[m.key][j])}" style="color:${m.color}">${m.label[0]} ${metricNumber(m.key,a[m.key][j])}</span>`).join('')}</div>`).join(''):'<p>'+t('No recorded activity in this bin.')+'</p>'}${ranked.length>8?'<p>'+t('Top eight contributors shown. Select an agent to inspect their activity.')+'</p>':''}</div></details>
      ${selected && rows[0]?`<p class="review-detail-foot">${t('Selected range')}: ${number(rows[0].totals.threads)} ${t('threads started')} · ${number(rows[0].totals.replies)} ${t('replies sent')} · ${number(rows[0].totals.received)} ${t('mentions received')}</p>${box.filtered.length>1?'<button id="reviewclearselection" class="secondary">'+t('Compare all agents')+'</button>':''}`:''}`;
    $('#reviewdetailprev',box).onclick=()=>selectBin(box,j-1,box.selection.agent,true);
    $('#reviewdetailnext',box).onclick=()=>selectBin(box,j+1,box.selection.agent,true);
    $('#reviewparticipants',box).ontoggle=e=>{if(e.target.isConnected)box.participantsOpen=e.target.open;};
    if($('#reviewclearselection',box))$('#reviewclearselection',box).onclick=()=>selectBin(box,j,null);
    if(['reviewdetailprev','reviewdetailnext','reviewclearselection'].includes(focused)){
      const button=$('#'+focused,box);(button && !button.disabled?button:$('#reviewdetail h3',box)).focus({preventScroll:true});
    }else if(headingFocused)$('#reviewdetail h3',box).focus({preventScroll:true});
  }
  function exportCSV(box) {
    const data=box.review;if(!data)return;
    const quote=v=>'"'+String(v).replaceAll('"','""')+'"';
    const csv=[['bin_start_utc','bin_end_utc','agent','tokens','messages','commits','observed_seconds','partial'].map(quote).join(',')];
    for(const a of box.filtered) data.buckets.forEach((at,j)=>{const [from,to]=bounds(data,j);csv.push([new Date(from).toISOString(),new Date(to).toISOString(),a.name,a.tokens[j],a.messages[j],a.commits[j],data.bucket_coverage_seconds[j],data.partial_buckets[j]].map(quote).join(','));});
    const url=URL.createObjectURL(new Blob(['\ufeff'+csv.join('\r\n')],{type:'text/csv;charset=utf-8'})),link=document.createElement('a');
    link.href=url;link.download=`huntun-performance-${project}-${data.range}-${data.bin}.csv`;link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
  }
  return {update,stop:()=>{request++;next=0;$('#performancebox')?.reviewObserver?.disconnect();},relocalize:()=>{
    const box=$('#performancebox');if(box?.review && box.dataset.reviewProject===project){const restore=preserveViewport(box);build(box,project);render(box);restore();}
  }};
}
