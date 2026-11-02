"""Claude Code CLI bridge. Stream JSON control protocol; no extra package needed."""
import argparse,collections,json,os,subprocess,sys,threading,time,uuid
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent.parent/'fatfish-codex'))
try:from worker import SshBridge
except ImportError:SshBridge=None
HIDDEN=0x08000000 if os.name=='nt' else 0
TITLE_HEAD_LINES=200

def atomic(path,value):
    tmp=path.with_suffix('.pending');tmp.write_text(json.dumps(value,ensure_ascii=False),encoding='utf-8');os.replace(tmp,path)
def text_of(message):
    content=(message.get('message') or {}).get('content',[])
    if isinstance(content,str):return content
    return '\n'.join(b.get('text','') for b in content if b.get('type')=='text')

class ClaudeExecutor:
    def __init__(self,root,executable,projects=None):
        self.root=Path(root);self.root.mkdir(parents=True,exist_ok=True)
        (self.root/'runs').mkdir(exist_ok=True);(self.root/'workspace').mkdir(exist_ok=True)
        self.executable=str(executable);self.projects=Path(projects or Path.home()/'.claude/projects')
        self.lock=threading.RLock();self.processes={};self.inputs={}
        self.path=self.root/'jobs.json';self.jobs=json.loads(self.path.read_text(encoding='utf-8')) if self.path.exists() else {}
        self.routes_path=self.root/'routes.json';self.routes=json.loads(self.routes_path.read_text(encoding='utf-8')) if self.routes_path.exists() else {}
        for job in self.jobs.values():
            if job.get('state') in ('starting','running','waiting_input','queued'):
                job.update(state='unknown',status='unknown',terminal_confirmed=True,text='连接器曾重启，本次执行结果需read核对；没有重复执行。')
        self.save()
    def save(self):
        with self.lock:atomic(self.path,self.jobs)
    def transcript(self,sid):
        if str(uuid.UUID(sid))!=sid.lower():raise ValueError('需要Claude Code真实session UUID')
        return next(self.projects.glob('*/'+sid+'.jsonl'),None)
    def metadata(self,sid):
        route=self.routes.get(sid,{})
        file=self.transcript(sid)
        meta=dict(thread_id=sid,name=route.get('name'),cwd=route.get('cwd'),initialized=file is not None)
        if file:
            with file.open(encoding='utf-8') as stream:
                for index,line in zip(range(TITLE_HEAD_LINES),stream):
                    try:row=json.loads(line)
                    except ValueError:continue
                    if index<12:
                        if row.get('cwd'):meta['cwd']=row['cwd']
                        if row.get('type')=='user' and not meta['name']:meta['name']=text_of(row).strip()[:90]
                    title=row.get('customTitle')
                    if row.get('type')=='custom-title' and isinstance(title,str) and title.strip():meta['name']=title
            with file.open('rb') as stream:
                stream.seek(max(0,file.stat().st_size-12000));tail=stream.read().decode('utf-8',errors='replace')
            for line in tail.splitlines():
                try:row=json.loads(line)
                except ValueError:continue
                title=row.get('customTitle')
                if row.get('type')=='custom-title' and isinstance(title,str) and title.strip():meta['name']=title
            meta['updated_at']=file.stat().st_mtime
        elif sid not in self.routes:raise ValueError('本机找不到该Claude Code会话')
        return meta
    def read(self,sid):
        meta=self.metadata(sid);messages=collections.deque(maxlen=12);file=self.transcript(sid)
        if file:
            with file.open(encoding='utf-8') as stream:
                for line in stream:
                    try:row=json.loads(line)
                    except ValueError:continue
                    if row.get('isSidechain') or row.get('type') not in ('assistant','user'):continue
                    text=text_of(row)
                    if text:messages.append({'role':row['type'],'text':text,'uuid':row.get('uuid'),'timestamp':row.get('timestamp')})
        with self.lock:
            active=next((j for j in reversed(list(self.jobs.values())) if not j.get('answer_to') and j.get('thread_id')==sid and j.get('state') in ('starting','running','waiting_input','queued')),None)
            pending=active.get('pending_questions',[]) if active else []
            status=active['state'] if active else 'idle'
        remaining=6500;result=[]
        for row in reversed(messages):
            if remaining<=0:break
            cap=min(remaining,4000 if not result else 1200);text=row['text']
            result.append(dict(row,text=text[:cap],truncated=len(text)>cap,phase='final_answer' if not result and row['role']=='assistant' and not active else 'commentary' if row['role']=='assistant' else None));remaining-=min(cap,len(text))
        return dict(meta,status=status,messages=list(reversed(result)),pending_questions=pending)
    def write(self,jid,value):
        with self.lock:
            process=self.processes[jid];process.stdin.write(json.dumps(value,ensure_ascii=False)+'\n');process.stdin.flush()
    def runner(self,jid):
        with self.lock:
            job=self.jobs[jid];sid=job['thread_id'];meta=self.metadata(sid);exists=meta['initialized']
            cwd=meta.get('cwd') or str(self.root/'workspace')
            command=[self.executable,'-p','--input-format','stream-json','--output-format','stream-json','--verbose','--replay-user-messages','--permission-prompt-tool','stdio']
            command += ['--resume' if exists else '--session-id',sid]
            if self.routes.get(sid,{}).get('model'):command+=['--model',self.routes[sid]['model']]
            if not exists and meta.get('name'):command+=['--name',meta['name']]
            stderr=(self.root/'runs'/(jid+'.stderr.log')).open('w',encoding='utf-8')
            process=subprocess.Popen(command,cwd=cwd,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=stderr,text=True,encoding='utf-8',errors='replace',bufsize=1,creationflags=HIDDEN)
            self.processes[jid]=process;job.update(state='running',pid=process.pid);self.save()
        self.write(jid,{'type':'control_request','request_id':'init-'+jid,'request':{'subtype':'initialize'}})
        try:
            with (self.root/'runs'/(jid+'.jsonl')).open('a',encoding='utf-8') as log:
                for line in process.stdout:
                    log.write(line);log.flush()
                    try:value=json.loads(line)
                    except ValueError:continue
                    kind=value.get('type')
                    if kind=='control_response' and value.get('response',{}).get('request_id')=='init-'+jid:
                        if value['response'].get('subtype')=='error':raise RuntimeError(value['response'].get('error'))
                        self.write(jid,{'type':'user','session_id':sid,'message':{'role':'user','content':job['prompt']},'parent_tool_use_id':None,'uuid':str(uuid.uuid5(uuid.NAMESPACE_URL,'fatfish-claude:'+jid))})
                    elif kind=='control_request':
                        request=value.get('request',{});rid=value['request_id']
                        if request.get('subtype')=='can_use_tool':
                            if request.get('tool_name')=='AskUserQuestion':
                                with self.lock:
                                    self.inputs[rid]=(jid,request.get('input',{}));job.update(state='waiting_input',pending_questions=[{'call_id':rid,'questions':request.get('input',{}).get('questions',[])}]);self.save()
                            else:
                                self.write(jid,{'type':'control_response','response':{'subtype':'success','request_id':rid,'response':{'behavior':'allow','updatedInput':request.get('input',{})}}})
                        else:
                            self.write(jid,{'type':'control_response','response':{'subtype':'error','request_id':rid,'error':'Unsupported control request: '+str(request.get('subtype'))}})
                    elif kind=='control_cancel_request':
                        with self.lock:self.inputs.pop(value.get('request_id'),None)
                    elif kind=='result':
                        failed=value.get('is_error') is True or value.get('subtype')!='success'
                        with self.lock:
                            job.update(state='failed' if failed else 'completed',status='failed' if failed else 'completed',terminal_confirmed=True,text=str(value.get('result') or '; '.join(value.get('errors') or []) or '本轮结束，未取得正文。')[-6500:],result_session_id=value.get('session_id'),result_uuid=value.get('uuid'),usage=value.get('usage'),pending_questions=[]);self.save()
                        process.stdin.close()
            process.wait()
            with self.lock:
                if not job.get('terminal_confirmed'):
                    job.update(state='interrupted' if job.get('stop_requested') else 'failed',status='interrupted' if job.get('stop_requested') else 'failed',terminal_confirmed=True,text='Claude Code进程已退出，未取得result回执；read查看已写入内容。');self.save()
        except Exception as error:
            with self.lock:job.update(state='failed',status='failed',terminal_confirmed=True,text=type(error).__name__+': '+str(error)[:1000]);self.save()
            if process.poll() is None:process.terminate()
        finally:
            stderr.close()
            with self.lock:
                self.processes.pop(jid,None)
                for rid,(parent,_) in list(self.inputs.items()):
                    if parent==jid:self.inputs.pop(rid,None)
    def advance(self):
        with self.lock:
            busy={j['thread_id'] for j in self.jobs.values() if not j.get('answer_to') and j.get('state') in ('starting','running','waiting_input')}
            for jid,job in self.jobs.items():
                if job.get('state')=='queued' and job['thread_id'] not in busy:
                    busy.add(job['thread_id']);job['state']='starting';threading.Thread(target=self.launch,args=(jid,),daemon=True).start()
    def launch(self,jid):
        try:self.runner(jid)
        except Exception as error:
            with self.lock:self.jobs[jid].update(state='failed',status='failed',terminal_confirmed=True,text=type(error).__name__+': '+str(error)[:1000]);self.save()
    def execute(self,request):
        action=request['action'];sid=request.get('thread_id')
        if action=='status':return {'online':True,'pid':os.getpid(),'running':len(self.processes)}
        if action=='list':
            files=sorted(self.projects.glob('*/*.jsonl'),key=lambda p:p.stat().st_mtime,reverse=True);query=request.get('query','').lower();rows=[]
            ids=list(dict.fromkeys(list(reversed(self.routes))+[p.stem for p in files]))
            for identifier in ids:
                try:meta=self.metadata(identifier)
                except (ValueError,OSError):continue
                if not query or query in json.dumps(meta,ensure_ascii=False).lower():rows.append(dict(meta,id=identifier))
                if len(rows)>=20:break
            return {'threads':rows}
        if action=='create':
            with self.lock:
                reference=self.metadata(request['source_thread_id']) if request.get('source_thread_id') else {}
                sid=str(uuid.uuid5(uuid.NAMESPACE_URL,'fatfish-claude-create:'+request['id']));self.routes.setdefault(sid,{'cwd':request.get('cwd') or reference.get('cwd') or str(self.root/'workspace'),'name':request.get('title'),'model':request.get('model')});atomic(self.routes_path,self.routes)
            return {'thread_id':sid,'state':'created','session_initialized':self.transcript(sid) is not None,'note':'已预留空会话编号，首次发送时由Claude Code写入原生历史。'}
        if action=='read':return self.read(sid)
        if action=='watch':
            with self.lock:
                job=self.jobs.get(request['id'])
                if not job:return {'state':'unknown','terminal_confirmed':False}
                if job.get('answer_to'):job=self.jobs[job['answer_to']]
                if job.get('pending_questions'):
                    questions=job['pending_questions'];return {'state':'waiting_input','status':'completed','terminal_confirmed':True,'text':'Claude Code正在提问：\n'+json.dumps(questions,ensure_ascii=False)+'\n请用claude_code continue(job_id=当前任务ID,answers={call_id,answers:{问题原文:回答}})代答，然后等待最终结果。'}
                return {k:job[k] for k in ('state','status','terminal_confirmed','text','result_session_id','result_uuid') if k in job}
        if action=='stop':
            with self.lock:
                stopped=[]
                for jid,job in self.jobs.items():
                    if job.get('thread_id')!=sid:continue
                    if jid in self.processes:
                        job['stop_requested']=True;self.write(jid,{'type':'control_request','request_id':'stop-'+jid,'request':{'subtype':'interrupt'}});stopped.append(jid)
                    elif job.get('state')=='queued':job.update(state='interrupted',status='interrupted',terminal_confirmed=True,text='排队任务已停止。');stopped.append(jid)
                self.save()
            return {'thread_id':sid,'state':'interruption_requested','jobs':stopped}
        if action!='send':raise ValueError('Unsupported Claude operation')
        self.metadata(sid)
        with self.lock:
            jid=request['id']
            if jid in self.jobs:return {'thread_id':sid,'turn_id':'claude-'+jid,'state':'submitted','already_submitted':True}
            answers=None
            try:
                text=request['text'];begin=text.find('{');candidate=json.loads(text[begin:]) if begin>=0 else {}
                if candidate.get('call_id') and isinstance(candidate.get('answers'),dict):answers=candidate
            except (ValueError,KeyError):pass
            if answers:
                parent,original=self.inputs[answers['call_id']]
                if self.jobs[parent]['thread_id']!=sid:raise ValueError('问题不属于该会话')
                values=answers['answers']
                if request.get('auto_reply'):values={key:'[大肥鱼代答，不是jg本人] '+str(value) for key,value in values.items()}
                updated=dict(original,answers=values)
                self.jobs[jid]={'thread_id':sid,'answer_to':parent,'state':'running'}
                self.write(parent,{'type':'control_response','response':{'subtype':'success','request_id':answers['call_id'],'response':{'behavior':'allow','updatedInput':updated}}})
                self.inputs.pop(answers['call_id']);self.jobs[parent].update(state='running',pending_questions=[]);self.save()
            else:self.jobs[jid]={'thread_id':sid,'prompt':request['text'],'state':'queued','terminal_confirmed':False};self.save()
        self.advance()
        return {'thread_id':sid,'turn_id':'claude-'+jid,'state':'submitted','terminal_confirmed':False}

