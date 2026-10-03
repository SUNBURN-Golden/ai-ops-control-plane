'use strict';
const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const labels = {queued:'시작 대기',preparing:'레포 준비',planning:'계획 중',building:'개발 중',reviewing:'감사 중',supervising:'감리 중',publishing:'결과 정리',verifying:'CI 확인 중',waiting_provider:'연결 재시도 대기',paused:'일시정지',needs_user:'결정 필요',unknown:'실행 확인 필요',ready:'검수 준비',accepted:'검수 완료',cancelled:'취소됨'};
const roleInfo = {planner:['계획','레포의 기준 문서를 읽고 완성까지의 순서를 정합니다.','⌁'],builder:['개발','코드를 구현하고 테스트·디버깅·수정을 이어갑니다.','↗'],reviewer:['감사','별도 세션에서 실제 코드와 검증 근거를 확인합니다.','◎'],supervisor:['감리','원래 목표와 산출물 전체가 충족됐는지 점검합니다.','◈']};
function providerInfo(id){return state?.providers.find(p=>p.id===id);}
let state = null, selected = null, activePage = 'workspace', modelsDirty = false, dialogAction = null, timer = null;
let pendingRequestId = null, pendingRequestPayload = null;
function el(tag, className, content) { const node = document.createElement(tag); if(className) node.className=className; if(content!==undefined) node.textContent=content; return node; }
function toast(message){ const node=$('#toast'); node.textContent=message; node.hidden=false; clearTimeout(timer); timer=setTimeout(()=>node.hidden=true,5500); }
async function api(path, body, bearer){
  const headers={'Content-Type':'application/json','X-AIOPS-Client':'desktop'};
  if(bearer) headers.Authorization='Bearer '+bearer;
  const response=await fetch('/api'+path,{method:body===undefined?'GET':'POST',headers,body:body===undefined?undefined:JSON.stringify(body),credentials:'same-origin',cache:'no-store'});
  const data=await response.json();
  if(!response.ok) throw new Error(data.message||data.error||'요청을 완료하지 못했습니다.');
  return data;
}
function go(page){
  activePage=page;
  for(const node of $$('.page')) node.hidden=node.id!==page+'-page';
  for(const node of $$('[data-page]')) { node.classList.toggle('active',node.dataset.page===page); node.setAttribute('aria-current',node.dataset.page===page?'page':'false'); }
  $('#breadcrumb').textContent={workspace:'작업실',models:'모델 설정',connect:'연결'}[page];
  if(page==='models') renderModels();
  window.scrollTo({top:0,behavior:'auto'});
}
for(const button of $$('[data-page]')) button.addEventListener('click',()=>go(button.dataset.page));
for(const button of $$('[data-goto]')) button.addEventListener('click',()=>go(button.dataset.goto));
$('.brand').addEventListener('click',event=>{event.preventDefault();go('workspace');});
function safeLink(url, label){
  const node=el('a','button secondary small',label+' ↗');
  try { const parsed=new URL(url); if(parsed.protocol!=='https:'||parsed.hostname!=='github.com') throw Error(); node.href=url; node.target='_blank'; node.rel='noopener noreferrer'; }
  catch {return el('span','detail-meta',label+' (링크 확인 필요)');}
  return node;
}
function pill(job){return el('span','status-pill '+(['needs_user','unknown'].includes(job.state)?'attention':['ready','accepted'].includes(job.state)?'ready':['paused','cancelled'].includes(job.state)?'muted':''),job.pause_requested&&job.attempt?'이 단계 후 멈춤':labels[job.state]||job.state);}
function renderJobs(){
  const jobs=state.jobs; const active=jobs.filter(j=>!['accepted','cancelled'].includes(j.state));
  $('#job-count').textContent=String(active.length); $('#active-count').textContent=String(active.length);
  $('#empty-state').hidden=jobs.length>0;
  if(!selected&&jobs.length)selected=jobs[0].id;
  const list=$('#job-list'); list.replaceChildren();
  for(const job of jobs){
    const row=el('button','job-row'+(selected===job.id?' selected':'')); row.type='button';
    row.append(el('span','job-mark',job.state==='accepted'?'✓':'↗'));
    const body=el('div','job-body');body.append(el('strong','',job.repository),el('p','',job.goal));row.append(body,pill(job),el('span','row-arrow','›'));
    row.addEventListener('click',()=>{selected=job.id;renderJobs();renderDetail(true);});list.append(row);
  }
  renderDetail(false);
}
function actionButton(title, action, kind='secondary'){
  const button=el('button','button '+kind+' small',title);button.type='button';
  button.addEventListener('click',async()=>{button.disabled=true;try{await action();}catch(error){toast(error.message);}finally{button.disabled=false;}});return button;
}
async function action(job, name, value={}) { await api('/jobs/'+job.id+'/'+name,value); await refresh(); }
function ask(job,name,title,context){dialogAction={job,name};$('#feedback-title').textContent=title;$('#feedback-context').textContent=context||'';$('#feedback-text').value='';$('#feedback-dialog').showModal();$('#feedback-text').focus();}
function moment(value){return new Date(value*1000).toLocaleTimeString('ko-KR',{hour:'2-digit',minute:'2-digit'});}
function renderHealth(job){
  const h=job.health;if(!h)return null;
  const box=el('div','execution-card'),heading=el('strong'),body=el('p');
  if(job.attempt){
    const minutes=Math.floor(h.elapsed_seconds/60),profile=h.profile;
    heading.textContent=h.status==='outcome_unknown'?'종료 확인을 기다리고 있어요':h.status==='finishing'?'실행 종료를 확인하고 있어요':h.status==='quiet'?'실행 중 · 최근 출력 없음':'실행 중 · 출력 관측됨';
    body.textContent=`${roleInfo[h.role][0]} · ${providerInfo(profile.provider)?.name||profile.provider} · ${minutes}분 경과 · 실행 한도 ${moment(h.deadline_at)}. `+(h.last_output_at?`마지막 출력 ${moment(h.last_output_at)}. `:'아직 출력이 관측되지 않았어요. ')+(h.status==='outcome_unknown'?'같은 실행의 종료 기록을 다시 확인하며 중복 작업을 막고 있어요.':'출력 관측은 작업 완료를 의미하지 않아요.');
  }else if(h.status==='waiting_for_owner'){
    heading.textContent='앞선 실행의 확인을 기다려요';body.textContent=h.blocking_repository+'의 미확정 실행이 정리되면 자동으로 이어갑니다.';
  }else if(h.retry_at){
    heading.textContent=h.status==='feedback_wait'?'같은 작업의 보완을 이어갑니다':'연결을 다시 시도할 예정이에요';
    body.textContent=`${moment(h.retry_at)}에 같은 도구와 설정으로 자동 진행합니다.`;
  }else if(['ready','accepted'].includes(job.state)){
    heading.textContent=job.state==='accepted'?'사용자 검수 완료':'결과를 검수할 수 있어요';body.textContent='현재 코드의 감사·감리와 CI 결과를 기준으로 한 개발 산출물입니다. 병합·배포와 병합 후 검증은 별도입니다.';
  }else return null;
  box.append(heading,body);return box;
}
function renderDetail(scroll){
  const job=state.jobs.find(j=>j.id===selected);const detail=$('#job-detail');
  if(!job){detail.hidden=true;return;}
  const wasOpen=$('.events',detail)?.open||false;detail.hidden=false;detail.replaceChildren();
  const header=el('div','detail-header'), title=el('div');title.append(el('h2','',job.repository),el('p','',new Date(job.created*1000).toLocaleString('ko-KR')+' 시작'));header.append(title,pill(job));detail.append(header);
  const steps=[['planning','계획'],['building','개발·테스트'],['reviewing','감사'],['supervising','감리'],['publishing','결과 정리'],['ready','최종 검수']];
  const phase=['ready','accepted'].includes(job.state)?'ready':job.phase==='verifying'?'publishing':job.phase;const index=steps.findIndex(s=>s[0]===phase);const pipe=el('div','pipeline');
  steps.forEach((s,i)=>{if(i)pipe.append(el('span','pipeline-arrow','›'));pipe.append(el('span','pipeline-step '+(job.state==='ready'&&i===5||phase===s[0]?'current':i<index?'done':''),(i<index?'✓ ':'')+s[1]));});detail.append(pipe);
  const health=renderHealth(job);if(health)detail.append(health);
  if(job.summary)detail.append(el('div','detail-summary',job.summary));
  if(job.plan){
    if(job.program_scope)detail.append(el('div','scope-note',`원본 프로그램 ${job.program_scope.count}개 · 구현 단계 ${job.built_tasks.length}개. 원본 노드·명세·의존성을 고정해 계획 누락을 검사합니다. 별도 대기 카탈로그와 실환경·릴리스 완료는 이 수치에 포함되지 않습니다.`));
    const tasks=el('ul','tasks');for(const task of job.plan.tasks){const built=job.built_tasks.includes(task.id);const item=el('li','task-item'+(built?'':' pending'));item.append(el('span','task-check',built?'✓':'○'));const content=el('div');content.append(el('strong','',task.title),el('p','',built?'구현 단계 완료 · 전체 검증 결과는 아래에서 확인':task.acceptance.join(' · ')));item.append(content);tasks.append(item);}detail.append(tasks);
    const review=job.review?.head===job.head, supervision=job.supervision?.head===job.head;
    const ci={passed:'통과',pending:'진행 중',failed:'수정 중',not_configured:'등록된 검사 없음',not_published:'로컬 결과 · 미확인'}[job.ci?.state]||'대기';
    detail.append(el('div','detail-meta',`현재 코드 검증 · 감사 ${review?'완료':'대기'} / 감리 ${supervision?'완료':'대기'} / GitHub CI ${ci}`));
  }
  if(job.question){const box=el('div','question-box');box.append(el('strong','',job.state==='unknown'?'실행 상태를 확인해야 합니다':'여기만 확인해 주세요'),el('p','',job.question));detail.append(box);}
  const buttons=el('div','detail-actions');
  if(job.pr_url)buttons.append(safeLink(job.pr_url,'결과 PR 보기'));
  if(job.state==='ready'){
    buttons.append(actionButton('검수 완료 ✓',()=>action(job,'accept'),'primary'));
    buttons.append(actionButton('수정 요청',()=>ask(job,'revise','어떤 부분을 더 수정할까요?','의견을 반영한 뒤 감사와 감리를 다시 거칩니다.')));
  }else if(job.state==='needs_user'){
    if(job.provider_error){buttons.append(actionButton('연결 확인 후 계속',()=>action(job,'resume'),'primary'),actionButton('연결 설정 보기',()=>go('connect')));}
    else buttons.append(actionButton('답하고 계속 진행',()=>ask(job,'resume','필요한 결정만 알려 주세요',job.question),'primary'));
  }
  else if(['paused','waiting_provider'].includes(job.state))buttons.append(actionButton('이어서 진행 ↗',()=>action(job,'resume'),'primary'));
  else if(!['unknown','accepted','cancelled'].includes(job.state))buttons.append(actionButton(job.pause_requested?'일시정지 요청됨':'일시정지',()=>action(job,'pause')));
  if(!job.attempt&&['paused','needs_user','waiting_provider'].includes(job.state))buttons.append(actionButton('작업 취소',()=>action(job,'cancel')));
  if(!job.attempt&&['paused','needs_user','waiting_provider'].includes(job.state)&&JSON.stringify(job.settings.roles)!==JSON.stringify(state.settings.roles))buttons.append(actionButton('새 모델 설정 적용',()=>action(job,'reconfigure')));
  detail.append(buttons);
  const team=Object.entries(job.settings.roles).map(([role,config])=>roleInfo[role][0]+': '+(providerInfo(config.provider)?.name||config.provider)+(config.model?' / '+config.model:' / CLI 기본값')).join(' · ');
  detail.append(el('div','detail-meta',team));
  if(job.head)detail.append(el('div','detail-meta',`검토 대상 ${job.head.slice(0,12)} · 실행 요청 ${job.calls}회`));
  const events=el('details','events');events.open=wasOpen;events.append(el('summary','','작업 기록 보기'));const log=el('div');events.append(log);detail.append(events);
  const load=async()=>{try{const rows=await api('/jobs/'+job.id+'/events');if(selected!==job.id)return;log.replaceChildren();for(const row of rows){const node=el('div','event',row.message);node.prepend(el('time','',new Date(row.created*1000).toLocaleString('ko-KR')));log.append(node);}}catch(error){log.textContent=error.message;}};
  events.addEventListener('toggle',()=>{if(events.open)load();});if(wasOpen)load();
  if(scroll)detail.scrollIntoView({behavior:matchMedia('(prefers-reduced-motion: reduce)').matches?'auto':'smooth',block:'start'});
}
function renderModels(){
  if(!state||modelsDirty)return;
  const root=$('#role-grid');root.replaceChildren();
  for(const [role,info] of Object.entries(roleInfo)){
    const card=el('div','role-card card'),heading=el('div','role-heading');heading.append(el('h2','',info[0]),el('span','role-symbol',info[2]));card.append(heading,el('p','',info[1]));
    const fields=el('div','role-fields');const providerWrap=el('div'),modelWrap=el('div');
    const label=el('label','','실행 도구');label.htmlFor=role+'-provider';const provider=el('select');provider.id=role+'-provider';
    for(const item of state.providers){const option=el('option','',item.name);option.value=item.id;provider.append(option);}provider.value=state.settings.roles[role].provider;
    const ml=el('label','','모델 ID');ml.htmlFor=role+'-model';const model=el('input');model.id=role+'-model';model.type='text';model.placeholder='비우면 CLI 기본 모델';model.value=state.settings.roles[role].model;model.maxLength=120;
    const hint=el('p','provider-hint');hint.id=role+'-provider-hint';model.setAttribute('aria-describedby',hint.id);
    const explain=()=>{const item=providerInfo(provider.value);model.placeholder=item.model_hint;model.required=item.model_required;hint.textContent=item.description+' 모델 확인: '+item.models_command;};
    provider.addEventListener('change',()=>{model.value='';explain();});explain();
    providerWrap.append(label,provider);modelWrap.append(ml,model);fields.append(providerWrap,modelWrap);card.append(fields,hint);root.append(card);
  }
  $('#session-minutes').value=state.settings.session_minutes;$('#max-calls').value=state.settings.max_agent_calls;$('#keep-awake').checked=state.settings.keep_awake;$('#publish-pr').checked=state.settings.publish_pr;
}
function shellQuote(value){return "'"+value.replaceAll("'","'\\''")+"'";}
function renderConnections(){
  const c=state.connections;$('#host-dot').classList.add('online');$('#connection-label').textContent='앱에 연결됨';$('#version').textContent=state.version;
  const configured=c.git.installed&&c.gh.installed&&c.github_authenticated&&Object.values(state.settings.roles).every(v=>c[v.provider]?.installed);
  $('#setup-banner').hidden=configured;
  const list=$('#connection-list');list.replaceChildren();
  for(const [key,name] of [['git','Git'],['gh','GitHub CLI'],...state.providers.map(p=>[p.id,p.name])]){
    const item=c[key]||{},row=el('div','connection-row'),body=el('div');body.append(el('strong','',name),el('p','',item.version||(item.installed?'설치 확인됨':'설치 후 다시 확인해 주세요.')));const ready=item.installed&&(key!=='gh'||c.github_authenticated);row.append(body,el('span','status-pill '+(ready?'ready':'muted'),ready?(key==='gh'?'로그인됨':key==='git'?'설치됨':'설치됨 · 로그인 미확인'):item.installed?(key==='gh'&&c.github_authentication==='not_checked'?'로그인 확인 전':'로그인 필요'):'미설치'));
    const info=providerInfo(key);if(info){const help=el('p','connection-help');help.append(el('code','',info.login_command),document.createTextNode(' · '));const link=el('a','','설치 안내 ↗');link.href=info.docs_url;link.target='_blank';link.rel='noopener noreferrer';help.append(link);body.append(help);}list.append(row);
  }
  const prefix=shellQuote(state.python_path)+' '+shellQuote(state.cli_path)+' --data-dir '+shellQuote(state.data_directory);
  $('#cli-example').textContent=prefix+" start \\\n  --repo BeautifulMind-JT/ZARI \\\n  --goal '레포에 명시된 산출물을 완성하고 결과를 검수할 수 있게 해 줘' \\\n  --request-id zari-delivery-001";
  $('#mcp-example').textContent=JSON.stringify({mcpServers:{aiops:{command:state.python_path,args:[state.cli_path,'--data-dir',state.data_directory,'mcp']}}},null,2);
}
async function refresh(){
  try{const next=await api('/state');const changed=JSON.stringify(next.jobs)!==JSON.stringify(state?.jobs);state=next;$('#connection-error').hidden=true;renderConnections();if(changed||!$('#job-list').children.length)renderJobs();renderModels();}
  catch(error){$('#host-dot').classList.remove('online');$('#connection-label').textContent='연결 확인 필요';$('#connection-error').textContent=error.message;$('#connection-error').hidden=false;}
}
$('#refresh').addEventListener('click',refresh);
$('#doctor').addEventListener('click',async event=>{event.target.disabled=true;try{await api('/doctor',{});await refresh();toast('연결 상태를 확인했습니다.');}catch(error){toast(error.message);}finally{event.target.disabled=false;}});
$('#new-job').addEventListener('submit',async event=>{
  event.preventDefault();const button=$('#start-button');button.disabled=true;
  const value={repository:$('#repository').value.trim().replace(/^github\.com\//,'https://github.com/'),goal:$('#goal').value.trim()};
  const fingerprint=JSON.stringify(value);if(pendingRequestPayload!==fingerprint){pendingRequestId=crypto.randomUUID();pendingRequestPayload=fingerprint;}
  try{const job=await api('/jobs',{...value,request_id:pendingRequestId});selected=job.id;pendingRequestId=null;pendingRequestPayload=null;await refresh();renderDetail(true);toast('맡았습니다. 계획부터 시작합니다.');}
  catch(error){toast(error.message);}finally{button.disabled=false;}
});
$('#model-form').addEventListener('input',()=>modelsDirty=true);
$('#model-form').addEventListener('change',()=>modelsDirty=true);
$('#model-form').addEventListener('submit',async event=>{
  event.preventDefault();const button=$('button[type=submit]',event.target);button.disabled=true;
  const roles={};for(const role of Object.keys(roleInfo))roles[role]={provider:$('#'+role+'-provider').value,model:$('#'+role+'-model').value.trim()};
  try{await api('/settings',{schema_version:1,roles,session_minutes:Number($('#session-minutes').value),max_agent_calls:Number($('#max-calls').value),keep_awake:$('#keep-awake').checked,publish_pr:$('#publish-pr').checked});modelsDirty=false;await refresh();toast('저장했습니다. 다음 작업부터 적용됩니다.');}catch(error){toast(error.message);}finally{button.disabled=false;}
});
$('#close-dialog').addEventListener('click',()=>$('#feedback-dialog').close());
$('#feedback-form').addEventListener('submit',async event=>{event.preventDefault();const button=$('button[type=submit]',event.target);button.disabled=true;try{const {job,name}=dialogAction;await action(job,name,{[name==='resume'?'answer':'feedback']:$('#feedback-text').value.trim()});$('#feedback-dialog').close();toast('의견을 반영해 이어서 진행합니다.');}catch(error){toast(error.message);}finally{button.disabled=false;}});
for(const name of ['cli','mcp'])$('#copy-'+name).addEventListener('click',async()=>{try{await navigator.clipboard.writeText($('#'+name+'-example').textContent);toast('복사했습니다.');}catch{toast('브라우저에서 복사를 허용하거나 위 내용을 직접 복사해 주세요.');}});
async function boot(){
  const secret=location.hash.slice(1);if(secret&&secret!=='workspace'){history.replaceState(null,'','/');try{await api('/session',{},secret);}catch(error){toast(error.message);}}
  await refresh();
  const schedule=()=>setTimeout(async()=>{if(!document.hidden)await refresh();schedule();},5000);schedule();
}
boot();
