(()=>{
'use strict';
const DEMO = location.protocol === 'file:' || new URLSearchParams(location.search).has('demo');
let state=null,csrf='',view='dashboard',filter='all',actionBusy=false,toastTimer;
const el = id=>document.getElementById(id);
const esc = v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const niceTime = s=>{if(!s)return 'Not yet';const d=new Date(s);if(isNaN(d))return '—';return new Intl.DateTimeFormat('en-IN',{timeZone:'Asia/Kolkata',day:'2-digit',month:'short',hour:'2-digit',minute:'2-digit',hour12:true}).format(d).replace(',',' ·');};
const shortTime = s=>{if(!s)return 'Never';let d=new Date(s);if(isNaN(d))return '—';return new Intl.DateTimeFormat('en-IN',{timeZone:'Asia/Kolkata',day:'2-digit',month:'short',hour:'2-digit',minute:'2-digit'}).format(d);};
function safeUrl(raw){try{let u=new URL(raw);return u.protocol==='https:' && (u.hostname==='www.icaionlineregistration.org'||u.hostname==='icaionlineregistration.org')?u.href:null;}catch{return null;}}
function field(b,keys){for(const [k,v] of Object.entries(b.fields||{})){let low=k.toLowerCase();if(keys.some(x=>low.includes(x))&&v)return v;}return null;}
function batchRow(b,wide=false){const batchNo=field(b,['batch number','batch no','batch id','batch code'])||field(b,['batch'])||'Batch number not provided';const start=field(b,['start date','from date','batch start']);const end=field(b,['end date','to date','batch end']);const link=(b.links||[]).map(safeUrl).find(Boolean)||'https://www.icaionlineregistration.org/launchbatchdetail.aspx';const current=b.currently_listed;const tag=!current?'<span class="tag inactive">PREVIOUSLY SEEN</span>':b.is_baseline?'<span class="tag base">TRACKED</span>':'<span class="tag new">NEW</span>';
return `<tr><td><span class="course-main">${esc(b.course)}</span><span class="course-meta">${esc(batchNo)}</span></td><td class="date-label">${esc(start||'—')}</td>${wide?`<td class="date-label">${esc(end||'—')}</td><td class="date-label">${esc(niceTime(b.first_seen))}</td>`:''}<td>${tag}</td><td><a class="table-external" href="${esc(link)}" target="_blank" rel="noopener noreferrer" title="View on official ICAI site" aria-label="Open official ICAI site">↗</a></td></tr>`;}
function renderRows(id,batches,wide){el(id).innerHTML=batches.length?batches.map(b=>batchRow(b,wide)).join(''):`<tr><td colspan="${wide?6:4}" class="empty-state"><span class="empty-icon">◌</span>No batches recorded yet.<br>Results will appear after a successful ICAI scan.</td></tr>`;}
function iconFor(kind){return {new:'✦',success:'✓',sent:'➤',error:'!',warning:'!',info:'◷'}[kind]||'◷';}
function renderEvents(id,events){el(id).innerHTML=events.length?events.map(e=>`<div class="event-item"><span class="event-symbol ${esc(e.kind)}">${iconFor(e.kind)}</span><div class="event-main"><div class="event-title">${esc(e.title)}</div><div class="event-detail">${esc(e.details||'')}</div></div><span class="event-time">${esc(niceTime(e.at))}</span></div>`).join(''):'<div class="empty-state">No activity yet. Your first scan will appear here.</div>';}
function render(){if(!state)return;
const last=state.last_check||{},good=state.last_success||{},stats=state.stats||{known:0,new:0,pending:0};const error=last.status==='error';const running=state.running||last.status==='running';
el('nav-count').textContent=stats.known;
el('interval-text').textContent=`${state.poll_minutes} minutes`;
el('monitor-frequency').textContent=`Every ${state.poll_minutes} min`;
el('settings-interval').textContent=`Every ${state.poll_minutes} minutes`;
el('stat-known').textContent=stats.known;
el('stat-new').textContent=stats.new;
el('stat-last').textContent=good.finished_at?shortTime(good.finished_at):'Not checked';
el('last-check-detail').textContent=good.finished_at?'Last confirmed successful scan':'Awaiting first successful scan';
el('stat-telegram').textContent=state.telegram_ready?'Configured':'Not connected';
el('telegram-detail').textContent=state.telegram_ready?(stats.pending?`${stats.pending} alert(s) pending delivery`:'Ready for new-batch alerts'):'Add token and chat ID to enable';
el('monitor-telegram').textContent=state.telegram_ready?'Configured ✓':'Not configured';
el('monitor-last').textContent=last.started_at?shortTime(last.started_at):'Never';
el('monitor-title').textContent=running?'Checking ICAI now':error?'Last scan failed':good.finished_at?'Monitoring active':'Waiting for first scan';
el('monitor-subtitle').textContent=running?'Browser scan in progress':error?'Review the error below':good.finished_at?'Tracking new batch announcements':'Automatic checks are enabled';
el('monitor-light').classList.toggle('warn',error);
const top=el('top-status');top.className=`status-pill ${error?'problem':good.finished_at?'live':''}`;top.innerHTML=`<span class="status-dot"></span> ${running?'Checking now':error?'Check failed':good.finished_at?'Monitoring active':'Initializing'}`;
const banner=el('error-banner');banner.hidden=!error;banner.textContent=error?`Last check failed: ${last.message||'Unknown error'}. The monitor will retry automatically.`:'';
el('check-now').disabled=running||actionBusy||DEMO;el('scan-button-icon').textContent=running?'◷':'↻';
el('test-telegram').disabled=!state.telegram_ready||actionBusy||DEMO;
el('settings-test').disabled=!state.telegram_ready||actionBusy||DEMO;
el('settings-telegram').textContent=state.telegram_ready?'Alerts are configured. Telegram test messages can confirm delivery.':state.telegram_bot_ready?'Bot token is configured. Find and verify the recipient chat below.':'Add a freshly rotated TELEGRAM_BOT_TOKEN to your hosting service to connect the bot.';
el('bot-token-state').textContent=state.telegram_bot_ready?'Configured ✓':'Missing';
el('recipient-state').textContent=state.telegram_chat_id?`Chat ID ${state.telegram_chat_id}`:'Not verified';
el('find-chats').disabled=!state.telegram_bot_ready||actionBusy||DEMO;
el('verify-recipient').disabled=!state.telegram_bot_ready||actionBusy||DEMO;
const batches=state.batches||[];renderRows('latest-batches',batches.slice(0,6),false);
const filtered=batches.filter(b=>filter==='new'?!b.is_baseline:filter==='active'?b.currently_listed:true);renderRows('all-batches',filtered,true);
el('batch-total-label').textContent=`${filtered.length} of ${batches.length} batches`;
renderEvents('recent-activity',(state.events||[]).slice(0,4));renderEvents('full-activity',state.events||[]);
}
function showToast(message,error=false){const box=el('toast');box.className='toast show'+(error?' error':'');box.textContent=message;clearTimeout(toastTimer);toastTimer=setTimeout(()=>box.className='toast',4000);}
function setView(next){view=next;document.querySelectorAll('.view').forEach(node=>node.classList.toggle('active',node.id==='view-'+next));document.querySelectorAll('[data-view]').forEach(b=>b.classList.toggle('active',b.dataset.view===next));el('page-name').textContent=({dashboard:'Overview',batches:'All batches',activity:'Activity log',settings:'Settings'})[next]||'Overview';window.scrollTo({top:0,behavior:'smooth'});}
async function load(){if(DEMO){state=demoData;csrf='';el('demo-banner').hidden=false;render();return;}try{const response=await fetch('/api/state',{credentials:'same-origin',cache:'no-store'});if(response.status===401){location.href='/login';return;}if(!response.ok)throw Error('Unable to load monitor status');state=await response.json();csrf=state.csrf;render();}catch(error){showToast(error.message,true);}}
async function post(url){if(DEMO)return showToast('This is a visual preview. Deploy the website to activate.',true);if(actionBusy)return;actionBusy=true;render();try{const res=await fetch(url,{method:'POST',credentials:'same-origin',headers:{'X-CSRF-Token':csrf}});const data=await res.json();if(!res.ok)throw Error(data.detail||data.message||'Request failed');showToast(data.message||'Done');if(url==='/api/logout'){location.href='/login';return;}await load();}catch(err){showToast(err.message,true);}finally{actionBusy=false;render();}}
document.querySelectorAll('[data-view]').forEach(b=>b.addEventListener('click',()=>setView(b.dataset.view)));
document.querySelectorAll('[data-filter]').forEach(b=>b.addEventListener('click',()=>{filter=b.dataset.filter;document.querySelectorAll('[data-filter]').forEach(x=>x.classList.toggle('chosen',x===b));render();}));
el('check-now').addEventListener('click',()=>post('/api/check'));
el('test-telegram').addEventListener('click',()=>post('/api/telegram/test'));
el('settings-test').addEventListener('click',()=>post('/api/telegram/test'));
el('find-chats').addEventListener('click', async ()=>{
  if(DEMO)return showToast('Recipient discovery works after deployment.',true);
  if(actionBusy)return;
  actionBusy=true;render();el('chat-results').textContent='Finding conversations...';
  try{
    const r=await fetch('/api/telegram/chats',{method:'POST',credentials:'same-origin',headers:{'X-CSRF-Token':csrf}});
    const data=await r.json();if(!r.ok)throw Error(data.detail||'Telegram lookup failed');
    const list=data.chats||[];
    el('chat-results').replaceChildren();
    if(!list.length){el('chat-results').textContent='No chats found. Open @PranjalAlert_bot, send /start and Hello, then try again.';}
    else list.forEach(chat=>{
      const b=document.createElement('button');b.className='chat-option';b.type='button';
      b.textContent=`${chat.name} · ${chat.id}  → Select`;b.addEventListener('click',()=>{
        el('recipient-input').value=chat.id;showToast('Chat selected. Click Verify & save to send a test.');
      });el('chat-results').append(b);
    });
  }catch(e){el('chat-results').textContent=e.message;showToast(e.message,true)}
  finally{actionBusy=false;render();}
});
el('verify-recipient').addEventListener('click',async()=>{
  if(DEMO)return showToast('Recipient verification works after deployment.',true);
  if(actionBusy)return;
  const chat_id=el('recipient-input').value.trim();
  if(!/^\d{5,20}$/.test(chat_id))return showToast('Enter a valid numeric Telegram Chat ID.',true);
  actionBusy=true;render();
  try{
    const r=await fetch('/api/telegram/recipient',{method:'POST',credentials:'same-origin',
      headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:JSON.stringify({chat_id})});
    const data=await r.json();if(!r.ok)throw Error(data.detail||'Verification failed');
    showToast(data.message);await load();
  }catch(e){showToast(e.message,true)}
  finally{actionBusy=false;render();}
});

el('logout-btn').addEventListener('click',()=>post('/api/logout'));
const updateClock=()=>el('time-clock').textContent=new Intl.DateTimeFormat('en-IN',{timeZone:'Asia/Kolkata',hour:'2-digit',minute:'2-digit',hour12:true}).format(new Date())+' IST';updateClock();setInterval(updateClock,30000);
const demoData={telegram_chat_id:'123456789',telegram_bot_ready:true,telegram_bot_username:'PranjalAlert_bot',poll_minutes:5,telegram_ready:true,running:false,stats:{known:4,new:2,pending:0},last_check:{status:'ok',started_at:new Date().toISOString(),finished_at:new Date().toISOString()},last_success:{id:3,finished_at:new Date().toISOString()},batches:[{key:'demo-1',course:'ICITSS – Orientation Course',region:'Western Region',pou:'Nashik',fields:{'Batch Number':'DEMO–NASHIK–104','Start Date':'12/12/2026','End Date':'28/12/2026'},links:[],first_seen:new Date().toISOString(),is_baseline:false,notified:true,currently_listed:true},{key:'demo-2',course:'ICITSS – Information Technology',region:'Western Region',pou:'Nashik',fields:{'Batch Number':'DEMO–NASHIK–105','Start Date':'18/12/2026','End Date':'03/01/2027'},links:[],first_seen:new Date().toISOString(),is_baseline:false,notified:true,currently_listed:true},{key:'demo-3',course:'Advanced ICITSS – MCS',region:'Western Region',pou:'Nashik',fields:{'Batch Number':'DEMO–NASHIK–102','Start Date':'05/12/2026'},links:[],first_seen:new Date().toISOString(),is_baseline:true,notified:true,currently_listed:true},{key:'demo-4',course:'Advanced ICITSS – Advanced IT',region:'Western Region',pou:'Nashik',fields:{'Batch Number':'DEMO–NASHIK–101','Start Date':'01/12/2026'},links:[],first_seen:new Date().toISOString(),is_baseline:true,notified:true,currently_listed:false}],events:[{kind:'new',title:'New batch: ICITSS – Orientation Course',details:'Example alert (not an actual announcement)',at:new Date().toISOString()},{kind:'sent',title:'Telegram alert sent',details:'Example alert delivery',at:new Date().toISOString()},{kind:'success',title:'Baseline created',details:'Example monitoring history',at:new Date().toISOString()}]};
load();if(!DEMO)setInterval(load,12000);
})();