def run(config):
    root=Path(config['state_file']).parent;executor=ClaudeExecutor(root,config['claude_executable']);token=Path(config['token_file']).read_text().strip()
    def state(value):atomic(Path(config['state_file']),dict(value,pid=os.getpid(),at=time.time()))
    while True:
        bridge=None
        try:
            bridge=SshBridge(config,token)
            def post(value):return bridge.post(dict(value,backend='claude'))
            while True:
                executor.advance();poll=post({'action':'poll','protocol_version':5})
                if poll.get('backend')!='claude':raise RuntimeError('Server has not enabled the Claude queue')
                job=poll.get('job')
                if job:
                    try:payload={'action':'result','id':job['id'],'claim':job['claim'],'result':executor.execute(job)}
                    except Exception as error:payload={'action':'result','id':job['id'],'claim':job['claim'],'error':True,'result':{'error':type(error).__name__,'message':str(error)[:1000]}}
                    post(payload)
                for watch in poll.get('watches',[]):
                    value=executor.execute({'action':'watch','id':watch['job_id']})
                    if value.get('terminal_confirmed'):post({'action':'completed','job_id':watch['job_id'],'turn_id':watch['turn_id'],'status':value['status'],'text':value.get('text',''),'terminal_confirmed':True,'status_source':'claude_stream_result' if value['state']!='waiting_input' else 'claude_control_question','completion_outcome':value['state']})
                state({'online':True,'running':len(executor.processes)});time.sleep(1 if job or poll.get('watches') else 2)
        except Exception as error:state({'online':False,'error':type(error).__name__,'message':str(error)[:250]})
        finally:
            if bridge:bridge.close()
        time.sleep(5)
if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--config',required=True);args=parser.parse_args();config=json.loads(Path(args.config).read_text(encoding='utf-8-sig'))
    import msvcrt
    lock=Path(config['state_file']).with_suffix('.lock').open('a+b');lock.write(b'0');lock.flush();lock.seek(0)
    try:msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
    except OSError:raise SystemExit(0)
    run(config)
