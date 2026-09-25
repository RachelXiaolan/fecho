const $ = (selector, root=document) => root.querySelector(selector);
  const $$ = (selector, root=document) => [...root.querySelectorAll(selector)];
  const esc = value => String(value == null ? '' : value).replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  const state = {data:null, view:'today', taskStatus:'all', reportTab:'daily', undo:null, toastTimer:null,
    me:null, viewAs:null, editing:false, settings:{agents:[], folders:[]}, admin:null, style:null,
    prevView:null, feedback:{images:[], mine:[], all:null}, filterMenu:{open:null,active:0}, accountMenuOpen:false};

  // ---- 界面语言 ----
  // 记在浏览器里，登录页、onboard 页、面板共用同一个 key。默认中文。
  const I18N = JSON.parse(document.getElementById('i18n').textContent);
  const LANG_KEY = 'fecho-lang';
  let lang = (()=>{ try{ return localStorage.getItem(LANG_KEY)==='en'?'en':'zh'; }catch(_){ return 'zh'; } })();
  function t(key, vars={}){
    const text=(I18N[lang]||{})[key] ?? I18N.zh[key] ?? key;
    return text.replace(/\{(\w+)\}/g,(_,name)=>vars[name] ?? '');
  }
  function applyStatic(){
    document.documentElement.lang = lang==='en' ? 'en' : 'zh-CN';
    document.title = t('doc_title');
    $$('[data-i18n]').forEach(el=>{ el.textContent=t(el.dataset.i18n); });
    $$('[data-i18n-placeholder]').forEach(el=>{ el.placeholder=t(el.dataset.i18nPlaceholder); });
    $$('[data-i18n-aria]').forEach(el=>{ el.setAttribute('aria-label',t(el.dataset.i18nAria)); });
    const langToggle=$('#lang-toggle'); if(langToggle){ langToggle.textContent=lang==='en'?'EN':'中'; langToggle.setAttribute('aria-pressed',String(lang==='en')); langToggle.title=t('lang_aria'); }
    const shell=$('.shell');
    if(shell&&typeof applySidebar==='function') applySidebar(shell.classList.contains('sidebar-collapsed'),{persist:false});
  }
  function setLang(next){
    lang=next; try{ localStorage.setItem(LANG_KEY,next); }catch(_){}
    applyStatic(); switchView(state.view,{scroll:false});
    if(state.me&&state.me.cloud) renderAccountMenu();
    showViewAs();
    if(state.data){ hydrateFilters(); renderAll(); }
    if(state.me&&state.me.cloud){ renderSettings(); if(state.admin) renderAdmin(); if(state.style) showStyle(state.style); renderFeedbackDraft(); renderFeedbackMine(); if(state.feedback.all) renderFeedbackAll(); }
  }

  async function request(path, options={}){
    const response = await fetch(path, options);
    let payload = {};
    try { payload = await response.json(); } catch (_) { payload = {}; }
    if (!response.ok) throw new Error(payload.error || payload.detail || t('err_request',{status:response.status}));
    if (payload.ok === false) throw new Error(payload.error || t('err_operation'));
    return payload;
  }
  const post = (path, body) => request(path, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body || {})});

  function localDate(){
    const parts=new Intl.DateTimeFormat('en',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'}).formatToParts(new Date());
    const values=Object.fromEntries(parts.map(part=>[part.type,part.value]));
    return `${values.year}-${values.month}-${values.day}`;
  }
  // 切日期：看过的旧日期直接用上次读到的，不再去服务器读；只有今天会自动刷新（打开页面、每分钟、切回这个标签页）。
  // 服务器这边本来就只是读已经记下的进展和日报，不会重新整理；真要重出日报点「重新生成」。
  const isToday=date=>date===localDate();
  const cacheKey=date=>((state.viewAs||{}).author||'me')+'|'+date;
  async function loadDashboard({quiet=false, useCache=false}={}){
    const date = $('#work-date').value || localDate();
    const cached=(state.cache||{})[cacheKey(date)];
    if(useCache&&cached){
      state.data=cached; hydrateFilters(); renderAll();
      if(isToday(date)) loadDashboard({quiet:true});      // 今天的记录可能又多了，后台悄悄补一次
      return;
    }
    if (!quiet){ $('#loading-copy').textContent=t(isToday(date)?'loading':'loading_day',{date}); $('#loading').classList.add('show'); }
    $('#error-state').classList.remove('show');
    try{
      const data = await request(state.viewAs
        ? `/api/admin/dashboard?author=${encodeURIComponent(state.viewAs.author)}&date=${encodeURIComponent(date)}`
        : `/api/dashboard?date=${encodeURIComponent(date)}`);
      // 等回来的时候人可能已经切到别的日期了：存下来，但别画到页面上
      if(data.date!==($('#work-date').value||localDate())){ (state.cache||(state.cache={}))[cacheKey(data.date)]=data; return; }
      state.data=data; hydrateFilters(); renderAll();
    }catch(error){ resetFilterControls(); showError(error.message); }
    finally{ $('#loading').classList.remove('show'); }
  }

  function hydrateFilters(){
    const data=state.data;
    fillSelect($('#agent-filter'), data.filters.agents, t('all'));
    fillSelect($('#source-filter'), data.filters.ingestion_methods, t('all'));
    const statusLabels=Object.fromEntries(['done','wip','blocked','unknown'].map(s=>[s,t('status_'+s)]));
    fillSelect($('#status-filter'), data.filters.completion_statuses, t('all'), statusLabels);
    setFilterControlsReady(true);
  }
  function resetFilterControls(){
    // 接口失败时仍保留可见的“全部”项和错误提示，
    // 不能把三个控件冻成没有文字的紫色方块，让人以为下拉菜单自身坏了。
    fillSelect($('#agent-filter'), [], t('all'));
    fillSelect($('#source-filter'), [], t('all'));
    fillSelect($('#status-filter'), [], t('all'));
    setFilterControlsReady(true);
  }
  function setFilterControlsReady(ready){
    $$('[data-filter-trigger]').forEach(trigger=>{
      trigger.disabled=!ready;
      trigger.setAttribute('aria-disabled',String(!ready));
    });
  }
  function fillSelect(select, values, first, labels={}){
    const current=select.value; select.innerHTML=`<option value="">${esc(first)}</option>`+
      values.map(v=>`<option value="${esc(v)}">${esc(labels[v]||v)}</option>`).join('');
    if(values.includes(current)) select.value=current;
    syncFilterMenu(select);
  }
  function filterParts(select){
    return {field:select.closest('[data-filter-select]'),trigger:$('#'+select.id+'-trigger'),menu:$('#'+select.id+'-menu')};
  }
  function syncFilterMenu(select){
    const {trigger,menu}=filterParts(select); if(!trigger||!menu) return;
    const options=[...select.options], selectedIndex=Math.max(0,select.selectedIndex);
    trigger.textContent=(options[selectedIndex]||{}).textContent||'';
    menu.innerHTML=options.map((option,index)=>`<button class="filter-option" id="${select.id}-option-${index}" type="button" role="option" aria-selected="${index===selectedIndex}" data-filter-option="${select.id}" data-filter-index="${index}"><span class="filter-option-mark" aria-hidden="true">✓</span><span>${esc(option.textContent)}</span></button>`).join('');
    if(state.filterMenu.open===select.id) updateFilterMenuActive(select,state.filterMenu.active);
    else trigger.removeAttribute('aria-activedescendant');
  }
  function updateFilterMenuActive(select,index){
    const {trigger,menu}=filterParts(select), count=select.options.length; if(!trigger||!menu||!count) return;
    const next=(index+count)%count; state.filterMenu.active=next;
    $$('.filter-option',menu).forEach((option,i)=>option.classList.toggle('active',i===next));
    trigger.setAttribute('aria-activedescendant',select.id+'-option-'+next);
  }
  function closeFilterMenu({focus=false}={}){
    const id=state.filterMenu.open; if(!id) return;
    const select=$('#'+id); if(!select){ state.filterMenu.open=null; return; }
    const {field,trigger,menu}=filterParts(select);
    menu.hidden=true; field.classList.remove('open'); trigger.setAttribute('aria-expanded','false'); trigger.removeAttribute('aria-activedescendant');
    state.filterMenu.open=null; state.filterMenu.active=0;
    if(focus) trigger.focus();
  }
  function openFilterMenu(select){
    if(state.filterMenu.open&&state.filterMenu.open!==select.id) closeFilterMenu();
    const {field,trigger,menu}=filterParts(select); if(!field||!trigger||!menu) return;
    if(trigger.disabled) return;
    syncFilterMenu(select); menu.hidden=false; field.classList.add('open'); trigger.setAttribute('aria-expanded','true');
    state.filterMenu.open=select.id; updateFilterMenuActive(select,Math.max(0,select.selectedIndex));
  }
  function chooseFilterOption(select,index){
    if(!select.options[index]) return;
    select.selectedIndex=index;
    select.dispatchEvent(new Event('change',{bubbles:true}));
    syncFilterMenu(select); closeFilterMenu({focus:true});
  }
  function moveFilterMenu(select,step){
    updateFilterMenuActive(select,state.filterMenu.active+step);
  }
  function matches(update){
    const agent=$('#agent-filter').value, source=$('#source-filter').value, status=$('#status-filter').value;
    return (!agent || update.source_agent===agent) && (!source || update.ingestion_method===source) &&
      (!status || update.completion_status===status);
  }
  function statusTag(status){ const known=['done','wip','blocked','unknown'].includes(status); return `<span class="tag ${esc(status)}">${known?esc(t('status_'+status)):esc(status)}</span>`; }
  // 有模型起的短名就用短名：自由任务的标题是第一条进展的原文，当名字认不出是哪件事
  function issueLabel(task){ const name=task.alias||task.title; return task.issue_key ? `${esc(task.issue_key)} · ${esc(name)}` : `${esc(name)} · ${esc(t('freeform'))}`; }
  function empty(title, copy){ return `<div class="empty"><strong>${esc(title)}</strong>${esc(copy)}</div>`; }

  function renderAll(){
    const d=state.data, o=d.overview;
    checkGen(); renderGen();
    (state.cache||(state.cache={}))[cacheKey(d.date)]=d;     // 改完东西服务器回的最新数据也记下，切回这天看到的是新的
    $('#nav-today').textContent=o.updates; $('#nav-review').textContent=d.review.items.length;
    $('#nav-tasks').textContent=d.tasks.items.filter(t=>t.status==='open').length;
    $('#nav-reports').textContent=d.reports.dirty?'!':(d.reports.daily?'1':'—');
    $('#nav-system').textContent=d.system.scan_runs.some(r=>r.status==='failed')?'!':'✓';
    renderToday(); renderReview(); renderTasks(); renderReports(); renderSystem();
  }

  function renderToday(){
    const d=state.data,o=d.overview;
    $('#today-date').textContent=d.date.replaceAll('-',' / ');
    const visibleTasks=d.today.tasks.map(t=>({...t,updates:t.updates.filter(matches)})).filter(t=>t.updates.length);
    $('#today-heading').textContent=o.updates ? t(isToday(d.date)?'today_heading_n':'day_heading_n',{n:o.tasks}) : t('today_heading_none');
    $('#today-summary').textContent=o.updates ? t('today_summary_n',{updates:o.updates, agents:new Set(d.today.tasks.flatMap(x=>x.updates.map(u=>u.source_agent))).size, review:d.review.items.length}) : t('today_summary_none');
    $('#metrics').innerHTML=[['metric_updates',o.updates,''],['metric_tasks',o.tasks,''],['metric_review',d.review.items.length,'attention'],['metric_hidden',o.hidden,'']]
      .map(([label,value,kind])=>`<div class="metric ${kind}"><strong>${value}</strong><span>${esc(t(label))}</span></div>`).join('');
    $('#today-tasks').innerHTML=visibleTasks.length?visibleTasks.map(task=>{
      const statuses=task.updates.map(u=>u.completion_status); const taskStatus=statuses.includes('blocked')?'blocked':statuses.includes('wip')?'wip':statuses.every(s=>s==='done')?'done':'unknown';
      return `<details class="timeline" open><summary><span><span class="item-title">${issueLabel(task)}</span><span class="meta">${esc(t('n_updates',{n:task.updates.length}))}</span></span>${statusTag(taskStatus)}</summary><div class="timeline-body">${task.updates.map(u=>`<div class="progress-line"><div>${esc(u.content_md)}</div><div class="meta">${esc(u.source_agent)}${(u.meta||{}).entrypoint?' · '+esc(t('entry_'+u.meta.entrypoint)):''} · ${esc(u.ingestion_method)} · ${esc((u.created_at||'').slice(11,16))} ${statusTag(u.completion_status)}</div></div>`).join('')}</div></details>`;
    }).join(''):empty(t('empty_filter'),t('empty_filter_sub'));
    const notices=[];
    if(d.review.items.length) notices.push(`<div class="item"><div class="item-title">${esc(t('notice_review',{n:d.review.items.length}))}</div><div class="meta">${esc(t('notice_review_sub'))}</div></div>`);
    if(d.reports.dirty) notices.push(`<div class="item"><div class="item-title">${esc(t('notice_dirty'))}</div><div class="meta">${esc(t('notice_dirty_sub'))}</div></div>`);
    if(o.hidden) notices.push(`<div class="item"><div class="item-title">${esc(t('notice_hidden',{n:o.hidden}))}</div><div class="meta">${esc(t('notice_hidden_sub'))}</div></div>`);
    $('#attention').innerHTML=notices.join('')||empty(t('empty_attention'),t('empty_attention_sub'));
    $('#timeline').innerHTML=d.timeline.groups.length?d.timeline.groups.map(group=>`<details class="timeline"><summary><span class="item-title">${group.issue_key?`<a href="https://mobius.feedmob.com/issue/${esc(group.issue_key)}" target="_blank" rel="noreferrer">${esc(group.issue_key)}</a> · `:''}${esc(group.title)}</span><span class="meta">${esc(t('n_items',{n:Object.values(group.days).flat().length}))}</span></summary><div class="timeline-body">${Object.entries(group.days).sort().reverse().map(([date,items])=>`<div class="progress-line"><b>${esc(date)}</b>${items.map(text=>`<div>${esc(text)}</div>`).join('')}</div>`).join('')}</div></details>`).join(''):empty(t('empty_timeline'),t('empty_timeline_sub'));
  }

  function issueOptions(selected){
    return `<option value="" ${selected?'':'selected'}>${esc(t('freeform'))}</option>`+state.data.issues.items.map(issue=>`<option value="${esc(issue.issue_key)}" ${issue.issue_key===selected?'selected':''}>${esc(issue.issue_key)} · ${esc(issue.title)}</option>`).join('');
  }
  function renderReview(){
    const items=state.data.review.items.filter(matches); $('#review-total').textContent=t('n_items',{n:items.length});
    $('#review-list').innerHTML=items.length?items.map(item=>`<article class="review-card" data-update="${esc(item.update_id)}" data-old-issue="${esc(item.issue_key||'')}"><div class="review-grid"><div><div class="item-body">${esc(item.content)}</div><div class="meta"><span class="tag ${esc(item.confidence)}">${esc(t('confidence_'+item.confidence))}</span><span>${esc(item.method)}</span><span>${esc(item.source_agent)} · ${esc(item.ingestion_method)}</span><span>${esc((item.created_at||'').slice(11,16))}</span>${statusTag(item.completion_status)}</div></div><div class="review-actions"><label class="field"><span style="font-size:10px;color:var(--muted);font-weight:700">${esc(t('assign_to'))}</span><select data-role="issue">${issueOptions(item.issue_key)}</select></label><textarea data-role="content" aria-label="${esc(t('fix_content_aria'))}">${esc(item.content)}</textarea><div class="button-row"><button class="btn primary small" data-action="confirm-update">${esc(t('confirm_lock'))}</button></div></div></div></article>`).join(''):empty(t('empty_review'),t('empty_review_sub'));
    const hidden=state.data.hidden.items;
    $('#hidden-list').innerHTML=hidden.length?hidden.map(item=>`<div class="item item-head"><div><div class="item-body">${esc(item.content_md)}</div><div class="meta">${esc(item.status)} ${item.k?'· '+esc(item.k):''}</div></div><button class="btn small" data-action="restore" data-id="${esc(item.update_id)}">${esc(t('restore'))}</button></div>`).join(''):empty(t('empty_hidden'),t('empty_hidden_sub'));
  }

  // 合并目标 = 已有任务 + 同步来的 issue。issue 按编号去重；输进去的不在列表里也行，
  // 只要是 issue 编号，服务器会现去 Mobius 查。以前只列已有任务，issue 没进展就选不到
  function mergeTargets(){
    const live=state.data.tasks.items.filter(x=>x.status!=='merged'), seen=new Set(), out=[];
    // 同步来的 issue 服务器已经按「开着的在前、优先级高的在前」排好；有任务短名的用短名
    const alias={}; live.filter(x=>x.issue_key).forEach(x=>{ if(!alias[x.issue_key]) alias[x.issue_key]=x.alias||x.title; });
    (state.data.issues.items||[]).forEach(i=>{ if(seen.has(i.issue_key))return; seen.add(i.issue_key); out.push({label:`${i.issue_key} · ${alias[i.issue_key]||i.title}`,issue:i.issue_key,state:i.state,stateType:i.state_type}); });
    live.filter(x=>x.issue_key).forEach(x=>{ if(seen.has(x.issue_key))return; seen.add(x.issue_key); out.push({label:`${x.issue_key} · ${x.alias||x.title}`,issue:x.issue_key}); });
    const labels=new Set(out.map(o=>o.label));
    live.filter(x=>!x.issue_key).sort((a,b)=>(b.last_update||'').localeCompare(a.last_update||'')).forEach(x=>{
      let label=`${x.alias||x.title} · ${t('freeform')}`;
      if(labels.has(label)) label+=` (${(x.last_update||'').slice(5,10)})`;   // 同名的自由任务靠日期分开
      labels.add(label); out.push({label,task:x.task_id});
    });
    return out;
  }
  // issue 状态图标：形状照 Linear 那套（虚线圈 / 空圈 / 半圈 / 大半圈 / 勾 / 叉 / 斜杠），颜色用自己的主题色
  const STATUS_KIND={backlog:'backlog',todo:'todo',unstarted:'todo','in progress':'progress',started:'progress','in review':'review',
    done:'done',completed:'done',canceled:'canceled',cancelled:'canceled',duplicate:'duplicate'};
  function statusKind(name,type){ return STATUS_KIND[(name||'').toLowerCase()]||STATUS_KIND[(type||'').toLowerCase()]||(type?'todo':''); }
  function statusIcon(name,type){
    const kind=statusKind(name,type), c='cx="8" cy="8"';
    const shape={
      backlog:`<circle ${c} r="6" fill="none" stroke="currentColor" stroke-width="1.5" stroke-dasharray="2.2 2"/>`,
      todo:`<circle ${c} r="6" fill="none" stroke="currentColor" stroke-width="1.5"/>`,
      progress:`<circle ${c} r="6" fill="none" stroke="currentColor" stroke-width="1.5"/><path d="M8 4.2a3.8 3.8 0 0 1 0 7.6z" fill="currentColor"/>`,
      review:`<circle ${c} r="6" fill="none" stroke="currentColor" stroke-width="1.5"/><path d="M8 4.2a3.8 3.8 0 1 1-3.8 3.8H8z" fill="currentColor"/>`,
      done:`<circle ${c} r="7" fill="currentColor"/><path d="m5.1 8.2 2 2 3.9-4.1" fill="none" stroke="var(--surface)" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>`,
      canceled:`<circle ${c} r="7" fill="currentColor"/><path d="m5.7 5.7 4.6 4.6m0-4.6-4.6 4.6" stroke="var(--surface)" stroke-width="1.6" stroke-linecap="round"/>`,
      duplicate:`<circle ${c} r="7" fill="currentColor"/><path d="m5.6 10.4 4.8-4.8" stroke="var(--surface)" stroke-width="1.6" stroke-linecap="round"/>`,
      free:`<rect x="2.5" y="2.5" width="11" height="11" rx="3" fill="none" stroke="currentColor" stroke-width="1.4" stroke-dasharray="2 1.8"/>`,
    }[kind||'free'];
    return `<svg class="status-icon status-${kind||'free'}" viewBox="0 0 16 16" width="15" height="15" aria-hidden="true">${shape}</svg>`;
  }

  // 合并目标下拉：原生 datalist 只能显示纯文字，放不了状态图标，所以自己画
  const MERGE_MENU_MAX=60;
  function mergeMatches(query){
    const q=(query||'').trim().toLowerCase(), all=state.mergeTargets||[];
    return (q?all.filter(o=>o.label.toLowerCase().includes(q)):all).slice(0,MERGE_MENU_MAX);
  }
  function openMergeMenu(input){
    closeMergeMenu();
    const items=mergeMatches(input.value);
    if(!items.length) return;
    const menu=document.createElement('div');
    menu.className='merge-menu'; menu.id='merge-menu'; menu.setAttribute('role','listbox');
    menu.innerHTML=items.map((o,i)=>`<div class="merge-option${i===0?' active':''}" role="option" id="merge-opt-${i}" data-i="${i}" title="${esc(o.issue?(o.state||''):t('freeform'))}">${o.issue?statusIcon(o.state,o.stateType):statusIcon('','')}<span>${esc(o.label)}</span></div>`).join('');
    input.parentElement.appendChild(menu);
    input.setAttribute('aria-expanded','true'); input.setAttribute('aria-controls','merge-menu'); input.setAttribute('aria-activedescendant','merge-opt-0');
    state.mergeMenu={input,items,active:0};
  }
  function closeMergeMenu(){
    const m=state.mergeMenu; if(!m) return;
    state.mergeMenu=null;
    const menu=$('#merge-menu'); if(menu) menu.remove();
    if(m.input){ m.input.setAttribute('aria-expanded','false'); m.input.removeAttribute('aria-activedescendant'); }
  }
  function moveMergeActive(step){
    const m=state.mergeMenu; if(!m) return;
    m.active=(m.active+step+m.items.length)%m.items.length;
    $$('#merge-menu .merge-option').forEach((el,i)=>el.classList.toggle('active',i===m.active));
    const el=$(`#merge-opt-${m.active}`); if(el){ el.scrollIntoView({block:'nearest'}); m.input.setAttribute('aria-activedescendant',el.id); }
  }
  function pickMergeOption(i){
    const m=state.mergeMenu; if(!m||!m.items[i]) return;
    m.input.value=m.items[i].label;
    closeMergeMenu();
  }
  function resolveMergeTarget(text){
    const hit=(state.mergeTargets||[]).find(o=>o.label===text);
    if(hit) return hit;
    const key=(text.match(/^\s*([A-Za-z][A-Za-z0-9]{1,9}-\d+)(?![\w-])/)||[])[1];
    return key?{issue:key.toUpperCase()}:null;
  }
  function renderTasks(){
    $$('#task-tabs button').forEach(button=>button.classList.toggle('active',button.dataset.taskStatus===state.taskStatus));
    const hasGlobalFilter=$('#agent-filter').value||$('#source-filter').value||$('#status-filter').value;
    const tasks=state.data.tasks.items.filter(x=>(state.taskStatus==='all'||x.status===state.taskStatus)&&
      (!hasGlobalFilter||x.updates.some(matches)));
    // 页面每分钟会悄悄刷新一次，重画会把正在输入的合并目标冲掉——先记下，画完放回去
    const typed={}; $$('.merge-input').forEach(i=>{ if(i.value) typed[i.dataset.mergeTarget]=i.value; });
    const typing=document.activeElement&&document.activeElement.dataset?document.activeElement.dataset.mergeTarget:null;
    closeMergeMenu();
    state.mergeTargets=mergeTargets();
    $('#task-list').innerHTML=tasks.length?tasks.map(task=>{
      const action=task.status==='open'?`<button class="btn small" data-action="complete-task" data-id="${esc(task.task_id)}">${esc(t('complete'))}</button>`:task.status==='done'?`<button class="btn small" data-action="reopen-task" data-id="${esc(task.task_id)}">${esc(t('reopen'))}</button>`:'';
      const statusText=['open','done','merged'].includes(task.status)?t('task_status_'+task.status):task.status;
      return `<div class="task-row"><div><div class="item-title">${issueLabel(task)}</div><div class="item-body">${esc(task.last_progress||t('no_progress'))}</div><div class="meta"><span class="tag ${task.status==='done'?'done':''}">${esc(statusText)}</span><span>${esc(t('n_updates',{n:task.update_count}))}</span><span>${esc(t('last_updated',{time:(task.last_update||'').slice(0,16).replace('T',' ')}))}</span></div></div><div class="task-actions">${action}${task.status!=='merged'?`<span class="merge-box"><input class="merge-input" role="combobox" aria-expanded="false" aria-autocomplete="list" aria-label="${esc(t('merge_aria'))}" placeholder="${esc(t('merge_into'))}" data-merge-target="${esc(task.task_id)}" autocomplete="off" spellcheck="false"></span><button class="btn small" data-action="merge-task" data-id="${esc(task.task_id)}">${esc(t('merge'))}</button>`:''}</div></div>`;
    }).join(''):empty(t('empty_tasks'),t('empty_tasks_sub'));
    Object.entries(typed).forEach(([id,v])=>{ const i=$(`[data-merge-target="${id}"]`); if(i) i.value=v; });
    if(typing){ const i=$(`[data-merge-target="${typing}"]`); if(i){ i.focus(); i.setSelectionRange(i.value.length,i.value.length); } }
  }

  // ---- Markdown → HTML ----
  // 日报是我们自己的代码拼出来的，语法范围很小（标题、有序/嵌套列表、加粗、链接、
  // 引用、行内代码），所以不引外部库：面板要保持零外部依赖，断网也能打开。
  // 内容来自模型输出，一律先转义再加标记；链接只放行 http(s)，挡掉 javascript: 这类。
  const MD_IMG=/!\[([^\]]*)\]\(((?:https?:\/\/|\/api\/reports\/images\/)[^\s)]+)\)/g;   // 只放行 http(s) 和本站传上来的图
  let mdImages=0;
  function mdInline(text){
    // 行内代码先换成占位符，免得里面的 ** 被当成加粗。占位符用私有区字符的
    // 转义写法——文件里不能夹原始控制字符，浏览器解析 script 时会把它们换掉。
    const codes=[];
    let s=esc(text).replace(/`([^`]+)`/g,(_,c)=>{codes.push(c);return ''+(codes.length-1)+'';});
    // 图片：![说明|宽度](地址)，宽度写法和 Obsidian 一样。按出现顺序编号，编辑时拖大小靠编号找回原文那一处
    s=s.replace(MD_IMG,(_,alt,url)=>{
      const parts=alt.split('|'); let width='';
      if(parts.length>1&&/^\d+(x\d+)?$/.test(parts[parts.length-1].trim())) width=parts.pop().trim().split('x')[0];
      return `<span class="md-img" data-img="${mdImages++}"><img src="${url}" alt="${parts.join('|')}"${width?` style="width:${width}px"`:''} loading="lazy"></span>`;
    });
    s=s.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,'<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
    s=s.replace(/\*\*([^*]+)\*\*/g,'<strong>$1</strong>');
    // 斜体要放在加粗后面：先把 ** 收走，剩下的单个 * 才是斜体。~~删除线~~ 一并支持
    s=s.replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g,'$1<em>$2</em>');
    s=s.replace(/~~([^~\n]+)~~/g,'<del>$1</del>');
    return s.replace(/(\d+)/g,(_,i)=>'<code>'+codes[+i]+'</code>');
  }

  // Obsidian 的提示框类型和别名，归成几种颜色
  const CALLOUT_KIND={note:'note',info:'note',todo:'note',abstract:'abstract',summary:'abstract',tldr:'abstract',
    tip:'tip',hint:'tip',important:'tip',success:'success',check:'success',done:'success',question:'question',help:'question',
    faq:'question',warning:'warning',caution:'warning',attention:'warning',failure:'danger',fail:'danger',missing:'danger',
    danger:'danger',error:'danger',bug:'danger',example:'example',quote:'quote',cite:'quote'};
  const TABLE_SEP=/^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$/;
  const FENCE=/^\s*(`{3,}|~{3,})/;
  function tableCells(row){
    let s=row.trim();
    if(s.startsWith('|')) s=s.slice(1);
    if(s.endsWith('|')&&!s.endsWith('\\|')) s=s.slice(0,-1);
    return s.split(/(?<!\\)\|/).map(cell=>cell.trim().replace(/\\\|/g,'|'));
  }

  function md(src){
    mdImages=0;
    return '<div class="md-doc">'+mdBlocks(String(src||'').replace(/\r\n?/g,'\n').split('\n'))+'</div>';
  }

  function mdBlocks(lines){
    const out=[], lists=[];              // lists: 当前打开的列表栈 {indent, tag}
    let para=[], quote=[];
    const flushPara=()=>{ if(para.length){ out.push('<p>'+mdInline(para.join(' '))+'</p>'); para=[]; } };
    const flushQuote=()=>{ if(quote.length){ out.push('<blockquote>'+quote.map(mdInline).join('<br>')+'</blockquote>'); quote=[]; } };
    const closeLists=(indent)=>{ while(lists.length && lists[lists.length-1].indent>=indent) out.push('</li></'+lists.pop().tag+'>'); };
    const flushAll=()=>{ flushPara(); flushQuote(); closeLists(0); };

    for(let i=0;i<lines.length;i++){
      const line=lines[i].replace(/\s+$/,'');

      // 整行的 HTML 注释不显示。编辑器把表格列宽行高存成 <!-- fecho-table ... -->
      // 跟在表格后面，看日报时不该冒出来
      if(/^<!--[\s\S]*-->$/.test(line.trim())) continue;

      // 代码块：原样显示，里面的符号一律不当标记
      const fence=line.match(FENCE);
      if(fence){
        flushAll(); const body=[];
        for(i++;i<lines.length&&!lines[i].trim().startsWith(fence[1]);i++) body.push(lines[i]);
        out.push(`<pre class="md-code"><code>${esc(body.join('\n'))}</code></pre>`);
        continue;
      }
      if(!line.trim()){ flushAll(); continue; }

      const h=line.match(/^(#{1,6})\s+(.*)$/);
      if(h){ flushAll(); const n=h[1].length; out.push(`<h${n} class="md-h">${mdInline(h[2])}</h${n}>`); continue; }

      if(/^\s*[─—-]{3,}\s*$/.test(line)){ flushAll(); out.push('<hr>'); continue; }

      // 表格：这一行带 |，下一行是 |---|
      if(line.includes('|')&&i+1<lines.length&&lines[i+1].includes('|')&&TABLE_SEP.test(lines[i+1])){
        flushAll();
        const head=tableCells(line);
        const align=tableCells(lines[i+1]).map(c=>/^:-+:$/.test(c)?'center':/-+:$/.test(c)?'right':'');
        const cell=(tag,text,k)=>`<${tag}${align[k]?` style="text-align:${align[k]}"`:''}>${mdInline(text)}</${tag}>`;
        const rows=[];
        for(i+=2;i<lines.length&&lines[i].includes('|')&&lines[i].trim();i++) rows.push(tableCells(lines[i]));
        i--;
        out.push(`<div class="md-table"><table><thead><tr>${head.map((c,k)=>cell('th',c,k)).join('')}</tr></thead><tbody>${rows.map(r=>`<tr>${head.map((_,k)=>cell('td',r[k]||'',k)).join('')}</tr>`).join('')}</tbody></table></div>`);
        continue;
      }

      // 提示框：> [!note] 标题；带 - 默认折叠，带 + 默认展开
      const callout=!quote.length&&line.match(/^>\s?\[!(\w+)\]([+-]?)\s*(.*)$/);
      if(callout){
        flushPara(); closeLists(0);
        const body=[];
        for(i++;i<lines.length&&/^>/.test(lines[i]);i++) body.push(lines[i].replace(/^>\s?/,''));
        i--;
        const name=callout[1], kind=CALLOUT_KIND[name.toLowerCase()]||'note';
        const title=callout[3]?mdInline(callout[3]):esc(name.charAt(0).toUpperCase()+name.slice(1).toLowerCase());
        const inner=body.some(l=>l.trim())?`<div class="callout-body">${mdBlocks(body)}</div>`:'';
        out.push(callout[2]
          ?`<details class="callout c-${kind}"${callout[2]==='+'?' open':''}><summary class="callout-title">${title}</summary>${inner}</details>`
          :`<div class="callout c-${kind}"><div class="callout-title">${title}</div>${inner}</div>`);
        continue;
      }

      const q=line.match(/^>\s?(.*)$/);
      if(q){ flushPara(); closeLists(0); quote.push(q[1]); continue; }

      // 有人习惯先写勾选框再写编号：`[ ] 1. 内容`。先折成和 `1. [ ] 内容`
      // 一样的内部结构，后面的编号、待办和嵌套逻辑就能完全复用。
      const taskFirst=line.match(/^(\s*)\[(.)\]\s+(\d+)\.\s*(.*)$/);
      const li=taskFirst?[taskFirst[0],taskFirst[1],taskFirst[3],`[${taskFirst[2]}] ${taskFirst[4]}`]
        :(line.match(/^(\s*)(\d+)\.\s+(.*)$/) || line.match(/^(\s*)([*\-•])\s+(.*)$/));
      if(li){
        flushPara(); flushQuote();
        const indent=li[1].replace(/\t/g,'    ').length, ordered=/^\d+$/.test(li[2]), tag=ordered?'ol':'ul';
        closeLists(indent+1);            // 比这一项缩进更深的都收掉
        const top=lists[lists.length-1];
        // 待办：[ ] 没做；[x] 做完（划掉）；方括号里放别的字符（[/] [-] [a]…）打勾但不划掉，和 Obsidian 一样
        const task=li[3].match(/^\[(.)\](?:\s+(.*))?$/);
        const mark=task&&task[1];
        const cls=!task?'':mark===' '?'task':/^[xX]$/.test(mark)?'task checked done':'task checked';
        const open=cls?`<li class="${cls}" data-task="${esc(mark)}">`:'<li>';
        if(top && top.indent===indent && top.tag===tag){ out.push('</li>'+open); }
        else{
          if(top && top.indent===indent) out.push('</li></'+lists.pop().tag+'>');
          // 保留原编号：Blocked 下面的「2.」不能被重新数成「1.」
          out.push(`<${tag}${ordered&&li[2]!=='1'?` start="${li[2]}"`:''}>`+open);
          lists.push({indent, tag});
        }
        out.push(task?`<span class="task-box"></span><span class="task-text">${mdInline(task[2]||'')}</span>`:mdInline(li[3]));
        continue;
      }

      if(lists.length){ out.push(' '+mdInline(line.trim())); continue; }   // 列表项的续行
      flushQuote(); para.push(line.trim());
    }
    flushAll();
    return out.join('');
  }

  // 有子项的列表项、标题：左边加折叠箭头（悬停出现，和 Obsidian 一样）
  // 折起来之后在后面显示「…」，提醒这里藏着东西
  function enhanceMd(root){
    if(!root) return;
    const button=`<button type="button" class="fold" aria-expanded="true" aria-label="${esc(t('fold'))}"></button>`;
    const more='<span class="fold-more" aria-hidden="true">…</span>';
    root.querySelectorAll('li').forEach(li=>{
      const child=li.querySelector(':scope>ul,:scope>ol');
      if(child&&!li.querySelector(':scope>.fold')){ li.classList.add('foldable'); li.insertAdjacentHTML('afterbegin',button); child.insertAdjacentHTML('beforebegin',more); }
    });
    root.querySelectorAll('.md-h').forEach(h=>{ if(!h.querySelector(':scope>.fold')){ h.insertAdjacentHTML('afterbegin',button); h.insertAdjacentHTML('beforeend',more); } });
  }
  // 直接记在父 li 的那一层：里面某个 li 已经折起来，也不会影响这一级整体收起或展开。
  function setListFold(host,folded){
    const child=host.querySelector(':scope>ul,:scope>ol');
    if(!child) return false;
    host.classList.toggle('folded',folded);
    child.hidden=folded;
    const button=host.querySelector(':scope>.fold');
    if(button) button.setAttribute('aria-expanded',String(!folded));
    return true;
  }
  // 折起一个标题 = 藏起它后面、直到下一个同级或更高级标题之前的内容。里面折着的小标题展开外层后仍保持折着
  function applyHeadingFolds(container){
    let hide=null;
    for(const el of container.children){
      const m=/^H([1-6])$/.exec(el.tagName), level=m?+m[1]:null;
      if(level!==null&&hide!==null&&level<=hide) hide=null;
      el.classList.toggle('fold-hidden',hide!==null);
      if(level!==null&&hide===null&&el.classList.contains('folded')) hide=level;
    }
  }
  // ---- 看日报时记住折叠状态：按人、日期、标签页各记一份；内容变了就作废，不把折叠套到别的地方 ----
  const FOLD_KEY='fecho-folds';
  const foldKey=()=>cacheKey(state.data.date)+'|'+state.reportTab;
  function hashText(s){ let h=0; for(let i=0;i<s.length;i++) h=(h*31+s.charCodeAt(i))|0; return String(h); }
  function foldHosts(root){ return [...root.querySelectorAll('.fold')].map(button=>button.parentElement); }
  function readFolds(){ try{ return JSON.parse(localStorage.getItem(FOLD_KEY)||'{}')||{}; }catch(_){ return {}; } }
  function saveFolds(){
    const root=$('#report-content'); if(!root||state.editing) return;
    const folded=foldHosts(root).map((host,i)=>host.classList.contains('folded')?i:-1).filter(i=>i>=0);
    const all=readFolds(), key=foldKey();
    delete all[key];
    if(folded.length) all[key]={sig:state.reportSig, folded};
    const keys=Object.keys(all); keys.slice(0,Math.max(0,keys.length-60)).forEach(k=>delete all[k]);   // 只留最近 60 份
    try{ localStorage.setItem(FOLD_KEY,JSON.stringify(all)); }catch(_){}
  }
  function restoreFolds(root){
    const saved=readFolds()[foldKey()];
    if(!saved||saved.sig!==state.reportSig) return;
    const hosts=foldHosts(root);
    (saved.folded||[]).forEach(i=>{ const host=hosts[i]; if(!host) return; if(host.tagName==='LI') setListFold(host,true); else { host.classList.add('folded'); host.querySelector(':scope>.fold').setAttribute('aria-expanded','false'); } });
    root.querySelectorAll('.md-doc,.callout-body').forEach(applyHeadingFolds);
  }

  // ---- 改日报的编辑框：和 Obsidian 一样，回车延续列表、Tab 缩进、编号自动重排 ----
  // 这几个函数只算文字，不碰页面，方便单独测
  function openLightbox(src, alt){
    let box=$('#lightbox');
    if(!box){ box=document.createElement('div'); box.id='lightbox'; box.className='lightbox'; box.setAttribute('role','dialog'); box.setAttribute('aria-modal','true'); box.innerHTML='<img alt="">'; document.body.appendChild(box); }
    box.setAttribute('aria-label',t('img_view'));
    const img=box.querySelector('img'); img.src=src; img.alt=alt||'';
    box.classList.add('show');
  }
  function closeLightbox(){ const box=$('#lightbox'); if(box) box.classList.remove('show'); }
  async function uploadReportImages(files){
    if(state.viewAs){ toast(t('toast_readonly'),null,true); return; }
    for(const file of files){
      try{
        if(!FB_TYPES.test(file.type)) throw new Error(t('fb_bad_type'));
        toast(t('img_uploading'));
        const blob=file.size<=FB_TARGET_BYTES?file:await shrinkImage(file);
        const dataUrl=await readAsDataURL(blob);
        const r=await post('/api/reports/images',{data:dataUrl.split(',')[1]});
        FechoEditor.insert(`![${t('img_alt')}|480](${r.url})`);
        toast(t('img_inserted'));
      }catch(error){ toast(error.message,null,true); }
    }
  }


  // ---------- 快速 API：签发一把钥匙并复制 ----------
  function toggleQuickApiHelp(){
    const help=$('#quick-api-help-text'), anchor=$('#quick-api-help');
    if(!help||!anchor) return;
    if(!help.hidden){ help.hidden=true; return; }
    help.innerHTML=t('quick_api_help');                 // 文案里有 <b>，不转义
    if(help.parentElement!==document.body) document.body.appendChild(help);
    const box=anchor.getBoundingClientRect();
    const width=Math.min(250,Math.max(180,innerWidth-16));
    help.style.width=width+'px';
    help.style.left=Math.min(Math.max(8,box.left),Math.max(8,innerWidth-width-8))+'px';
    help.style.top=(box.bottom+8)+'px';
    help.hidden=false;
    const shown=help.getBoundingClientRect();           // 贴着屏幕底就往上翻
    if(shown.bottom>innerHeight-8) help.style.top=Math.max(8,innerHeight-shown.height-8)+'px';
  }
  const quickApiText=d=>`Base URL: ${d.base_url}\nAPI Key: ${d.api_key}\nOpenAPI: ${d.openapi_url}\nAuthorization: Bearer ${d.api_key}`;
  function copyTextFallback(text){
    const area=document.createElement('textarea');
    area.value=text; area.setAttribute('readonly','');
    area.style.position='fixed'; area.style.opacity='0';
    document.body.appendChild(area); area.focus(); area.select();
    const ok=document.execCommand('copy'); area.remove();
    if(!ok) throw new Error(t('err_operation'));
  }
  async function copyQuickApi(){
    const help=$('#quick-api-help-text'); if(help) help.hidden=true;
    if(state.viewAs){ toast(t('toast_readonly'),null,true); return; }
    const button=$('#quick-api-open'); if(button) button.disabled=true;
    const pending=post('/api/quick-api/credentials',{});
    try{
      let copied=false;
      // 剪贴板的授权只在点击那一刻有效。所以先把一个 Promise 交给 ClipboardItem，
      // 等请求回来它自己填进去——先 await 再写就已经过期了
      if(navigator.clipboard&&navigator.clipboard.write&&window.ClipboardItem){
        try{
          const blob=pending.then(d=>new Blob([quickApiText(d)],{type:'text/plain'}));
          await navigator.clipboard.write([new ClipboardItem({'text/plain':blob})]);
          copied=true;
        }catch(_){}
      }
      if(!copied){
        const text=quickApiText(await pending);
        if(navigator.clipboard&&navigator.clipboard.writeText){
          try{ await navigator.clipboard.writeText(text); copied=true; }catch(_){}
        }
        if(!copied) copyTextFallback(text);
      }
      await pending;                                     // 请求失败要报出来，不能假装复制成功
      toast(t('quick_api_copied'));
    }catch(error){ toast(error.message||t('err_operation'),null,true); }
    finally{ if(button) button.disabled=false; }
  }

  // ---------- 出日报进度 ----------
  // 云端点了生成只是排进队，以前只说一句「已排队」，然后就没下文了：卡在排队、卡在模型、
  // 后台根本没在跑，看起来都一样。这里每几秒问一次做到哪一步了。
  const GEN_POLL_MS=3000, GEN_SHOW_DONE_MS=15*60*1000, GEN_WORKER_STALE_S=150, GEN_SLOW_DAILY_S=180;
  const gen={date:null,status:null,timer:null,tick:null,checkedAt:0,skew:0,dismissed:null,local:null};
  const isoMs=v=>v?Date.parse(v):NaN;
  const clockText=sec=>{ sec=Math.max(0,Math.round(sec)); const m=Math.floor(sec/60); return m>=60?`${Math.floor(m/60)}:${String(m%60).padStart(2,'0')}:${String(sec%60).padStart(2,'0')}`:`${m}:${String(sec%60).padStart(2,'0')}`; };
  const serverNow=()=>Date.now()-gen.skew;
  function genActive(st){ return !!(st&&st.job&&(st.job.status==='queued'||st.job.status==='running')); }
  async function checkGen(force=false){
    if(!(state.me&&state.me.cloud)||state.viewAs||!state.data) return;
    const date=state.data.date;
    if(!force&&gen.date===date&&(gen.timer||Date.now()-gen.checkedAt<20000)) return;
    gen.checkedAt=Date.now();
    let st; try{ st=await request(`/api/jobs/status?date=${encodeURIComponent(date)}`); }catch(_){ return; }
    if(state.data.date!==date) return;                                   // 人已经切到别的日期了
    const was=gen.date===date&&genActive(gen.status);
    gen.date=date; gen.status=st; gen.skew=Date.now()-isoMs(st.now);
    renderGen();
    if(genActive(st)){ if(!gen.timer) gen.timer=setInterval(()=>checkGen(true),GEN_POLL_MS); }
    else{ clearInterval(gen.timer); gen.timer=null; if(was) loadDashboard({quiet:true}); }   // 刚做完：把新日报拉回来
  }
  function renderGen(){
    const box=$('#gen-progress'), st=gen.status, job=st&&st.job, now=serverNow();
    clearInterval(gen.tick); gen.tick=null;
    box.classList.remove('warn','bad','ok');
    if(gen.local){                                                        // 本机版：一次请求里同步做完，只能显示等了多久
      box.hidden=false;
      box.innerHTML=`<div class="gen-head"><span class="spinner" aria-hidden="true"></span><b>${esc(t('gen_local'))}</b><span class="gen-time">${clockText((Date.now()-gen.local)/1000)}</span></div>`;
      gen.tick=setInterval(renderGen,1000); return;
    }
    if(!job||gen.date!==(state.data&&state.data.date)||gen.dismissed===job.job_id){ box.hidden=true; return; }
    const finished=job.finished_at&&now-isoMs(job.finished_at)<GEN_SHOW_DONE_MS;
    if(!genActive(st)&&!finished){ box.hidden=true; return; }
    const stages=st.stages||[], close=`<button class="gen-close" type="button" data-gen-close aria-label="${esc(t('gen_close'))}">×</button>`;
    const bar=(cur,done)=>`<div class="gen-steps">${stages.map((s,i)=>`<div class="gen-step ${done||i<cur?'done':i===cur?'now':''}"></div>`).join('')}</div>`+
      `<div class="gen-labels">${stages.map((s,i)=>`<span class="${done||i<cur?'done':i===cur?'now':''}">${esc(t('gen_stage_'+s))}</span>`).join('')}</div>`;
    let html='';
    if(job.status==='queued'&&job.attempts>0&&job.error){
      box.classList.add('bad');
      const at=new Date(isoMs(job.run_after)).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'});
      html=`<div class="gen-head"><b>${esc(t('gen_failed'))}</b><span class="gen-time">${esc(t('gen_retry',{time:at}))}</span></div><div class="gen-note">${esc(job.error)}</div>`;
    }else if(job.status==='queued'){
      const waited=(now-isoMs(job.created_at))/1000, seen=st.worker_seen_at?(now-isoMs(st.worker_seen_at))/1000:Infinity;
      // 后台在忙别的长任务时心跳会稀一点；没活干还不露面才算掉线
      const down=seen>(st.running?600:GEN_WORKER_STALE_S);
      if(down&&!later) box.classList.add('warn');
      const later=isoMs(job.run_after)>now;          // 自动重出：等新进展传完再开跑
      const note=later?t('gen_scheduled',{time:new Date(isoMs(job.run_after)).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'})})
        :down?t('gen_worker_down',{m:Math.max(1,Math.round(seen/60))}):st.queue_ahead?t('gen_queue_ahead',{n:st.queue_ahead}):st.running?t('gen_queue_busy',{n:st.running}):'';
      html=`<div class="gen-head"><span class="spinner" aria-hidden="true"></span><b>${esc(t('gen_queued'))}</b><span class="gen-time">${clockText(waited)}</span></div>${bar(-1,false)}${note?`<div class="gen-note">${esc(note)}</div>`:''}`;
    }else if(job.status==='running'){
      const p=job.progress||{}, cur=Math.max(0,stages.indexOf(p.stage)), stageSec=p.at?(now-isoMs(p.at))/1000:0;
      const slow=p.stage==='daily'&&stageSec>GEN_SLOW_DAILY_S;
      if(slow) box.classList.add('warn');
      html=`<div class="gen-head"><span class="spinner" aria-hidden="true"></span><b>${esc(t('gen_running',{i:cur+1,n:stages.length,stage:t('gen_stage_'+(p.stage||stages[0]))}))}</b>`+
        `<span class="gen-time">${esc(t('gen_step_time',{t:clockText(stageSec)}))} · ${clockText((now-isoMs(job.started_at))/1000)}</span></div>${bar(cur,false)}`+
        (slow?`<div class="gen-note">${esc(t('gen_slow_daily'))}</div>`:p.detail?`<div class="gen-note">${esc(p.detail)}</div>`:'');
    }else{
      const took=(isoMs(job.finished_at)-isoMs(job.started_at||job.created_at))/1000;
      const gener=(st.report&&st.report.generator)||'', fallback=gener.startsWith('fallback');
      if(job.status==='failed'){
        box.classList.add('bad');
        html=`<div class="gen-head"><b>${esc(t('gen_failed'))}</b>${close}</div><div class="gen-note">${esc(job.error||'')}</div>`;
      }else if(fallback){
        box.classList.add('bad');
        const why=((st.report.warnings||[]).find(w=>/LLM/.test(w))||'');
        html=`<div class="gen-head"><b>${esc(t('gen_fallback'))}</b><span class="gen-time">${clockText(took)}</span>${close}</div>${bar(stages.length,true)}${why?`<div class="gen-note">${esc(why)}</div>`:''}`;
      }else{
        // 分批写时个别批失败：整篇不是兜底稿，但那几项是进展原文，得让人知道
        const partial=st.report&&st.report.partial;
        box.classList.add(partial?'warn':'ok');
        html=`<div class="gen-head"><b>${esc(t(partial?'gen_partial':'gen_done',{t:clockText(took)}))}</b>${partial?`<span class="gen-time">${clockText(took)}</span>`:''}${close}</div>${bar(stages.length,true)}${partial?`<div class="gen-note">${esc(partial)}</div>`:''}`;
      }
    }
    box.hidden=false; box.innerHTML=html;
    if(genActive(st)) gen.tick=setInterval(renderGen,1000);             // 秒数在两次查询之间也往前走
  }
  $('#gen-progress').addEventListener('click',event=>{ if(event.target.closest('[data-gen-close]')&&gen.status&&gen.status.job){ gen.dismissed=gen.status.job.job_id; renderGen(); } });

  // ---------- 日报截图 ----------
  // 每天要把日报截图贴进频道，长日报不好长截图。点一下把渲染好的日报整块画成 PNG，
  // 文件名按「0923_worklog_Rachel.png」起好。存哪：每次弹系统的保存框，或者固定存到一个文件夹。
  // 两种都要 Chrome / Edge 的文件系统接口；Safari、Firefox 只能退回普通下载。
  const SNAPSHOT_SRC='/vendor/snapshot/snapshot.min.js';
  const canPickFile=typeof window.showSaveFilePicker==='function', canPickDir=typeof window.showDirectoryPicker==='function';
  function snapMode(){ try{ return localStorage.getItem('fecho-snap-mode')==='folder'&&canPickDir?'folder':'ask'; }catch(_){ return 'ask'; } }
  function setSnapMode(mode){ try{ localStorage.setItem('fecho-snap-mode',mode); }catch(_){} }
  // 文件夹的句柄只能存在浏览器的 IndexedDB 里（存不进 localStorage，也不该上服务器）
  function snapDb(){ return new Promise((ok,fail)=>{ const r=indexedDB.open('fecho-snap',1); r.onupgradeneeded=()=>r.result.createObjectStore('kv'); r.onsuccess=()=>ok(r.result); r.onerror=()=>fail(r.error); }); }
  async function snapDir(value){
    try{
      const db=await snapDb(), tx=db.transaction('kv',value===undefined?'readonly':'readwrite'), store=tx.objectStore('kv');
      if(value!==undefined){ store.put(value,'dir'); return new Promise(ok=>{ tx.oncomplete=()=>ok(value); tx.onerror=()=>ok(null); }); }
      return await new Promise(ok=>{ const g=store.get('dir'); g.onsuccess=()=>ok(g.result||null); g.onerror=()=>ok(null); });
    }catch(_){ return null; }
  }
  function snapFileName(){
    const d=(state.data&&state.data.date)||localDate(), me=state.me||{};
    let name=(me.display_name||me.author||'').trim();
    if(name.includes('@')) name=name.split('@')[0].split(/[._-]/)[0];
    name=(name.split(/\s+/)[0]||'me').replace(/[\\/:*?"<>|]/g,'');
    name=name.charAt(0).toUpperCase()+name.slice(1);
    return `${d.slice(5,7)}${d.slice(8,10)}_worklog_${name}.png`;
  }
  let snapshotLib=null;
  function loadSnapshotLib(){
    if(window.FechoSnapshot) return Promise.resolve(window.FechoSnapshot);
    if(!snapshotLib) snapshotLib=new Promise((ok,fail)=>{ const el=document.createElement('script'); el.src=SNAPSHOT_SRC; el.onload=()=>ok(window.FechoSnapshot); el.onerror=()=>{ snapshotLib=null; fail(new Error('snapshot.min.js')); }; document.head.appendChild(el); });
    return snapshotLib;
  }
  // 日报正文自己没底色，颜色来自外面那层面板；不往上找就截出一张透明底的图，贴到深色频道里字都看不清
  function paintedBackground(el){
    for(;el&&el.nodeType===1;el=el.parentElement){
      const bg=getComputedStyle(el).backgroundColor;
      if(bg&&bg!=='transparent'&&!/rgba\([^)]*,\s*0\)$/.test(bg)) return bg;
    }
    return getComputedStyle(document.body).backgroundColor||'#fff';
  }
  async function captureReport(){
    const node=$('#report-content'), lib=await loadSnapshotLib();
    return lib.toPng(node,{background:paintedBackground(node),
      skip:el=>el.classList&&(el.classList.contains('fold')||el.classList.contains('fold-more'))});
  }
  async function writeTo(handle,blob){ const w=await handle.createWritable(); await w.write(blob); await w.close(); }
  async function chooseSnapDir(){
    const dir=await window.showDirectoryPicker({id:'fecho-snap',mode:'readwrite'});
    await snapDir(dir); setSnapMode('folder'); return dir;
  }
  // 先定好存哪，再截图：保存框、选文件夹、授权都得在点击的那一下里弹出来，
  // 隔了一次长截图再弹，浏览器会以「不是用户操作」拒绝。
  async function snapTarget(name){
    if(snapMode()==='folder'){
      let dir=await snapDir();
      if(!dir) dir=await chooseSnapDir();
      // 重开浏览器后权限可能要再点一次「允许」；不给就让人重选
      if((await dir.queryPermission({mode:'readwrite'}))!=='granted'&&(await dir.requestPermission({mode:'readwrite'}))!=='granted') throw new Error(t('snap_denied'));
      return async blob=>{ await writeTo(await dir.getFileHandle(name,{create:true}),blob); return t('snap_saved_folder',{folder:dir.name,name}); };  // 同名覆盖：同一天重截就是要最新那张
    }
    if(canPickFile){
      const handle=await window.showSaveFilePicker({id:'fecho-snap',suggestedName:name,types:[{description:'PNG',accept:{'image/png':['.png']}}]});
      return async blob=>{ await writeTo(handle,blob); return t('snap_saved',{name:handle.name}); };
    }
    return async blob=>{
      const a=document.createElement('a'); a.href=URL.createObjectURL(blob); a.download=name; document.body.appendChild(a); a.click(); a.remove();
      setTimeout(()=>URL.revokeObjectURL(a.href),10000);
      return t('snap_downloaded',{name});
    };
  }
  async function takeSnapshot(){
    const button=$('#snap-report'); button.disabled=true;
    try{
      const save=await snapTarget(snapFileName());
      toast(t('snap_working'));
      toast(await save(await captureReport()));
    }catch(error){
      if(error&&error.name==='AbortError') return;          // 人自己在保存框里点了取消
      toast(t('snap_failed',{error:error.message||String(error)}),null,true);
    }finally{ button.disabled=false; }
  }
  async function renderSnapPopover(){
    const pop=$('#snap-popover'), mode=snapMode(), dir=canPickDir?await snapDir():null;
    pop.innerHTML=`<h4>${esc(t('snap_title'))}</h4>`+(canPickFile||canPickDir?
      `<label><input type="radio" name="snap-mode" value="ask" ${mode==='ask'?'checked':''}><span>${esc(t('snap_ask'))}</span></label>`+
      (canPickDir?`<label><input type="radio" name="snap-mode" value="folder" ${mode==='folder'?'checked':''}><span>${esc(t('snap_folder'))}</span></label>
        <div class="snap-folder"><b>${esc(dir?dir.name:t('snap_no_folder'))}</b><button class="btn small" type="button" id="snap-pick-dir">${esc(t(dir?'snap_change':'snap_pick'))}</button></div>`:'')
      :`<p class="snap-note">${esc(t('snap_only_download'))}</p>`)+
      `<p class="snap-note">${esc(t('snap_name',{name:snapFileName()}))}</p>`;
  }
  function toggleSnapPopover(open){
    const pop=$('#snap-popover'); open=open===undefined?pop.hidden:open;
    pop.hidden=!open; $('#snap-settings').setAttribute('aria-expanded',String(open));
    if(open) renderSnapPopover();
  }
  $('#snap-report').addEventListener('click',takeSnapshot);
  $('#snap-settings').addEventListener('click',event=>{ event.stopPropagation(); toggleSnapPopover(); });
  $('#snap-popover').addEventListener('change',async event=>{
    const r=event.target.closest('input[name="snap-mode"]'); if(!r) return;
    if(r.value==='folder'&&!(await snapDir())){ try{ await chooseSnapDir(); }catch(_){ setSnapMode('ask'); } }
    else setSnapMode(r.value);
    renderSnapPopover();
  });
  $('#snap-popover').addEventListener('click',async event=>{
    if(!event.target.closest('#snap-pick-dir')) return;
    try{ await chooseSnapDir(); }catch(_){}
    renderSnapPopover();
  });
  document.addEventListener('click',event=>{ const pop=$('#snap-popover'); if(!pop.hidden&&!event.target.closest('.snap-group')) toggleSnapPopover(false); });
  document.addEventListener('keydown',event=>{ if(event.key==='Escape'&&!$('#snap-popover').hidden) toggleSnapPopover(false); });

  function renderReports(){
    const report=state.data.reports; $('#report-dirty').classList.toggle('show',report.dirty);
    $('#report-dirty-copy').textContent=report.generator==='human' ? t('dirty_human') : t('dirty_llm');
    // 还没生成也能先手写：按钮从「修改」变成「手动写日报」
    $('#edit-report').hidden=!!state.viewAs||state.editing;
    $('#snap-group').hidden=state.editing||state.reportTab!=='daily'||!report.daily;
    $('#edit-report').textContent=t(report.daily?'edit':'write_report');
    // 正在改的时候，每分钟的自动刷新不能把编辑框重画掉——写了一半的字会没
    if(state.editing&&state.reportTab==='daily'&&$('#report-editor')) return;
    $$('#report-tabs button').forEach(button=>button.classList.toggle('active',button.dataset.reportTab===state.reportTab));
    const empty=text=>`<p class="report-empty">${esc(text)}</p>`;
    let html='';
    // 没有日报时要说清楚是哪种「没有」：这天根本没收到记录，重新生成也生成不出东西；
    // 有记录只是还没生成，才值得去点生成。
    const updates=(state.data.overview||{}).updates||0;
    const noDaily=updates ? t('no_daily_with_updates',{n:updates}) : t('no_daily_empty');
    if(state.reportTab==='daily') html=state.editing
      ?`<div class="report-edit"><p class="meta edit-hint">${esc(t(report.daily?'edit_hint':'write_hint'))}</p><div class="report-editor" id="report-editor" aria-label="${esc(t('edit_aria'))}"></div><input type="file" id="report-image-file" accept="image/png,image/jpeg,image/webp,image/gif" multiple hidden><div class="button-row"><button class="btn small" type="button" id="insert-image">${esc(t('img_insert'))}</button><span class="md-hint" title="${esc(t('md_hint_more'))}">${esc(t('md_hint'))}</span><button class="btn" id="cancel-edit">${esc(t('cancel'))}</button><button class="btn primary" id="save-edit">${esc(t('save'))}</button></div></div>`
      :(report.daily?md(report.daily):empty(noDaily));
    if(state.reportTab==='voice') html=report.voice?md(report.voice):empty(t('no_voice'));
    if(state.reportTab==='history') html=report.history.length
      ?report.history.map((item,index)=>`<div class="report-version">${esc(t('version_label',{n:index+1, kind:item.kind, by:t(item.generator==='human'?'by_human':'by_model'), time:stamp(item.created_at)}))}</div>${md(item.content_md)}`).join('<hr>')
      :empty(t('no_history'));
    if(state.reportTab==='warnings') html=report.warnings.length
      ?'<ul>'+report.warnings.map(text=>`<li>${esc(text)}</li>`).join('')+'</ul>'
      :empty(t('no_warnings'));
    // 内容没变就不重画：每分钟的自动刷新不能把人折起来的标题和列表又全展开
    if(state.reportHtml===html) return;
    state.reportHtml=html;
    state.reportSig=hashText(html);
    FechoEditor.destroy();                    // 重画前先把上一个编辑器拆掉，不然事件和 DOM 都留着
    $('#report-content').innerHTML=html;
    if(state.editing&&state.reportTab==='daily'){
      FechoEditor.create($('#report-editor'), report.daily||t('write_template',{date:state.data.date}),
                         {onImages:uploadReportImages});
      FechoEditor.focus();
    }
    else { enhanceMd($('#report-content')); restoreFolds($('#report-content')); }
  }

  function renderSystem(){
    const doctor=state.data.system.doctor;
    $('#doctor').innerHTML=`<div class="item-title">${esc(t('health_check'))}</div><div class="meta" style="margin-bottom:8px">Fecho ${esc(doctor.version||'')} · ${esc(t('local_first'))}</div>`+doctor.checks.map(check=>`<div class="check ${check.ok?'':'bad'}"><span class="check-mark">${check.ok?'✓':'!'}</span><div><div class="item-title">${esc(check.name)}</div><div class="meta">${esc(check.detail)}</div>${check.fix?`<div class="meta">${esc(t('suggestion',{fix:check.fix}))}</div>`:''}</div></div>`).join('');
    // 云端版没有「你电脑上的定时任务」，后端会把 automation 给成 null。
    // 这时候别照本机的说法报「尚未安装自动任务」——那是句永远修不好的假警报。
    if(state.data.system.automation===null){
      // 检查项的名字是服务器给的（中文），按它找那一项
      const dt=(doctor.checks.find(c=>c.name==='每日自动整理')||{}).detail||'';
      $('#automation-status').innerHTML=`<div class="item-title">${esc(t('daily_automation'))}</div><div class="item"><div class="item-body">${esc(dt)}</div><div class="meta">${esc(t('timezone',{tz:'Asia/Shanghai'}))}</div></div>`;
    } else {
    const automation=state.data.system.automation||{}, last=automation.last_result||{};
    const configured=automation.configured_launch_agents||{}, runtime=automation.runtime||{};
    const dashboardRuntime=runtime['com.feedmob.fecho.dashboard']||{};
    const dashboardState=dashboardRuntime.ok?t('dash_ok'):configured['com.feedmob.fecho.dashboard']?t('dash_bad'):t('dash_none');
    $('#automation-status').innerHTML=`<div class="item-title">${esc(t('daily_automation'))}</div><div class="item"><div class="item-body">${esc(automation.enabled?t('auto_daily',{time:automation.daily_time}):t('auto_not_installed'))}</div><div class="meta">${esc(t('timezone',{tz:automation.timezone||'Asia/Shanghai'}))} · ${esc(t('dashboard_state',{state:dashboardState}))}</div></div><div class="item"><div class="meta">${esc(t('last_run'))}</div><div class="item-body">${last.status?`${esc(t(last.status==='succeeded'?'succeeded':'failed'))} · ${esc(last.date||'')} ${last.error?'· '+esc(last.error):''}`:esc(t('no_runs'))}</div></div>`;
    }
    const filters=state.data.filters;
    $('#sources').innerHTML=`<div class="item-title">${esc(t('sources'))}</div><div class="item"><div class="meta">Agent</div><div style="margin-top:7px">${filters.agents.map(a=>`<span class="tag">${esc(a)}</span>`).join(' ')||esc(t('none'))}</div></div><div class="item"><div class="meta">${esc(t('via'))}</div><div style="margin-top:7px">${filters.ingestion_methods.map(a=>`<span class="tag">${esc(a)}</span>`).join(' ')||esc(t('none'))}</div></div>`;
    const runs=state.data.system.scan_runs;
    $('#scan-runs').innerHTML=runs.length?runs.map(run=>`<tr><td>${esc((run.started_at||'').slice(0,16).replace('T',' '))}</td><td>${esc(run.producer_agent)}</td><td>${esc(run.project.split('/').pop())}</td><td>${run.status==='succeeded'?`<span class="tag done">${esc(t('succeeded'))}</span>`:`<span class="tag blocked">${esc(t('failed'))}</span>`}</td><td>${run.error?esc(run.error):esc(t('scan_result',{entries:run.entries, chunks:run.chunks}))}</td></tr>`).join(''):`<tr><td colspan="5">${esc(t('no_scans'))}</td></tr>`;
  }

  function switchView(view,{scroll=true}={}){ if(view!==state.view) state.prevView=state.view; state.view=view; $$('.nav button').forEach(b=>b.classList.toggle('active',b.dataset.view===view)); $$('.view').forEach(p=>p.classList.toggle('active',p.dataset.page===view)); $('.toolbar').hidden=['settings','admin','feedback'].includes(view); $('#page-title').textContent=t('page_'+view); $('#page-subtitle').textContent=t('page_'+view+'_sub'); if(scroll) window.scrollTo({top:0,behavior:'smooth'}); }
  function showError(message){ $('#error-copy').textContent=message; $('#error-state').classList.add('show'); }
  function toast(message, undo=null, isError=false){ clearTimeout(state.toastTimer); state.undo=undo; $('#toast-copy').textContent=message; $('#undo-action').style.display=undo?'block':'none'; $('#toast').classList.toggle('error',isError); $('#toast').classList.add('show'); state.toastTimer=setTimeout(()=>$('#toast').classList.remove('show'),6000); }
  async function mutate(path, body, success, undo=null){
    if(state.viewAs){ toast(t('toast_readonly'),null,true); throw new Error(t('toast_readonly')); }
    try{ const result=await post(path,body); if(result.dashboard){state.data=result.dashboard;renderAll();}else await loadDashboard({quiet:true}); toast(success,undo); return result; }
    catch(error){ toast(error.message,null,true); throw error; }
  }

  $('#account-trigger').addEventListener('click',toggleAccountMenu);
  // 合并目标输完直接回车，等于点旁边的「合并」
  $('#task-list').addEventListener('keydown',event=>{
    const input=event.target.closest('.merge-input'); if(!input||event.isComposing) return;
    const open=state.mergeMenu&&state.mergeMenu.input===input;
    if(event.key==='ArrowDown'||event.key==='ArrowUp'){ event.preventDefault(); if(!open) openMergeMenu(input); else moveMergeActive(event.key==='ArrowDown'?1:-1); return; }
    if(event.key==='Escape'&&open){ event.preventDefault(); event.stopPropagation(); closeMergeMenu(); return; }
    if(event.key!=='Enter') return;
    event.preventDefault();
    if(open){ pickMergeOption(state.mergeMenu.active); return; }            // 先选中，再回车才合并
    const b=$(`[data-action="merge-task"][data-id="${input.dataset.mergeTarget}"]`); if(b) b.click();
  });
  $('#task-list').addEventListener('focusin',event=>{ const input=event.target.closest('.merge-input'); if(input) openMergeMenu(input); });
  $('#task-list').addEventListener('input',event=>{ const input=event.target.closest('.merge-input'); if(input) openMergeMenu(input); });
  $('#task-list').addEventListener('focusout',event=>{ if(event.target.closest('.merge-input')) setTimeout(()=>{ const m=state.mergeMenu; if(m&&document.activeElement!==m.input) closeMergeMenu(); },0); });
  // 按下就选：mousedown 时阻止默认行为，输入框不失焦
  $('#task-list').addEventListener('mousedown',event=>{ const opt=event.target.closest('.merge-option'); if(!opt) return; event.preventDefault(); pickMergeOption(+opt.dataset.i); });
  $('#account-menu').addEventListener('click',event=>{
    const action=event.target.closest('[data-account-action]'); if(!action) return;
    closeAccountMenu();
    if(action.dataset.accountAction==='settings') switchView('settings');
  });
  $('#account-logout').addEventListener('click',async()=>{
    if(!confirm(t('account_logout_confirm'))) return;
    const button=$('#account-logout'); button.disabled=true;
    try{ await request('/auth/logout',{method:'POST'}); window.location.assign('/login'); }
    catch(error){ button.disabled=false; toast(error.message,null,true); }
  });
  document.addEventListener('click',event=>{ if(state.accountMenuOpen&&!event.target.closest('#account-menu')) closeAccountMenu(); });

  document.addEventListener('click', async event=>{
    const filterOption=event.target.closest('[data-filter-option]'), filterTrigger=event.target.closest('[data-filter-trigger]');
    if(!filterOption&&!filterTrigger&&!event.target.closest('.filter-select')) closeFilterMenu();
    if(filterOption){ const select=$('#'+filterOption.dataset.filterOption); if(select) chooseFilterOption(select,+filterOption.dataset.filterIndex); return; }
    if(filterTrigger){ const select=$('#'+filterTrigger.dataset.filterTrigger); if(select){ state.filterMenu.open===select.id?closeFilterMenu({focus:true}):openFilterMenu(select); } return; }
    const help=$('#quick-api-help-text');
    if(help&&!event.target.closest('.quick-api-entry')) help.hidden=true;
    const group=event.target.closest('[data-group]');
    if(group){
      event.preventDefault();
      const open=state.settings.openGroups;
      open.has(group.dataset.group)?open.delete(group.dataset.group):open.add(group.dataset.group);
      renderFolders(); return;
    }
    const pickAll=event.target.closest('[data-pick-all]'), pickNone=event.target.closest('[data-pick-none]');
    if(pickAll||pickNone){
      event.preventDefault();
      const key=(pickAll||pickNone).dataset[pickAll?'pickAll':'pickNone'];
      state.settings.folders.filter(f=>folderGroup(f.path)===key).forEach(f=>{ f.selected=!!pickAll; });
      renderFolders(); return;
    }
    const drop=event.target.closest('[data-drop]');
    if(drop){
      event.preventDefault();
      // 点在组头上就是整组一起移除——扫出来一堆 Codex/日期 目录，一个个点太累
      const key=drop.dataset.drop;
      const inGroup=(state.settings.folders||[]).filter(f=>folderGroup(f.path)===key);
      const paths=inGroup.length>1?inGroup.map(f=>f.path):[key];
      dropFolder(paths,false); return;
    }
    const back=event.target.closest('[data-restore]');
    if(back){ event.preventDefault(); dropFolder(back.dataset.restore,true); return; }
    if(event.target.closest('#folders-gone-toggle')){
      state.settings.showDropped=!state.settings.showDropped; renderHiddenFolders(); return;
    }
    if(event.target.closest('#quick-api-open')){ copyQuickApi(); return; }
    if(event.target.closest('#quick-api-help')){ toggleQuickApiHelp(); return; }
    const fold=event.target.closest('.report-paper .fold');
    if(fold){ const host=fold.parentElement; if(host.tagName==='LI') setListFold(host,!host.classList.contains('folded')); else { host.classList.toggle('folded'); fold.setAttribute('aria-expanded',String(!host.classList.contains('folded'))); if(host.classList.contains('md-h')) applyHeadingFolds(host.parentElement); } saveFolds(); return; }
    const zoom=event.target.closest('.report-paper .md-img img'); if(zoom){ openLightbox(zoom.currentSrc||zoom.src, zoom.alt); return; }
    if(event.target.closest('#lightbox')){ closeLightbox(); return; }
    if(event.target.closest('#insert-image')){ $('#report-image-file').click(); return; }
    if(event.target.closest('#sidebar-toggle')){
      applySidebar(!$('.shell').classList.contains('sidebar-collapsed')); return;
    }
    if(event.target.closest('#lang-toggle')){ setLang(lang==='zh'?'en':'zh'); return; }
    const nav=event.target.closest('[data-view]'); if(nav){switchView(nav.dataset.view);return;}
    const go=event.target.closest('[data-go]'); if(go){switchView(go.dataset.go);return;}
    const viewAs=event.target.closest('[data-view-as]'); if(viewAs){setViewAs(viewAs.dataset.viewAs,viewAs.dataset.name);return;}
    const decide=event.target.closest('[data-decide]');
    if(decide){ decide.disabled=true; try{ await post(`/api/admin/requests/${encodeURIComponent(decide.dataset.decide)}`,{approve:decide.dataset.approve==='1'}); toast(t(decide.dataset.approve==='1'?'toast_approved':'toast_denied')); await loadAdmin(); }catch(error){toast(error.message,null,true);} finally{decide.disabled=false;} return; }
    if(event.target.closest('#request-admin')){ try{ await post('/api/admin/request',{}); state.me=await request('/api/me'); renderSettings(); toast(t('toast_requested')); }catch(error){toast(error.message,null,true);} return; }
    if(event.target.closest('#edit-report')){ if(state.viewAs){toast(t('toast_readonly'),null,true);return;} state.editing=true; state.reportTab='daily'; renderReports(); FechoEditor.focus(); return; }
    if(event.target.closest('#cancel-edit')){ state.editing=false; renderReports(); return; }
    const saveEdit=event.target.closest('#save-edit');
    if(saveEdit){ saveEdit.disabled=true;
      try{ const r=await post('/api/reports/daily',{date:state.data.date,content_md:FechoEditor.value()}); state.editing=false; state.data=r.dashboard; renderAll();
        toast(t(r.saved.status==='unchanged'?'toast_unchanged':r.saved.new?'toast_write_saved':'toast_edit_saved')); if(state.me&&state.me.cloud) loadStyle().catch(()=>{}); }
      catch(error){ saveEdit.disabled=false; toast(error.message,null,true); }
      return; }
    const fbRemove=event.target.closest('[data-fb-remove]'); if(fbRemove){ state.feedback.images.splice(+fbRemove.dataset.fbRemove,1); renderFeedbackDraft(); return; }
    const fbStatus=event.target.closest('[data-fb-status]');
    if(fbStatus){ fbStatus.disabled=true; try{ await post(`/api/admin/feedback/${encodeURIComponent(fbStatus.dataset.id)}`,{status:fbStatus.dataset.fbStatus}); await loadAdminFeedback(); toast(t('fb_status_saved')); }catch(error){ fbStatus.disabled=false; toast(error.message,null,true); } return; }
    const reportTab=event.target.closest('[data-report-tab]'); if(reportTab){state.reportTab=reportTab.dataset.reportTab;renderReports();return;}
    const taskTab=event.target.closest('[data-task-status]'); if(taskTab){state.taskStatus=taskTab.dataset.taskStatus;renderTasks();return;}
    const action=event.target.closest('[data-action]'); if(!action)return;
    action.disabled=true;
    try{
      if(action.dataset.action==='confirm-update'){
        const card=action.closest('[data-update]'), updateId=card.dataset.update, oldIssue=card.dataset.oldIssue;
        const issue=$('[data-role="issue"]',card).value, content=$('[data-role="content"]',card).value;
        await mutate('/api/correct',{update_id:updateId,issue_key:issue,content,date:state.data.date},t('toast_confirmed'),()=>mutate('/api/reassign',{update_id:updateId,issue_key:oldIssue,date:state.data.date},t('toast_undo_assign')));
      }
      if(action.dataset.action==='restore') await mutate('/api/restore',{update_id:action.dataset.id,date:state.data.date},t('toast_restored'));
      if(action.dataset.action==='complete-task') await mutate(`/api/tasks/${action.dataset.id}/complete`,{date:state.data.date},t('toast_completed'));
      if(action.dataset.action==='reopen-task') await mutate(`/api/tasks/${action.dataset.id}/reopen`,{date:state.data.date},t('toast_reopened'));
      if(action.dataset.action==='merge-task'){
        const input=$(`[data-merge-target="${action.dataset.id}"]`), text=input.value.trim();
        if(!text) throw new Error(t('toast_pick_merge'));
        const target=resolveMergeTarget(text);
        if(!target){ toast(t('toast_merge_unknown'),null,true); return; }
        if(target.issue) await mutate(`/api/tasks/${encodeURIComponent(action.dataset.id)}/attach`,{issue_key:target.issue,date:state.data.date},t('toast_attached',{key:target.issue}));
        else await mutate('/api/tasks/merge',{source_task_id:action.dataset.id,target_task_id:target.task,date:state.data.date},t('toast_merged'));
      }
    }catch(error){ if(!$('#toast').classList.contains('show'))toast(error.message,null,true); }
    finally{action.disabled=false;}
  });

  async function regenerate(){
    if(state.viewAs){ toast(t('toast_readonly'),null,true); return; }
    const buttons=[$('#regenerate'),$('#dirty-regenerate')]; buttons.forEach(b=>b.disabled=true);
    toast(t('toast_generating'));
    // 云端版出日报要几分钟，接口只是排了个队——这时候说「已更新」是假话
    const local=!(state.me&&state.me.cloud);
    if(local){ gen.local=Date.now(); renderGen(); }
    try{ const result=await post('/api/regenerate',{date:state.data.date}); gen.local=null; state.data=result.dashboard; renderAll(); toast(t(result.empty?'toast_nothing_to_generate':result.queued?'toast_queued':'toast_generated'),null,!!result.empty); if(result.queued){ gen.dismissed=null; checkGen(true); } }
    catch(error){toast(error.message,null,true)} finally{gen.local=null; renderGen(); buttons.forEach(b=>b.disabled=false)}
  }
  $('#regenerate').addEventListener('click',regenerate); $('#dirty-regenerate').addEventListener('click',regenerate);
  $('#undo-action').addEventListener('click',async()=>{const undo=state.undo;state.undo=null;if(undo)await undo();});
  $('#retry').addEventListener('click',()=>loadDashboard());
  $('#reload-day').addEventListener('click',()=>loadDashboard());
  $('#work-date').value=localDate(); $('#work-date').addEventListener('change',()=>loadDashboard({useCache:true}));
  ['agent-filter','source-filter','status-filter'].forEach(id=>$('#'+id).addEventListener('change',renderAll));
  // 只有看的是今天才自动刷新：旧日期的记录不会自己变，要看最新的点「刷新」
  const refreshToday=()=>{ if(document.visibilityState==='visible'&&isToday($('#work-date').value||localDate())) loadDashboard({quiet:true}); };
  document.addEventListener('visibilitychange',refreshToday);
  window.addEventListener('focus',refreshToday);
  setInterval(refreshToday,60000);
  // 白天/黑夜：记在这台电脑的浏览器里；没选过就跟系统走（<head> 里那段在页面画出来之前就定好了，免得闪一下白）
  const THEME_KEY='fecho-theme';
  const themeTitle=()=>{ $('#theme-toggle').title=t(document.documentElement.dataset.theme==='dark'?'theme_to_light':'theme_to_dark'); };
  $('#theme-toggle').addEventListener('click',()=>{
    const next=document.documentElement.dataset.theme==='dark'?'light':'dark';
    document.documentElement.dataset.theme=next; try{ localStorage.setItem(THEME_KEY,next); }catch(_){}
    themeTitle();
  });
  themeTitle();
  // ---------- 云端版：设置与管理 ----------
  const AGENT_BADGE={'claude-code':'CC','codex':'CX','hermes':'HM','cursor':'CU','grok':'GK'};
  // 服务器存的是 UTC；给人看要换成北京时间，否则傍晚 6 点会显示成上午 10 点
  const stamp=value=>{
    if(!value) return '';
    const d=new Date(value); if(isNaN(d)) return String(value).slice(0,16).replace('T',' ');
    const p=Object.fromEntries(new Intl.DateTimeFormat('en',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hourCycle:'h23'}).formatToParts(d).map(x=>[x.type,x.value]));
    return `${p.year}-${p.month}-${p.day} ${p.hour}:${p.minute}`;
  };
  function scanAt(time){ const [h,m]=time.split(':').map(Number), x=h*60+m-15; return String(Math.floor(x/60)).padStart(2,'0')+':'+String(x%60).padStart(2,'0'); }

  function accountInitials(me){
    const text=String(me.display_name||me.author||'F').trim();
    const words=text.split(/\s+/).filter(Boolean);
    return (words.length>1?words.slice(0,2).map(word=>word[0]).join(''):text.slice(0,2)).toUpperCase();
  }
  function renderAccountMenu(){
    const me=state.me; if(!me) return;
    const name=me.display_name||me.author, email=me.author||'', initials=accountInitials(me);
    $('#account-name').textContent=name; $('#account-email').textContent=email; $('#account-avatar').textContent=initials;
    $$('[data-account-name]').forEach(el=>el.textContent=name);
    $$('[data-account-email]').forEach(el=>el.textContent=email);
    $$('[data-account-avatar]').forEach(el=>el.textContent=initials);
  }
  function closeAccountMenu({focus=false}={}){
    const menu=$('#account-menu'), trigger=$('#account-trigger'), popover=$('#account-popover');
    if(!menu||!trigger||!popover) return;
    menu.classList.remove('open'); popover.hidden=true; trigger.setAttribute('aria-expanded','false'); state.accountMenuOpen=false;
    if(focus) trigger.focus();
  }
  function toggleAccountMenu(){
    const menu=$('#account-menu'), trigger=$('#account-trigger'), popover=$('#account-popover');
    if(!menu||!trigger||!popover) return;
    if(state.accountMenuOpen){ closeAccountMenu(); return; }
    menu.classList.add('open'); popover.hidden=false; trigger.setAttribute('aria-expanded','true'); state.accountMenuOpen=true;
  }

  async function loadMe(){
    try{ state.me=await request('/api/me'); }catch(_){ state.me=null; }
    const cloud=!!(state.me&&state.me.cloud);
    $$('[data-cloud-only]').forEach(el=>el.hidden=!cloud);
    $$('[data-local-only]').forEach(el=>el.hidden=cloud);
    $$('[data-admin-only]').forEach(el=>el.hidden=!(cloud&&state.me.is_admin));
    if(!cloud) return;
    renderAccountMenu();
    try{ await loadSettings(); renderFeedbackDraft(); await loadFeedback(); if(state.me.is_admin){ await loadAdmin(); await loadAdminFeedback(); } }catch(error){ toast(error.message,null,true); }
  }
  async function loadSettings(){
    const [agents,folders,dropped]=await Promise.all([
      request('/api/agents'), request('/api/folders'), request('/api/folders/dismissed')]);
    state.settings.agents=agents.items; state.settings.folders=folders.items;
    state.settings.dropped=dropped.items;
    renderSettings();
    loadStyle().catch(error=>toast(error.message,null,true));
  }
  function renderSettings(){
    const me=state.me, select=$('#daily-time');
    if(!select.options.length) for(let m=15;m<22*60;m+=15){ const v=String(Math.floor(m/60)).padStart(2,'0')+':'+String(m%60).padStart(2,'0'); select.add(new Option(v,v)); }
    select.value=me.daily_time;
    $('#daily-time-copy').textContent=t('daily_time_now',{time:me.daily_time, scan:scanAt(me.daily_time)});
    $('#agent-list').innerHTML=state.settings.agents.map(a=>`<div class="agent-card ${a.scan_enabled?'on':''}"><div class="agent-badge">${esc(AGENT_BADGE[a.agent]||a.agent.slice(0,2).toUpperCase())}</div><div><div class="item-title">${esc(a.label)}</div>${a.agent==='claude-code'?`<div class="meta" style="margin-top:2px">${esc(t('agent_note_claude'))}</div>`:''}<div class="meta"><span><i class="dot ${a.connected?'live':''}"></i>${esc(a.connected?t('agent_connected',{time:stamp(a.last_seen)}):t('agent_not_connected'))}</span><span>${esc(t(!a.scannable?'agent_unsupported':a.scan_enabled?'agent_scanning':'agent_not_scanning'))}</span></div></div><label class="switch" ${a.scannable?'':`style="opacity:.45" title="${esc(t('agent_unsupported_title'))}"`}><input type="checkbox" data-agent-toggle="${esc(a.agent)}" ${a.scan_enabled?'checked':''} ${a.scannable?'':'disabled'} aria-label="${esc(t('agent_toggle_aria',{name:a.label}))}"><span></span></label></div>`).join('');
    renderFolders();
    const req=me.admin_request;
    $('#admin-status').innerHTML=me.is_admin
      ?`<div class="item-title">${esc(t('you_are_admin'))}</div><div class="meta">${esc(t('you_are_admin_sub'))}</div>`
      :req?`<div class="item-title">${esc(t('request_pending'))}</div><div class="meta">${esc(t('request_pending_sub',{time:stamp(req.created_at)}))}</div>`
      :`<div class="item-head"><div><div class="item-title">${esc(t('not_admin'))}</div><div class="meta">${esc(t('not_admin_sub'))}</div></div><button class="btn" id="request-admin">${esc(t('request_admin'))}</button></div>`;
  }
  // 按第 4 级目录分组折叠。/Users/你/Documents/work 底下那一堆平时收着，点一下才展开——
  // 自动扫出来的文件夹动辄上百个，摊开根本看不过来
  const FOLDER_DEPTH=4;
  function folderGroup(path){
    const lead=path.startsWith('/')?'/':'';            // Windows 路径是 C:/... ，没有开头那道斜杠
    return lead+path.split('/').filter(Boolean).slice(0,FOLDER_DEPTH).join('/');
  }
  function folderTree(items){
    const map=new Map();
    items.forEach(f=>{ const k=folderGroup(f.path); if(!map.has(k)) map.set(k,[]); map.get(k).push(f); });
    return [...map.entries()].map(([key,list])=>({key,list}));
  }
  // 扫描本来就含子目录，所以勾选要两头连锁，这是被机制逼出来的：
  //   勾一个 → 它盖住的下级一并打勾（它们确实在扫，显示成没勾就是骗人）
  //   取消一个 → 盖过它的上级必须一起取消（还有上级勾着的话，取消就是假的），
  //             它自己的下级也一起取消——「取消」就该是「这一片都别扫了」
  const folderUnder=(a,b)=>b!==a&&b.startsWith(a.replace(/\/+$/,'')+'/');
  function setFolderPick(path,on){
    state.settings.folders.forEach(f=>{
      if(f.path===path) f.selected=on;
      else if(folderUnder(path,f.path)) f.selected=on;
      else if(!on&&folderUnder(f.path,path)) f.selected=false;
    });
  }
  function renderFolders(){
    const items=state.settings.folders, picked=items.filter(f=>f.selected).length;
    $('#folder-count').textContent=t('folders_count',{picked, total:items.length});
    if(!state.settings.openGroups) state.settings.openGroups=new Set();
    const open=state.settings.openGroups;
    const when=f=>`<span class="meta" style="margin:0">${f.last_used?esc(t('folder_last_used',{date:f.last_used})):''}</span>`;
    const drop=(path,label)=>`<button type="button" class="folder-drop" data-drop="${esc(path)}" title="${esc(label)}" aria-label="${esc(label)}">×</button>`;
    const row=(f,short,cls)=>`<label class="folder" title="${esc(f.path)}">`
      +`<input type="checkbox" data-folder="${esc(f.path)}" ${f.selected?'checked':''}>`
      +`<span class="folder-path${cls?' '+cls:''}">${esc(short||f.path)}</span>`
      +when(f)+drop(f.path,t('folder_hide'))+`</label>`;

    const html=folderTree(items).map(({key,list})=>{
      if(list.length===1) return row(list[0]);          // 就一条，不必为它套一层
      const shown=open.has(key), n=list.filter(f=>f.selected).length;
      // 组头只是个标签，没有勾选框：一个文件夹可能自己是一个对话分组、底下又有别的对话分组，
      // 给组头加勾选框就得替人决定这两者的关系，很容易把人家自己那条悄悄弄丢
      const head=`<div class="folder folder-group">`
        +`<button type="button" class="folder-toggle${shown?' open':''}" data-group="${esc(key)}" aria-expanded="${shown}">▸</button>`
        +`<span class="folder-path">${esc(key)}</span>`
        +`<span class="meta folder-tally" style="margin:0">${esc(t('folders_scanning',{n, total:list.length}))}</span>`
        +`<span class="folder-bulk"><button type="button" class="btn tiny" data-pick-all="${esc(key)}">${esc(t('pick_all'))}</button>`
        +`<button type="button" class="btn tiny" data-pick-none="${esc(key)}">${esc(t('pick_none'))}</button></span>`
        +drop(key,t('folder_hide_group'))+`</div>`;
      if(!shown) return head;
      // 展开后子项只显示组名之后的那一截，全路径放 title 里
      return head+`<div class="folder-kids">`
        +list.map(f=>f.path===key
          ? row(f, t('folder_self'), 'folder-self')
          : row(f, f.path.slice(key.length+1)||f.path)).join('')+`</div>`;
    }).join('');

    $('#folder-list').innerHTML=items.length?html:empty(t('folders_empty'),t('folders_empty_sub'));
    renderHiddenFolders();
  }
  // 隐藏掉的收在面板底下，点一下才展开——列表本来就长，别再添乱
  function renderHiddenFolders(){
    const box=$('#folders-gone'), gone=state.settings.dropped||[];
    if(!box) return;
    if(!gone.length){ box.hidden=true; box.innerHTML=''; return; }
    box.hidden=false;
    const head=`${esc(t('folders_hidden',{n:gone.length}))} `
      +`<button type="button" id="folders-gone-toggle">${esc(t(state.settings.showDropped?'hide':'show'))}</button>`;
    box.innerHTML=state.settings.showDropped
      ? head+gone.map(f=>`<div class="folder gone"><span class="folder-path">${esc(f.path)}</span>`
          +`<span style="flex:1"></span>`
          +`<button type="button" class="btn tiny" data-restore="${esc(f.path)}">${esc(t('folder_unhide'))}</button></div>`).join('')
      : head;
  }
  async function dropFolder(path, restore){
    const paths=Array.isArray(path)?path:[path];
    try{
      await post('/api/folders/dismiss',{paths, restore:!!restore});
      const [list,gone]=await Promise.all([request('/api/folders'),request('/api/folders/dismissed')]);
      // 服务器不知道还没点「保存勾选」的那些勾，重新拉完要把它们贴回去，
      // 否则隐藏一个文件夹会顺手把人家勾了一半的选择抹掉
      const picked=new Set(state.settings.folders.filter(f=>f.selected).map(f=>f.path));
      state.settings.folders=list.items.map(f=>({...f, selected:f.selected||picked.has(f.path)}));
      state.settings.dropped=gone.items;
      renderFolders();
      toast(restore?t('toast_folder_unhidden'):t('toast_folder_hidden',{n:paths.length}));
    }catch(error){ toast(error.message,null,true); }
  }
  async function loadAdmin(){
    const [reqs,users]=await Promise.all([request('/api/admin/requests'),request('/api/admin/users')]);
    state.admin={requests:reqs.items, users:users.items};
    renderAdmin();
  }
  function renderAdmin(){
    const {requests,users}=state.admin;
    $('#nav-admin').textContent=requests.length||'—'; $('#request-count').textContent=requests.length;
    $('#request-list').innerHTML=requests.length
      ?requests.map(r=>`<div class="item item-head"><div><div class="item-title">${esc(r.display_name||r.author)}</div><div class="meta">${esc(r.author)} · ${esc(t('requested_at',{time:stamp(r.created_at)}))}</div></div><div class="button-row"><button class="btn small" data-decide="${esc(r.request_id)}" data-approve="0">${esc(t('deny'))}</button><button class="btn small primary" data-decide="${esc(r.request_id)}" data-approve="1">${esc(t('approve'))}</button></div></div>`).join('')
      :`<div class="empty"><strong>${esc(t('no_requests'))}</strong></div>`;
    $('#user-list').innerHTML=users.map(u=>`<tr><td><div class="item-title">${esc(u.display_name)}</div><div class="meta" style="margin-top:2px">${esc(u.author)}</div></td><td>${u.is_admin?'<span class="tag done">admin</span>':`<span class="tag">${esc(t('role_member'))}</span>`}</td><td>${esc(u.daily_time||'')}</td><td>${esc(u.last_active_date||'—')}</td><td>${u.update_count||0}</td><td><button class="btn small" data-view-as="${esc(u.author)}" data-name="${esc(u.display_name)}">${esc(t('view_log'))}</button></td></tr>`).join('');
  }
  function showViewAs(){
    $('#viewas').classList.toggle('show',!!state.viewAs);
    $('#viewas-copy').textContent=state.viewAs?t('viewas_copy',{name:state.viewAs.name, author:state.viewAs.author}):'';
  }
  function setViewAs(author,name){
    state.viewAs=author&&author!==(state.me||{}).author?{author,name}:null;
    showViewAs();
    switchView('today'); loadDashboard();
  }

  $('#viewas-exit').addEventListener('click',()=>setViewAs(null));
  $('#save-daily-time').addEventListener('click',async()=>{
    try{ const r=await post('/api/settings',{daily_time:$('#daily-time').value}); state.me.daily_time=r.daily_time; renderSettings(); toast(t('toast_time_saved',{time:r.daily_time})); }
    catch(error){toast(error.message,null,true);}
  });
  document.addEventListener('change',async event=>{
    const toggle=event.target.closest('[data-agent-toggle]');
    if(toggle){ const on=toggle.checked; toggle.disabled=true;
      try{ await post(`/api/agents/${encodeURIComponent(toggle.dataset.agentToggle)}`,{scan_enabled:on}); await loadSettings(); toast(t(on?'toast_scan_on':'toast_scan_off')); }
      catch(error){ toggle.checked=!on; toggle.disabled=false; toast(error.message,null,true); }
      return; }
    const folder=event.target.closest('[data-folder]');
    if(folder){ setFolderPick(folder.dataset.folder, folder.checked); renderFolders(); }
  });
  $('#folder-add-btn').addEventListener('click',()=>{
    const path=$('#folder-add').value.trim().replace(/\/+$/,'');
    // macOS / Linux 以 / 开头；Windows 以盘符开头，比如 C:\\Users\\you\\work
    if(!/^(\/|[A-Za-z]:[\\/])/.test(path)){ toast(t('toast_need_path'),null,true); return; }
    const hit=state.settings.folders.find(f=>f.path===path);
    if(hit) hit.selected=true; else state.settings.folders.unshift({path,selected:true,last_used:null});
    $('#folder-add').value=''; renderFolders(); toast(t('toast_folder_added'));
  });
  $('#save-folders').addEventListener('click',async()=>{
    try{ const r=await post('/api/folders',{selected:state.settings.folders.filter(f=>f.selected).map(f=>f.path)}); await loadSettings(); toast(t('toast_folders_saved',{n:r.selected.length})); }
    catch(error){toast(error.message,null,true);}
  });

  async function loadStyle(){ showStyle(await request('/api/style')); }
  function showStyle(profile){
    state.style=profile;
    $('#style-editor').value=profile.content_md||'';
    $('#style-updated').textContent=profile.updated_at?t('style_updated',{time:stamp(profile.updated_at)}):t('style_none');
  }
  async function saveStyle(content){
    try{ showStyle(await post('/api/style',{content_md:content})); toast(t(content.trim()?'toast_style_saved':'toast_style_cleared')); }
    catch(error){ toast(error.message,null,true); }
  }
  $('#save-style').addEventListener('click',()=>saveStyle($('#style-editor').value));
  $('#clear-style').addEventListener('click',()=>{ if(confirm(t('confirm_clear_style'))) saveStyle(''); });

  // ---------- 反馈 ----------
  // 线上一次请求最多 4.5 MB，base64 还要胖三分之一：每张压到 700 KB 以内，服务器那边再卡一道
  const FB_MAX_IMAGES=4, FB_MAX_EDGE=1600, FB_TARGET_BYTES=700000, FB_TOTAL_BYTES=3000000;
  const FB_TYPES=/^image\/(png|jpeg|webp|gif)$/;
  const readAsDataURL=blob=>new Promise((ok,fail)=>{ const r=new FileReader(); r.onload=()=>ok(r.result); r.onerror=()=>fail(r.error); r.readAsDataURL(blob); });
  async function shrinkImage(file){
    const url=URL.createObjectURL(file);
    try{
      const img=await new Promise((ok,fail)=>{ const i=new Image(); i.onload=()=>ok(i); i.onerror=()=>fail(new Error(t('fb_bad_type'))); i.src=url; });
      const scale=Math.min(1, FB_MAX_EDGE/Math.max(img.naturalWidth,img.naturalHeight));
      const canvas=document.createElement('canvas'); canvas.width=Math.max(1,Math.round(img.naturalWidth*scale)); canvas.height=Math.max(1,Math.round(img.naturalHeight*scale));
      const ctx=canvas.getContext('2d'); ctx.fillStyle='#fff'; ctx.fillRect(0,0,canvas.width,canvas.height); ctx.drawImage(img,0,0,canvas.width,canvas.height);
      let quality=0.88, blob=await new Promise(ok=>canvas.toBlob(ok,'image/jpeg',quality));
      while(blob&&blob.size>FB_TARGET_BYTES&&quality>0.5){ quality-=0.12; blob=await new Promise(ok=>canvas.toBlob(ok,'image/jpeg',quality)); }
      if(!blob) throw new Error(t('fb_bad_type'));
      return blob;
    } finally { URL.revokeObjectURL(url); }
  }
  async function addFeedbackFiles(files){
    for(const file of files){
      if(state.feedback.images.length>=FB_MAX_IMAGES){ toast(t('fb_too_many',{n:FB_MAX_IMAGES}),null,true); break; }
      try{
        if(!FB_TYPES.test(file.type)) throw new Error(t('fb_bad_type'));
        const blob=file.size<=FB_TARGET_BYTES?file:await shrinkImage(file);
        const used=state.feedback.images.reduce((sum,img)=>sum+img.size,0);
        if(used+blob.size>FB_TOTAL_BYTES) throw new Error(t('fb_too_big'));
        const dataUrl=await readAsDataURL(blob);
        state.feedback.images.push({mime:blob.type, data:dataUrl.split(',')[1], preview:dataUrl, size:blob.size});
      }catch(error){ toast(error.message,null,true); }
    }
    renderFeedbackDraft();
  }
  function renderFeedbackDraft(){
    const images=state.feedback.images;
    $('#fb-draft').innerHTML=images.map((img,i)=>`<div class="fb-thumb"><img src="${esc(img.preview)}" alt="${esc(t('fb_image_alt'))}"><button type="button" data-fb-remove="${i}" aria-label="${esc(t('fb_remove'))}">×</button></div>`).join('');
    $('#fb-add').textContent=t('fb_add',{n:images.length, max:FB_MAX_IMAGES});
    $('#fb-add').disabled=images.length>=FB_MAX_IMAGES;
  }
  function feedbackItem(item, forAdmin){
    const images=(item.images||[]).map(id=>{ const src=`/api/feedback/images/${encodeURIComponent(id)}`; return `<a href="${src}" target="_blank" rel="noopener"><img src="${src}" alt="${esc(t('fb_image_alt'))}" loading="lazy"></a>`; }).join('');
    const tagClass=item.status==='done'?'done':item.status==='new'?'unknown':'';
    const actions=forAdmin?`<div class="button-row" style="justify-content:flex-start;margin-top:10px">${['new','seen','done'].filter(s=>s!==item.status).map(s=>`<button class="btn small" type="button" data-fb-status="${s}" data-id="${esc(item.feedback_id)}">${esc(t('fb_mark_'+s))}</button>`).join('')}</div>`:'';
    return `<div class="item"><div class="item-head"><div class="item-body fb-body">${esc(item.body)}</div><span class="tag ${tagClass}">${esc(t('fb_status_'+item.status))}</span></div>${images?`<div class="fb-images">${images}</div>`:''}<div class="meta">${forAdmin?`<span>${esc(item.display_name||item.author)}</span>`:''}<span>${esc(stamp(item.created_at))}</span>${item.page?`<span>${esc(t('fb_from',{page:t('page_'+item.page)}))}</span>`:''}</div>${actions}</div>`;
  }
  async function loadFeedback(){ state.feedback.mine=(await request('/api/feedback')).items; renderFeedbackMine(); }
  function renderFeedbackMine(){
    const items=state.feedback.mine;
    $('#nav-feedback').textContent=items.length||'—';
    $('#fb-mine').innerHTML=items.length?items.map(item=>feedbackItem(item,false)).join(''):empty(t('fb_none_mine'),t('fb_none_mine_sub'));
  }
  async function loadAdminFeedback(){ state.feedback.all=(await request('/api/admin/feedback')).items; renderFeedbackAll(); }
  function renderFeedbackAll(){
    const items=state.feedback.all||[];
    $('#fb-new-count').textContent=t('fb_new_count',{n:items.filter(item=>item.status==='new').length});
    $('#fb-all').innerHTML=items.length?items.map(item=>feedbackItem(item,true)).join(''):`<div class="empty"><strong>${esc(t('fb_none_all'))}</strong></div>`;
  }
  $('#fb-add').addEventListener('click',()=>$('#fb-file').click());
  $('#fb-file').addEventListener('change',event=>{ addFeedbackFiles([...event.target.files]); event.target.value=''; });
  // 截图直接粘进输入框
  $('#fb-body').addEventListener('paste',event=>{
    const files=[...((event.clipboardData||{}).files||[])].filter(file=>file.type.startsWith('image/'));
    if(files.length){ event.preventDefault(); addFeedbackFiles(files); }
  });
  // 图片拖进来
  const dropZone=$('#fb-drop');
  const hasFiles=event=>!!event.dataTransfer&&[...event.dataTransfer.types].includes('Files');
  dropZone.addEventListener('dragover',event=>{ if(!hasFiles(event)) return; event.preventDefault(); dropZone.classList.add('dragging'); });
  dropZone.addEventListener('dragleave',()=>dropZone.classList.remove('dragging'));
  dropZone.addEventListener('drop',event=>{ if(!hasFiles(event)) return; event.preventDefault(); dropZone.classList.remove('dragging'); addFeedbackFiles([...event.dataTransfer.files].filter(file=>file.type.startsWith('image/'))); });
  $('#fb-submit').addEventListener('click',async()=>{
    const body=$('#fb-body').value.trim();
    if(!body&&!state.feedback.images.length){ toast(t('fb_empty'),null,true); return; }
    const button=$('#fb-submit'); button.disabled=true;
    try{
      await post('/api/feedback',{body, page:state.prevView||'today', images:state.feedback.images.map(({mime,data})=>({mime,data}))});
      $('#fb-body').value=''; state.feedback.images=[]; renderFeedbackDraft();
      await loadFeedback(); if(state.me&&state.me.is_admin) loadAdminFeedback().catch(()=>{});
      toast(t('fb_sent'));
    }catch(error){ toast(error.message,null,true); }
    finally{ button.disabled=false; }
  });

  // 改日报的键盘行为（回车续写列表、Tab 缩进、编号重排）和贴图都在编辑器里做，见 editor/entry.js
  document.addEventListener('keydown',event=>{
    const filterTrigger=event.target.closest('[data-filter-trigger]');
    if(event.key==='Escape'&&state.accountMenuOpen){ event.preventDefault(); closeAccountMenu({focus:true}); return; }
    if(event.key==='Escape'&&state.filterMenu.open){ event.preventDefault(); closeFilterMenu({focus:true}); return; }
    if(filterTrigger){
      const select=$('#'+filterTrigger.dataset.filterTrigger); if(!select) return;
      if(event.key==='ArrowDown'||event.key==='ArrowUp'){
        event.preventDefault(); if(state.filterMenu.open!==select.id) openFilterMenu(select);
        moveFilterMenu(select,event.key==='ArrowDown'?1:-1); return;
      }
      if(event.key==='Enter'||event.key===' '){
        event.preventDefault(); if(state.filterMenu.open===select.id) chooseFilterOption(select,state.filterMenu.active); else openFilterMenu(select); return;
      }
      if(event.key==='Tab') closeFilterMenu();
    }
    if(event.key!=='Escape') return;
    closeLightbox();
    const help=$('#quick-api-help-text'); if(help) help.hidden=true;
  });
  document.addEventListener('change',event=>{ if(event.target.id!=='report-image-file') return; uploadReportImages([...event.target.files]); event.target.value=''; });

  // 页面放大缩小 + 侧边栏收起：都记在这台电脑的浏览器里，下次进来还是上次那样
  const ZOOM_KEY='fecho-zoom', SIDEBAR_KEY='fecho-sidebar', ZOOM_STEPS=[0.8,0.9,1,1.1,1.25,1.5];
  function applyZoom(value){
    const root=document.documentElement;
    root.style.zoom=value===1?'':String(value);
    // 缩放后 100vh 这类单位会跟着放大，侧边栏和大图遮罩靠这个变量换算回来
    root.style.setProperty('--zoom',String(value));
    try{ localStorage.setItem(ZOOM_KEY,String(value)); }catch(_){}
  }
  // 没有按钮了，缩的时候闪一下倍数，不然不知道自己缩到哪了
  let zoomBadgeTimer=null;
  function flashZoom(value){
    let badge=$('#zoom-badge');
    if(!badge){ badge=document.createElement('div'); badge.id='zoom-badge'; badge.className='zoom-badge'; document.body.appendChild(badge); }
    badge.textContent=Math.round(value*100)+'%';
    badge.classList.add('show');
    clearTimeout(zoomBadgeTimer);
    zoomBadgeTimer=setTimeout(()=>badge.classList.remove('show'),900);
  }
  function currentZoom(){
    return parseFloat(document.documentElement.style.getPropertyValue('--zoom'))||1;
  }
  function stepZoom(delta){
    const now=currentZoom();
    let i=ZOOM_STEPS.indexOf(now); if(i<0) i=ZOOM_STEPS.indexOf(1);
    const next=ZOOM_STEPS[Math.max(0,Math.min(ZOOM_STEPS.length-1,i+delta))];
    if(next===now) return;
    applyZoom(next); flashZoom(next);
  }
  // 按住 ⌘（Mac）或 Ctrl 滚动缩放。要拦掉浏览器自己的缩放就必须 passive:false，
  // 否则 preventDefault 不生效
  document.addEventListener('wheel',event=>{
    if(!event.ctrlKey&&!event.metaKey) return;
    event.preventDefault();
    if(Math.abs(event.deltaY)<1) return;
    stepZoom(event.deltaY<0?1:-1);
  },{passive:false});
  // ⌘0 / Ctrl+0 恢复原始大小——按钮没了，这是唯一的归位方式
  document.addEventListener('keydown',event=>{
    if(!(event.metaKey||event.ctrlKey)) return;
    if(event.key!=='0'&&event.code!=='Digit0') return;
    event.preventDefault();
    if(currentZoom()!==1){ applyZoom(1); flashZoom(1); }
  });
  // 收起 / 展开都记在这台电脑上。窄屏没存过就默认收着——那一档本来就放不下宽侧边栏
  function applySidebar(collapsed,{persist=true}={}){
    const shell=$('.shell'); if(!shell) return;
    shell.classList.toggle('sidebar-collapsed',collapsed);
    shell.classList.toggle('sidebar-expanded',!collapsed);
    const button=$('#sidebar-toggle');
    if(button){
      const key=collapsed?'sidebar_expand':'sidebar_collapse';
      button.setAttribute('aria-expanded',String(!collapsed));
      button.setAttribute('aria-label',t(key)); button.setAttribute('title',t(key));
      const label=$('.sr-only',button); if(label) label.textContent=t(key);
    }
    if(persist){ try{ localStorage.setItem(SIDEBAR_KEY,collapsed?'1':'0'); }catch(_){} }
  }
  (()=>{
    let zoom=1, collapsed=null;
    try{
      zoom=parseFloat(localStorage.getItem(ZOOM_KEY))||1;
      const saved=localStorage.getItem(SIDEBAR_KEY);
      if(saved==='1'||saved==='0') collapsed=saved==='1';
    }catch(_){}
    if(collapsed===null) collapsed=window.matchMedia('(max-width:900px)').matches;
    applyZoom(ZOOM_STEPS.includes(zoom)?zoom:1);
    applySidebar(collapsed,{persist:false});
  })();

  applyStatic();
  setFilterControlsReady(false);
  switchView('today',{scroll:false});
  loadMe();
  loadDashboard();
