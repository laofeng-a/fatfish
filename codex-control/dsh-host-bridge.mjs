import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import crypto from 'node:crypto';

export const name = 'fatfish-dsh-bridge';
export const inject = ['sessionController', 'agents', 'sessions', 'userQuestions'];

export function createBridge(ctx, config={}) {
  const directory = config.directory??path.join(os.homedir(), '.codex', 'fatfish-dsh');
  fs.mkdirSync(directory, {recursive:true, mode:0o700});
  const tokenPath=path.join(directory,'host.token');
  if (!fs.existsSync(tokenPath)) fs.writeFileSync(tokenPath,crypto.randomBytes(32).toString('hex'),{mode:0o600});
  const token=fs.readFileSync(tokenPath,'utf8').trim();
  const statePath=path.join(directory,'admissions.json');
  const admissions=fs.existsSync(statePath)?JSON.parse(fs.readFileSync(statePath,'utf8')):{};
  const questions=new Map();
  const errors=new Map();
  const signal=new AbortController().signal;
  const save=()=>{fs.writeFileSync(statePath+'.pending',JSON.stringify(admissions),{mode:0o600});fs.renameSync(statePath+'.pending',statePath);};
  const textOf=e=>(e.data?.message?.content??e.data?.content??[]).filter(b=>b.type==='text').map(b=>b.text).join('\n');
  const inspect=id=>ctx.sessionController.inspect(id,signal);
  const eventsOf=state=>state.events??[];
  const isWatched=id=>Object.values(admissions).some(a=>a.sessionId===id&&!a.closed);
  ctx.on('agent/error',({agent,error})=>errors.set(agent.id,{at:Date.now(),message:String(error?.message??error).slice(0,1000)}));

  // Keep the ordinary DSH question UI working; an answer from either side settles it.
  ctx.on('user-questions/request',async(request,next)=>{
    const id=request.agent?.id;
    if(!id||!isWatched(id))return next(request);
    const callId=crypto.randomUUID();
    const controller=new AbortController();
    const abort=()=>controller.abort(request.signal?.reason);
    request.signal?.addEventListener('abort',abort,{once:true});
    let resolve,reject;
    const answer=new Promise((yes,no)=>{resolve=yes;reject=no;});
    questions.set(callId,{sessionId:id,questions:request.questions,resolve,reject});
    try{return await Promise.race([answer,next({...request,signal:controller.signal})]);}
    finally{questions.delete(callId);controller.abort();request.signal?.removeEventListener('abort',abort);}
  },{global:true,prepend:true});

  function pending(id){return [...questions.entries()].filter(([,q])=>q.sessionId===id).map(([callId,q])=>({call_id:callId,questions:q.questions}));}
  async function read(id){
    const state=await inspect(id);const events=eventsOf(state);
    const end=events.findLast(e=>e.type==='turn/end');
    const finalSeq=events.filter(e=>e.type==='assistant/message'&&!e.data?.interrupted&&end&&e.seq<=end.seq).at(-1)?.seq;
    let remaining=6000;const messages=[];
    for(const e of events.filter(e=>e.type==='user/message'||e.type==='assistant/message').slice(-8).reverse()){
      if(!remaining)break;const text=textOf(e);const cap=Math.min(remaining,e.seq===finalSeq?4000:1000);
      messages.push({role:e.type==='user/message'?'user':'assistant',phase:e.data?.interrupted?'interrupted':e.type==='assistant/message'?(e.seq===finalSeq?'final_answer':'commentary'):null,
        seq:e.seq,text:text.slice(0,cap),truncated:text.length>cap});remaining-=Math.min(text.length,cap);
    }
    messages.reverse();
    const title=events.findLast(e=>e.type==='session/title')?.data?.title??state.meta?.title;
    return {thread_id:id,name:title,cwd:state.meta?.cwd,status:ctx.agents.get(id)?.status??'notLoaded',
      messages,latest_seq:events.at(-1)?.seq??-1,pending_questions:pending(id),error:errors.get(id)?.message};
  }
  async function watch(jobId){
    const admission=admissions[jobId];if(!admission)return {state:'unknown',terminal_confirmed:false};
    const state=await inspect(admission.sessionId);const events=eventsOf(state);
    const user=admission.answerTo?{seq:admission.afterSeq}:events.find(e=>e.type==='user/message'&&e.data?.source?.rpcId===admission.requestId);
    const waiting=pending(admission.sessionId);
    if(waiting.length)return {state:'waiting_input',terminal_confirmed:true,status:'completed',
      text:'DSH正在提问：\n'+JSON.stringify(waiting)+'\n用dsh工具read核对；continue或send传answers={call_id,items:[{id,selected:[],custom:"回答"}]}代答。'};
    if(!user)return {state:'pending',terminal_confirmed:false};
    const previousStart=events.filter(e=>e.seq<=user.seq&&e.type==='turn/start').at(-1);
    const end=events.find(e=>e.seq>user.seq&&e.type==='turn/end'&&(!previousStart||e.data?.turn===previousStart.data?.turn));
    if(!end){
      const failure=errors.get(admission.sessionId);
      if(failure&&failure.at>=admission.at)return {state:'failed',status:'failed',terminal_confirmed:true,text:'DSH执行失败：'+failure.message};
      return {state:'running',terminal_confirmed:false};
    }
    const messages=events.filter(e=>e.seq>user.seq&&e.seq<=end.seq&&e.type==='assistant/message'&&!e.data?.interrupted);
    const reason=end.data?.reason??end.data?.status;
    const kind=typeof reason==='object'?reason?.kind:reason;
    const status=['aborted','cancelled','interrupted'].includes(kind)?'interrupted':kind==='error'||kind==='failed'?'failed':'completed';
    admission.closed=true;save();
    return {state:status,status,terminal_confirmed:true,text:messages.map(textOf).filter(Boolean).join('\n\n').slice(-6000)||`DSH本轮已结束（${kind??'未知原因'}），未取得最终正文。`,
      source_turn:previousStart?.data?.turn,turn_end_seq:end.seq,completion_reason:reason};
  }
  async function execute(job){
    const action=job.action,id=job.thread_id;
    if(action==='status')return {online:true,pid:process.pid,watched:Object.values(admissions).filter(a=>!a.closed).length};
    if(action==='list'){
      const result=await ctx.sessionController.list({},signal);const query=(job.query??'').toLowerCase();
      const rows=result.items.map(r=>({...r,title:r.projections?.values?.title??r.title}));
      return {threads:rows.filter(r=>!query||JSON.stringify([r.title,r.sessionId,r.cwd]).toLowerCase().includes(query)).slice(0,20).map(r=>({id:r.sessionId,name:r.title,cwd:r.cwd,status:r.running?'running':'idle'}))};
    }
    if(action==='create'){
      const sessionId='fatfish-'+job.id;
      const result=await ctx.sessionController.create({sessionId,...job.cwd?{cwd:job.cwd}:{}});
      if(job.title)await ctx.sessionController.rename({sessionId:result.sessionId,title:job.title});
      return {thread_id:result.sessionId,name:job.title,state:'created'};
    }
    if(action==='read')return read(id);
    if(action==='watch')return watch(job.id);
    if(action==='stop'){
      const result=await ctx.sessionController.cancel({sessionId:id});return {thread_id:id,stopped:true,state:'interruption_requested',result};
    }
    if(action!=='send')throw new Error('Unsupported DSH operation');
    const requestId='fatfish-'+job.id;
    if(admissions[job.id])return {thread_id:id,turn_id:'dsh-'+job.id,state:'submitted',already_submitted:true};
    const state=await inspect(id);const events=eventsOf(state);const afterSeq=events.at(-1)?.seq??-1;
    let answers;
    try{const begin=job.text.indexOf('{');if(begin>=0){const value=JSON.parse(job.text.slice(begin));if(value.call_id&&Array.isArray(value.items))answers=value;}}catch{}
    if(answers){
      const question=questions.get(answers.call_id);
      if(!question||question.sessionId!==id)throw new Error('该问题已由另一端回答；read查看最新进展。');
      if(question.questions.length!==answers.items.length||!question.questions.every(q=>answers.items.some(a=>a.id===q.id)))throw new Error('answers需逐项对应当前问题id');
      admissions[job.id]={sessionId:id,requestId,at:Date.now(),afterSeq,answerTo:answers.call_id};save();
      question.resolve({answers:answers.items});
      return {thread_id:id,turn_id:'dsh-'+job.id,state:'submitted',answered_question:true};
    }
    // requestId is also the DSH controller's own durable admission deduplication key.
    admissions[job.id]={sessionId:id,requestId,at:Date.now(),afterSeq};save();
    try{await ctx.sessionController.prompt({sessionId:id,requestId,content:[{type:'text',text:job.text}],mode:'queue',clientTimeZone:'Asia/Singapore'},signal);}
    catch(error){delete admissions[job.id];save();throw error;}
    return {thread_id:id,turn_id:'dsh-'+job.id,state:'submitted',terminal_confirmed:false};
  }
  const server=http.createServer(async(req,res)=>{
    const actual=Buffer.from(req.headers.authorization??''),expected=Buffer.from('Bearer '+token);
    if(actual.length!==expected.length||!crypto.timingSafeEqual(actual,expected)){res.writeHead(401);res.end();return;}
    try{
      const chunks=[];for await(const chunk of req)chunks.push(chunk);
      const job=JSON.parse(Buffer.concat(chunks).toString('utf8'));
      const result=await execute(job);res.writeHead(200,{'content-type':'application/json'});res.end(JSON.stringify(result));
    }catch(error){res.writeHead(400,{'content-type':'application/json'});res.end(JSON.stringify({error:String(error?.message??error)}));}
  });
  server.listen(config.port??55359,'127.0.0.1',()=>fs.writeFileSync(path.join(directory,'host.json'),JSON.stringify({url:'http://127.0.0.1:'+server.address().port,pid:process.pid,at:Date.now()}),{mode:0o600}));
  ctx.on('dispose',()=>{server.close();for(const q of questions.values())q.reject(new Error('DSH bridge stopped'));questions.clear();});
  return {execute,server};
}

export function apply(ctx,config){createBridge(ctx,config);}
