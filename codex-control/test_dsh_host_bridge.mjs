import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {once} from 'node:events';
import {createBridge} from './dsh-host-bridge.mjs';
const directory=fs.mkdtempSync(path.join(os.tmpdir(),'fatfish-dsh-test-'));
const events=[];const handlers=new Map();let calls=0;
const ctx={
  on:(name,fn)=>handlers.set(name,fn),
  agents:{get:()=>({status:'idle'})},
  sessionController:{
    inspect:async()=>({meta:{cwd:'C:/work'},events}),
    prompt:async request=>{calls++;events.push({type:'turn/start',seq:events.length,data:{turn:'turn-1'}});events.push({type:'user/message',seq:events.length,data:{source:{rpcId:request.requestId},content:request.content}});},
    list:async()=>({items:[{sessionId:'session-1',projections:{values:{title:'原有对话'}},cwd:'C:/work'}]})
  }
};
const bridge=createBridge(ctx,{directory,port:0});await once(bridge.server,'listening');
try{
  assert.equal((await bridge.execute({action:'list',query:'原有对话'})).threads[0].name,'原有对话');
  const job={action:'send',id:'1234567890abcdef12345678',thread_id:'session-1',text:'test'};
  assert.equal((await bridge.execute(job)).state,'submitted');
  assert.equal((await bridge.execute(job)).already_submitted,true);assert.equal(calls,1);
  assert.equal((await bridge.execute({action:'watch',id:job.id})).terminal_confirmed,false);
  events.push({type:'session/title',seq:events.length,data:{title:'原有对话'}});
  events.push({type:'assistant/message',seq:events.length,data:{message:{content:[{type:'text',text:'完成'}]}}});
  events.push({type:'turn/end',seq:events.length,data:{turn:'turn-1',reason:'completed'}});
  const result=await bridge.execute({action:'watch',id:job.id});assert.equal(result.terminal_confirmed,true);assert.equal(result.text,'完成');
  // A later human message is not attributed to this task's answer.
  events.push({type:'turn/start',seq:events.length,data:{turn:'turn-2'}});
  events.push({type:'assistant/message',seq:events.length,data:{content:[{type:'text',text:'另一个回答'}]}});
  assert.equal((await bridge.execute({action:'watch',id:job.id})).text,'完成');
  const read=await bridge.execute({action:'read',thread_id:'session-1'});assert.equal(read.messages.at(-1).text,'另一个回答');assert.equal(read.name,'原有对话');assert.equal(read.messages.find(m=>m.phase==='final_answer').text,'完成');
  await bridge.execute({...job,id:'2234567890abcdef12345678'});
  const controller=new AbortController();
  const answering=handlers.get('user-questions/request')({agent:{id:'session-1'},questions:[{id:'q1',question:'选哪一种？'}],signal:controller.signal},()=>new Promise(()=>{}));
  const question=(await bridge.execute({action:'read',thread_id:'session-1'})).pending_questions[0];assert.ok(question.call_id);
  const waiting=await bridge.execute({action:'watch',id:'2234567890abcdef12345678'});assert.equal(waiting.state,'waiting_input');
  const response=await bridge.execute({...job,id:'3234567890abcdef12345678',text:JSON.stringify({call_id:question.call_id,items:[{id:'q1',selected:[],custom:'第一种'}]})});
  assert.equal(response.answered_question,true);assert.equal((await answering).answers[0].custom,'第一种');
  assert.equal((await bridge.execute({action:'read',thread_id:'session-1'})).pending_questions.length,0);
  events.push({type:'turn/end',seq:events.length,data:{turn:'turn-1',reason:{kind:'error'}}});
  assert.equal((await bridge.execute({action:'watch',id:'3234567890abcdef12345678'})).status,'failed');
  console.log('DSH host: completion, deduplication, human-turn separation, read, question wake/answer and error status passed');
}finally{handlers.get('dispose')();fs.rmSync(directory,{recursive:true,force:true});}
