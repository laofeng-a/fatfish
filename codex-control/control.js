'use strict';
const el=id=>document.getElementById(id);
let token=sessionStorage.getItem('fatfish-control-token')||'';
let remoteMode=false,initialized=false;
function captureToken(){
  const value=location.hash.slice(1);
  if(/^[A-Za-z0-9_-]{30,80}$/.test(value)){
    token=value;sessionStorage.setItem('fatfish-control-token',value);
    history.replaceState(null,'',location.pathname);
  }
}
captureToken();
let selected=null,shared=false,nativeShared=false,cursor=null,loading=false,mutating=false,lastSignature='',browsingOlder=false;
let ready=false,authFailed=false,booted=false,polling=false,epoch=0,turnState='';
let rows=[];
const statusNames={idle:'等待新想法',active:'正在进行',notLoaded:'可继续',systemError:'执行端异常',completed:'已完成',interrupted:'已停止',failed:'执行失败',inProgress:'正在进行'};
function node(tag,className,text){const item=document.createElement(tag);if(className)item.className=className;if(text!==undefined)item.textContent=text;return item;}
function notice(text,error=false,retry=false){
  el('notice').hidden=!text;el('notice-text').textContent=text;
  el('notice').classList.toggle('error',error);el('retry').hidden=!retry;
}
function connected(state,label,bridge){
  el('connection').dataset.state=state;el('connection-label').textContent=label;el('bridge-label').textContent=bridge;
}
async function api(args){
  if(!token&&!remoteMode){authFailed=true;throw Error('请双击桌面的「大肥鱼共用 Codex」进入，快捷方式会自动连接。');}
  const abort=new AbortController(),timeout=setTimeout(()=>abort.abort(),45000);
  try{
    const headers={'Content-Type':'application/json'};if(!remoteMode)headers.Authorization='Bearer '+token;
    const response=await fetch('/api',{method:'POST',signal:abort.signal,headers,body:JSON.stringify(args)});
    if(!response.headers.get('Content-Type')?.includes('application/json')){authFailed=true;throw Error('邮箱登录可能已过期，请刷新页面重新登录。');}
    const value=await response.json();
    if(response.status===401){authFailed=true;ready=false;throw Error('连接凭据已失效，请从桌面的「大肥鱼共用 Codex」重新打开。');}
    if(!response.ok)throw Error(value.error||'连接暂时不可用');
    return value.result;
  }catch(error){
    if(error.name==='AbortError'||error instanceof TypeError){
      ready=false;connected('offline','本机连接暂时断开','可用桌面快捷方式重新启动');enabled();
      throw Error(['send','stop','attach'].includes(args.action)?'没有收到提交确认。请先查看对话记录，避免重复发送。':remoteMode?'连接暂时中断。页面会自动重试；若登录已过期，请刷新重新验证邮箱。':'连接没有响应。页面会自动重连，也可以用桌面快捷方式重新启动。');
    }
    throw error;
  }finally{clearTimeout(timeout);}
}
function enabled(){
  el('attach').disabled=!selected||shared||mutating||!ready;
  el('attach').textContent=shared?'已共用':'设为共用';
  el('refresh').disabled=!selected||!ready;
  el('text').disabled=!shared||mutating||!ready;
  el('send').disabled=!shared||mutating||!ready||!el('text').value.trim();
  el('stop').disabled=!shared||mutating||!ready||turnState!=='active';
  el('compose-context').replaceChildren(node('span','context-dot'),document.createTextNode(!ready?'等待本机连接':shared?'你与大肥鱼，共用这条会话':'设为共用后，就可以一起继续'));
  el('send').title=mutating?'正在提交…':'发送 · Ctrl + Enter';
  el('send').setAttribute('aria-busy',String(mutating));
  el('branch-kind').textContent=selected?(shared?(nativeShared?'原生共用 · ':'共用分支 · ')+(remoteMode?'手机接入':'电脑执行'):'来源对话 · 仅浏览'):'同一份上下文，一起继续';
  el('sync-state').textContent=!ready?'连接中':loading?'正在同步':statusNames[turnState]||'已连接';
  for(const button of el('threads').querySelectorAll('button'))button.disabled=mutating;
}
const smallScreen=matchMedia('(max-width:700px)');
function menu(open){document.body.classList.toggle('sidebar-open',open);el('scrim').hidden=!open;el('menu').setAttribute('aria-expanded',String(open));el('sidebar').inert=smallScreen.matches&&!open;}
smallScreen.addEventListener('change',()=>menu(false));menu(false);
function saveDraft(){if(selected)sessionStorage.setItem('fatfish-draft-'+selected,el('text').value);}
function sizeInput(){el('text').style.height='auto';el('text').style.height=Math.min(210,Math.max(49,el('text').scrollHeight))+'px';}
function isShared(row){return !!(row.remote_controlled||row.shared_thread_id);}
function paintThreads(){
  const area=el('threads');area.replaceChildren();el('thread-count').textContent=rows.length?String(rows.length).padStart(2,'0'):'';
  if(!rows.length)area.append(node('p','sidebar-empty',el('query').value?'没有找到这条会话，换个关键词试试。':'还没有最近会话。'));
  for(const row of rows){
    const button=node('button','thread');button.type='button';button.dataset.thread=row.id;
    const selectedHere=row.id===selected||row.shared_thread_id===selected;
    button.classList.toggle('selected',selectedHere);if(selectedHere)button.setAttribute('aria-current','page');
    button.append(node('span','thread-title',row.name||'未命名会话'));
    const project=(row.cwd||'').split(/[\\/]/).filter(Boolean).pop();
    button.append(node('small','',(isShared(row)?'共用会话':'来源会话')+(project?' · '+project:'')));
    button.title=row.name||row.id;
    button.onclick=()=>selectThread(row);area.append(button);
  }
}
async function selectThread(row){
  saveDraft();selected=row.shared_thread_id||row.id;shared=isShared(row);
  localStorage.setItem('fatfish-shared-thread',selected);browsingOlder=false;cursor=null;loading=false;lastSignature='';epoch++;
  turnState='';el('text').value=sessionStorage.getItem('fatfish-draft-'+selected)||'';sizeInput();
  el('title').textContent=row.name||'未命名会话';el('history').replaceChildren(node('p','empty','正在打开这条对话…'));
  notice(shared?'':'设为共用后，将连接这条原生会话；执行仍由Codex桌面负责。');
  paintThreads();menu(false);enabled();await load();
}
async function list(){
  const result=await api({action:'list',query:el('query').value});rows=result.threads;
  // A source and its shared copy lead to the same place; show one sidebar entry.
  const canonical=new Map();
  for(const row of rows){const key=row.shared_thread_id||row.id;const previous=canonical.get(key);if(!previous||row.remote_controlled)canonical.set(key,row);}
  rows=[...canonical.values()];
  if(!el('query').value&&result.default_thread_id&&!rows.some(row=>row.id===result.default_thread_id||row.shared_thread_id===result.default_thread_id)){
    rows.unshift({id:result.default_thread_id,name:'大肥鱼共用 Codex',remote_controlled:true});
  }
  paintThreads();
  if(!selected){
    const stored=localStorage.getItem('fatfish-shared-thread');
    const preferred=rows.find(row=>row.native&&row.id===result.default_thread_id);
    const pick=preferred||rows.find(row=>row.id===stored||row.shared_thread_id===stored)||rows.find(row=>row.id===result.default_thread_id||row.shared_thread_id===result.default_thread_id);
    if(pick)await selectThread(pick);else el('history').setAttribute('aria-busy','false');
  }
}
// Deliberately use DOM nodes, never HTML from model output. External images do not auto-load.
function inline(parent,text){
  const pattern=/(`[^`\n]+`|\*\*[^*\n]+\*\*|\[[^\]\n]+\]\(https?:\/\/[^\s)]+\))/g;
  let start=0;
  for(const match of text.matchAll(pattern)){
    parent.append(document.createTextNode(text.slice(start,match.index)));const part=match[0];let child;
    if(part.startsWith('`'))child=node('code','',part.slice(1,-1));
    else if(part.startsWith('**'))child=node('strong','',part.slice(2,-2));
    else{const link=part.match(/^\[([^\]]+)\]\((.+)\)$/);child=node('a','',link[1]);child.href=link[2];child.target='_blank';child.rel='noopener noreferrer';}
    parent.append(child);start=match.index+part.length;
  }
  parent.append(document.createTextNode(text.slice(start)));
}
function markdown(parent,text){
  const lines=text.replace(/\r\n/g,'\n').split('\n');let i=0;
  const cells=line=>line.trim().replace(/^\|/,'').replace(/\|$/,'').split('|').map(cell=>cell.trim());
  const isTable=index=>lines[index]?.includes('|')&&lines[index+1]?.includes('|')&&cells(lines[index+1]).every(cell=>/^:?-{3,}:?$/.test(cell));
  while(i<lines.length){
    const line=lines[i];if(!line.trim()){i++;continue;}
    if(/^\s*```/.test(line)){
      const language=line.replace(/^\s*```/,'').trim();i++;const code=[];
      while(i<lines.length&&!/^\s*```/.test(lines[i]))code.push(lines[i++]);if(i<lines.length)i++;
      const block=node('div','code-block'),heading=node('div','code-heading'),copy=node('button','','复制');copy.type='button';
      copy.onclick=async()=>{try{await navigator.clipboard.writeText(code.join('\n'));copy.textContent='已复制';setTimeout(()=>copy.textContent='复制',1500);}catch{copy.textContent='请选中文字复制';}};
      heading.append(node('span','',language||'代码'),copy);const pre=node('pre');pre.append(node('code','',code.join('\n')));block.append(heading,pre);parent.append(block);continue;
    }
    const heading=line.match(/^#{1,6}\s+(.+)$/);
    if(heading){const h=node(line.startsWith('###')?'h4':'h3');inline(h,heading[1]);parent.append(h);i++;continue;}
    if(isTable(i)){
      const wrap=node('div','table-wrap'),table=node('table'),head=node('thead'),tr=node('tr'),body=node('tbody');
      const columns=cells(line);for(const cell of columns){const th=node('th');th.scope='col';inline(th,cell);tr.append(th);}head.append(tr);i+=2;
      while(i<lines.length&&lines[i].includes('|')&&lines[i].trim()){
        const row=node('tr'),values=cells(lines[i++]);for(let col=0;col<columns.length;col++){const td=node('td');inline(td,values[col]||'');row.append(td);}body.append(row);
      }
      table.append(head,body);wrap.append(table);parent.append(wrap);continue;
    }
    if(/^\s*([-*]|\d+\.)\s+/.test(line)){
      const ordered=/^\s*\d+\./.test(line),list=node(ordered?'ol':'ul');
      while(i<lines.length&&new RegExp(ordered?'^\\s*\\d+\\.\\s+':'^\\s*[-*]\\s+').test(lines[i])){
        const li=node('li');inline(li,lines[i++].replace(/^\s*(?:[-*]|\d+\.)\s+/,''));list.append(li);
      }
      parent.append(list);continue;
    }
    if(/^>\s?/.test(line)){const quote=[];while(i<lines.length&&/^>\s?/.test(lines[i]))quote.push(lines[i++].replace(/^>\s?/,''));const block=node('blockquote');inline(block,quote.join('\n'));parent.append(block);continue;}
    const paragraph=[line];i++;
    while(i<lines.length&&lines[i].trim()&&!isTable(i)&&!/^(?:\s*```|#{1,6}\s|>\s?|\s*(?:[-*]|\d+\.)\s+)/.test(lines[i]))paragraph.push(lines[i++]);
    const p=node('p');inline(p,paragraph.join('\n'));parent.append(p);
  }
}
const toolNames={commandExecution:'运行命令',fileChange:'修改文件',mcpToolCall:'调用工具',webSearch:'搜索资料',reasoning:'思考记录',plan:'执行计划',imageView:'查看图片'};
function toolGroup(items){
  const details=node('details','tool-group'),summary=node('summary','','查看 '+items.length+' 项操作记录');details.append(summary);
  for(const item of items){const inner=node('details','tool');inner.append(node('summary','',toolNames[item.type]||item.type),node('pre','',item.text));details.append(inner);}
  return details;
}
function render(turns,append=false){
  const area=el('history'),nearBottom=area.scrollHeight-area.scrollTop-area.clientHeight<160,fragment=document.createDocumentFragment();
  const expanded=new Set([...area.querySelectorAll('details[open][data-record]')].map(item=>item.dataset.record));
  for(const turn of [...turns].reverse()){
    let tools=[],groupIndex=0;
    const flush=()=>{if(tools.length){const group=toolGroup(tools);group.dataset.record=turn.id+'-'+groupIndex++;group.open=expanded.has(group.dataset.record);fragment.append(group);tools=[];}};
    for(const item of turn.messages){
      if(!['userMessage','agentMessage'].includes(item.type)){tools.push(item);continue;}flush();
      const user=item.type==='userMessage',fish=user&&item.text.startsWith('[大肥鱼转达 jg]'),human=user&&item.text.startsWith('[jg · 共用控制页]');
      const text=fish||human?item.text.slice(item.text.indexOf('\n')+1):item.text;
      const article=node('article','bubble '+(user?'user':'assistant')),label=node('div','label');
      if(!user)label.append(node('span','avatar','C'));
      label.append(node('span','',user?(fish?'大肥鱼 · 转达 jg':human?'jg':'jg / 大肥鱼'):'Codex'));
      const copy=node('button','copy-message','复制');copy.type='button';copy.setAttribute('aria-label','复制这条'+(user?'消息':'回复'));
      copy.onclick=async()=>{try{await navigator.clipboard.writeText(text);copy.textContent='已复制';setTimeout(()=>copy.textContent='复制',1600);}catch{notice('浏览器未允许复制，可以长按文字选择复制。');}};label.append(copy);
      const content=node('div','message-content');markdown(content,text);article.append(label,content);
      if(item.truncated)article.append(node('div','message-note','这条消息较长，当前显示部分内容。'));
      fragment.append(article);
    }
    flush();
    if(turn.omitted_tool_items)fragment.append(node('p','empty','还有 '+turn.omitted_tool_items+' 项操作记录未展开，已优先保留对话正文。'));
  }
  if(!turns.length&&!append)fragment.append(node('p','empty','上下文已准备好，写下接下来想做的事。'));
  if(append){const height=area.scrollHeight;area.prepend(fragment);area.scrollTop+=area.scrollHeight-height;}
  else{area.replaceChildren(fragment);if(nearBottom||area.scrollTop===0)area.scrollTop=area.scrollHeight;}
}
async function load(older=false){
  if(!selected||loading||!ready)return;
  if(older)browsingOlder=true;loading=true;const current=epoch,id=selected;el('history').setAttribute('aria-busy','true');
  try{
    const result=await api({action:'history',thread_id:id,...(older&&cursor?{cursor}: {})});if(current!==epoch)return;
    const signature=JSON.stringify(result.turns);if(older||signature!==lastSignature){render(result.turns,older);if(!older)lastSignature=signature;}
    cursor=result.next_cursor;el('older').hidden=!cursor;updateScroll();
    nativeShared=!!result.native;
    const recent=result.turns[0]?.status;
    turnState=recent==='inProgress'?'active':recent||result.status?.type||'';el('session-status').textContent=statusNames[turnState]||'会话已连接';el('session-status').classList.toggle('active',turnState==='active');
    el('identity').textContent='会话编号：'+result.thread_id;
    if(result.name)el('title').textContent=result.name.replace(/^大肥鱼共用 · /,'');
    if(!older&&result.turns[0]?.status==='failed')notice('最近一次任务执行失败，可展开操作记录查看详情。页面连接仍可用。',true);
    enabled();
  }catch(error){if(current===epoch)notice(error.message,true,!authFailed);}
  finally{if(current===epoch){loading=false;el('history').setAttribute('aria-busy','false');enabled();}}
}
async function poll(){
  if(!initialized||polling||document.hidden||authFailed)return;polling=true;
  try{
    const status=await api({action:'status'});const recovered=!ready;ready=status.engine_ready;
    connected(ready?'ready':'connecting',ready?(remoteMode?'电脑执行端已连接':'本机执行端已连接'):'Codex 正在连接',status.bridge_online?'大肥鱼远程入口已连接':'大肥鱼远程入口正在重连');enabled();
    if(!ready){notice('Codex 正在连接，页面会自动重试。',false,true);return;}
    if(recovered)notice('');
    if(!booted){await list();booted=true;}
    else if(selected&&!mutating&&!browsingOlder)await load();
  }catch(error){notice(error.message,true,!authFailed);if(authFailed){connected('offline','需要从快捷方式打开','登录凭据只在此电脑保留');enabled();}}
  finally{polling=false;}
}
el('search').onsubmit=async event=>{event.preventDefault();try{await list();}catch(error){notice(error.message,true,!authFailed);}};
el('attach').onclick=async()=>{
  if(mutating||!ready)return;mutating=true;enabled();
  try{const result=await api({action:'attach',thread_id:selected});saveDraft();await selectThread({id:result.thread_id,name:result.name||el('title').textContent,remote_controlled:true});notice('共用已连接。接下来，你和大肥鱼都在这里继续。');await list();}
  catch(error){notice(error.message,true,!authFailed);}finally{mutating=false;enabled();}
};
el('composer').onsubmit=async event=>{
  event.preventDefault();const text=el('text').value;if(!text.trim()||mutating||!shared||!ready)return;
  mutating=true;enabled();
  try{const result=await api({action:'send',thread_id:selected,text,request_id:crypto.randomUUID()});if(result.submitted===false){notice(result.note||'本次没有提交任务。',true);return;}el('text').value='';saveDraft();sizeInput();browsingOlder=false;notice(result.state==='queued_on_owner'?'已加入原生会话队列，等待Codex桌面接收；尚未确认开始。':'已提交，完成状态以对话记录为准。');await load();}
  catch(error){notice(error.message,true,!authFailed);}finally{mutating=false;enabled();}
};
el('stop').onclick=async()=>{
  if(mutating||!ready)return;mutating=true;enabled();
  try{const result=await api({action:'stop',thread_id:selected,request_id:crypto.randomUUID()});notice(result.state==='interruption_requested'?'已请求停止当前任务。':result.state==='queue_cancelled'?'已取消待执行消息。':result.state==='interrupt_failed'?'桌面端未接受停止请求，请在Codex里停止。':result.state==='queue_cancel_failed'?'未能确认取消，请先查看Codex原生队列。':'没有找到本入口可停止的任务。',result.state==='interrupt_failed'||result.state==='queue_cancel_failed');await load();}
  catch(error){notice(error.message,true,!authFailed);}finally{mutating=false;enabled();}
};
el('usage').onclick=async()=>{
  const button=el('usage');button.disabled=true;
  try{
    const value=await api({action:'usage'}),parts=[];
    for(const bucket of value.limits||[]){for(const key of ['primary','secondary']){
      const w=bucket[key];if(!w)continue;
      const title=w.window_duration_mins===300?'5小时':w.window_duration_mins===10080?'每周':bucket.limit_name||bucket.limit_id;
      const left=w.remaining_percent==null?'未知':w.remaining_percent+'%';
      const reset=w.resets_at==null?'':'，重置 '+new Date(w.resets_at*1000).toLocaleString('zh-CN',{timeZone:'Asia/Singapore',month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit'});
      parts.push(title+'剩余 '+left+reset);
    }}
    notice((parts.join('；')||'用量数据暂不可用。')+(value.ordinary_usage_allowed===false?'；当前普通使用不可用。':''));
  }catch(error){notice(error.message,true);}finally{button.disabled=false;}
};
el('text').oninput=()=>{sizeInput();saveDraft();enabled();};
el('text').onkeydown=event=>{if((event.ctrlKey||event.metaKey)&&event.key==='Enter'){event.preventDefault();el('composer').requestSubmit();}};
function updateScroll(){const area=el('history');el('latest').hidden=area.scrollHeight-area.scrollTop-area.clientHeight<180&&!browsingOlder;}
el('history').addEventListener('scroll',updateScroll,{passive:true});
el('refresh').onclick=()=>{browsingOlder=false;load();};
el('latest').onclick=async()=>{browsingOlder=false;await load();el('history').scrollTop=el('history').scrollHeight;updateScroll();};
el('older').onclick=()=>load(true);el('retry').onclick=()=>initialized?poll():initialize();el('dismiss').onclick=()=>notice('');
el('menu').onclick=()=>menu(!document.body.classList.contains('sidebar-open'));el('scrim').onclick=()=>menu(false);
document.addEventListener('keydown',event=>{if(event.key==='Escape')menu(false);});
document.addEventListener('visibilitychange',()=>{if(!document.hidden)poll();});
window.addEventListener('hashchange',()=>{captureToken();authFailed=false;poll();});
async function phoneStatus(){
  try{
    const status=await api({action:'phone_status'}),online=status.state==='online'&&status.url;
    el('phone-link').hidden=!online;el('phone-copy').hidden=!online;
    if(online){el('phone-link').href=status.url;el('phone-link').textContent=status.url;el('phone-description').textContent='用手机打开下方链接，再通过 '+status.allowed_mail+' 的邮箱验证码登录。';}
    else el('phone-description').textContent=status.error||({starting:'手机入口正在建立，请稍后刷新。',disabled:'手机入口尚未开启。',offline:'手机入口已断开，本机页面仍可继续。'}[status.state]||'正在检查手机入口…');
  }catch(error){el('phone-description').textContent=error.message;}
}
el('phone-open').onclick=()=>{menu(false);el('phone-dialog').showModal();phoneStatus();};el('phone-refresh').onclick=phoneStatus;
el('phone-copy').onclick=async()=>{try{await navigator.clipboard.writeText(el('phone-link').href);el('phone-copy').textContent='已复制';setTimeout(()=>el('phone-copy').textContent='复制手机链接',1600);}catch{el('phone-description').textContent='请长按下方链接复制。';}};
el('phone-dialog').addEventListener('click',event=>{if(event.target===el('phone-dialog')){const r=el('phone-dialog').getBoundingClientRect();if(event.clientX<r.left||event.clientX>r.right||event.clientY<r.top||event.clientY>r.bottom)el('phone-dialog').close();}});
async function initialize(){
  try{const response=await fetch('/bootstrap');const settings=await response.json();remoteMode=settings.mode==='phone';
    if(remoteMode){el('phone-open').hidden=true;document.body.classList.add('phone-mode');}
    initialized=true;await poll();
  }catch{notice('暂时无法连接控制台，请刷新页面重试。',true,true);}
}
setInterval(poll,4000);enabled();initialize();
