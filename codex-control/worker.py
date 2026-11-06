"""Local Codex controller over a fixed outbound SSH channel; account auth stays local."""
import argparse,datetime as dt,hashlib,json,math,os,queue,re,shutil,subprocess,threading,time
import base64
from pathlib import Path

HIDDEN=getattr(subprocess,'CREATE_NO_WINDOW',0)

class WatcherYield(Exception):pass

def resolve_codex_executable(configured):
    """Recover after a desktop update removes a versioned Codex cache path."""
    if Path(configured).is_file():return str(configured)
    local=Path(os.environ.get('LOCALAPPDATA',str(Path.home()/'AppData'/'Local')))
    candidates=[p for p in (local/'OpenAI'/'Codex'/'bin').glob('*/codex.exe') if p.is_file()]
    if candidates:return str(max(candidates,key=lambda p:p.stat().st_mtime))
    resolved=shutil.which('codex')
    if resolved:return resolved
    raise FileNotFoundError('Codex executable is unavailable; no installed desktop cache or PATH entry found')

def ensure_video_reader(config):
    """Bring back the configured media worker; its profile lock prevents duplicates."""
    root=Path(config['state_file']).parent
    path=root/'video-reader.json';script=root/'media_connector.py'
    if not path.is_file() or not script.is_file():return {'state':'not_configured'}
    try:
        import msvcrt
        media=json.loads(path.read_text(encoding='utf-8'))
        with Path(media['state_file']).with_suffix('.lock').open('a+b') as lock:
            if lock.seek(0,2)==0:lock.write(b'0');lock.flush()
            lock.seek(0)
            try:msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
            except OSError:return {'state':'already_running'}
            msvcrt.locking(lock.fileno(),msvcrt.LK_UNLCK,1)
        python=Path(media['python']);windowless=python.with_name('pythonw.exe')
        if windowless.is_file():python=windowless
        process=subprocess.Popen([str(python),str(script),'--config',str(path)],cwd=root,
            stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=HIDDEN)
        return {'state':'started','pid':process.pid}
    except Exception as exc:return {'state':'start_failed','error':type(exc).__name__}

class AppServer:
    def __init__(self,executable,routes_file=None,writer_strategy='fork'):
        if writer_strategy not in ('fork','queue'):raise ValueError('Unknown writer conflict strategy')
        self.writer_strategy=writer_strategy;self.routes_file=Path(routes_file) if routes_file else None
        self.routes=self.load_routes()
        self.pending={};self.lock=threading.Lock();self.rpc_lock=threading.RLock();self.counter=0;self.wait_callback=None
        self.events=queue.Queue(maxsize=64);self.final={};self.active={}
        self.usage_cache=None
        self.watcher_seen={}
        self.process=subprocess.Popen([resolve_codex_executable(executable),'app-server','--stdio'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,text=True,encoding='utf-8',bufsize=1,creationflags=HIDDEN)
        threading.Thread(target=self.reader,daemon=True).start()
        try:
            self.rpc('initialize',{'clientInfo':{'name':'fatfish_controller','title':'Fatfish local Codex controller','version':'0.2'},'capabilities':{'experimentalApi':True}})
            self.write({'method':'initialized'})
        except Exception:
            self.close();raise

    def load_routes(self):
        if not self.routes_file or not self.routes_file.exists():return {}
        value=json.loads(self.routes_file.read_text(encoding='utf-8'))
        if not isinstance(value,dict):raise ValueError('Invalid remote-thread routes')
        for source,row in value.items():
            if not re.fullmatch(r'[a-zA-Z0-9_-]{12,80}',source) or not isinstance(row,dict) or not re.fullmatch(r'[a-zA-Z0-9_-]{12,80}',str(row.get('thread_id',''))):raise ValueError('Invalid remote-thread route')
        return value

    def save_routes(self):
        if not self.routes_file:return
        pending=self.routes_file.with_suffix('.pending')
        pending.write_text(json.dumps(self.routes,ensure_ascii=False,indent=2),encoding='utf-8')
        os.chmod(pending,0o600);os.replace(pending,self.routes_file)

    def write(self,value):
        with self.lock:self.process.stdin.write(json.dumps(value,ensure_ascii=False)+'\n');self.process.stdin.flush()

    def reader(self):
        try:
            for line in self.process.stdout:
                try:value=json.loads(line)
                except ValueError:continue
                if 'id' in value and 'method' not in value:
                    pending=self.pending.get(value['id'])
                    if pending:pending.put(value)
                    continue
                method=value.get('method','');params=value.get('params') or {}
                if 'id' in value:
                    # Desktop-only dynamic tools and human approvals are not silently granted.
                    if method=='item/tool/call':
                        self.write({'id':value['id'],'result':{'success':False,'contentItems':[{'type':'inputText','text':'当前远程入口提供原生文件和终端工具；桌面专属工具可由jg在桌面操作。'}]}})
                    else:self.write({'id':value['id'],'error':{'code':-32601,'message':'This remote client cannot grant interactive approvals. Ask the owner.'}})
                    continue
                thread=params.get('threadId');turn=params.get('turn') or {}
                if method=='turn/started' and thread:self.active[thread]=turn.get('id')
                if method=='item/completed':
                    item=params.get('item') or {}
                    if item.get('type')=='agentMessage' and item.get('phase')!='commentary' and thread:self.final[thread]=str(item.get('text',''))[:6000]
                if method=='turn/completed' and thread:
                    self.active.pop(thread,None)
                    final_text=self.final.pop(thread,'')
                    observation=self.turn_observation(turn,{'event':'turn_aborted' if turn.get('status') in
                        ('interrupted','cancelled') else 'task_complete','final_text':final_text})
                    event=dict(observation,thread_id=thread,turn_id=turn.get('id',''),text=final_text,
                        status_source='live_turn_completed_event')
                    try:self.events.put_nowait(event)
                    except queue.Full:pass
        finally:
            for pending in list(self.pending.values()):
                try:pending.put_nowait({'error':{'message':'Local Codex connection closed'}})
                except queue.Full:pass

    def rpc(self,method,params,timeout=35):
        with self.rpc_lock:return self._rpc(method,params,timeout)

    def _rpc(self,method,params,timeout=35):
        self.counter+=1;identifier=self.counter;pending=queue.Queue(maxsize=1);self.pending[identifier]=pending
        try:
            self.write({'id':identifier,'method':method,'params':params});deadline=time.monotonic()+timeout
            while True:
                remaining=deadline-time.monotonic()
                if remaining<=0:raise TimeoutError('Codex RPC timed out')
                try:result=pending.get(timeout=min(1,remaining));break
                except queue.Empty:
                    if self.wait_callback:self.wait_callback()
            if 'error' in result:raise RuntimeError(str(result['error'].get('message','Codex RPC error'))[:500])
            return result.get('result') or {}
        finally:self.pending.pop(identifier,None)

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:self.process.wait(timeout=8)
            except subprocess.TimeoutExpired:self.process.kill()

    @staticmethod
    def _usage_number(value):
        return value if type(value) in (int,float) and math.isfinite(value) else None

    @classmethod
    def sanitize_usage(cls,payload,observed_at=None):
        if not isinstance(payload,dict):raise RuntimeError('Codex usage response was not an object')
        multi=payload.get('rateLimitsByLimitId')
        source=multi if isinstance(multi,dict) and multi else {'codex':payload.get('rateLimits')}
        limits=[]
        for key,snapshot in source.items():
            if not isinstance(snapshot,dict):continue
            def window(name):
                value=snapshot.get(name)
                if value is None:return None
                if not isinstance(value,dict):return {'used_percent':None,'remaining_percent':None,
                    'window_duration_mins':None,'resets_at':None}
                used=cls._usage_number(value.get('usedPercent'))
                return {'used_percent':used,'remaining_percent':max(0,min(100,100-used)) if used is not None else None,
                    'window_duration_mins':value.get('windowDurationMins') if type(value.get('windowDurationMins')) is int else None,
                    'resets_at':value.get('resetsAt') if type(value.get('resetsAt')) is int else None}
            limit_id=str(snapshot.get('limitId') or key)[:80]
            name=snapshot.get('limitName')
            plan=snapshot.get('planType')
            limits.append({'limit_id':limit_id,'limit_name':str(name)[:120] if isinstance(name,str) else None,
                'plan_type':str(plan)[:40] if isinstance(plan,str) else None,
                'primary':window('primary'),'secondary':window('secondary'),
                'rate_limit_reached_type':str(snapshot.get('rateLimitReachedType'))[:80] if snapshot.get('rateLimitReachedType') is not None else None})
        permission=payload.get('ordinaryUsageAllowed')
        return {'observed_at':observed_at or dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds'),
            'ordinary_usage_allowed':permission if type(permission) is bool else None,'limits':limits}

    def read_usage(self,force=False):
        if not force and self.usage_cache and time.time()-self.usage_cache[0]<120:
            return dict(self.usage_cache[1],cache_state='fresh')
        try:raw=self.rpc('account/rateLimits/read',{},timeout=20)
        except Exception as exc:
            raise RuntimeError('Codex usage query failed: '+type(exc).__name__) from None
        result=self.sanitize_usage(raw)
        self.usage_cache=(time.time(),result)
        return dict(result,cache_state='fresh')

    def queue_on_owner(self,job,identifier):
        # Same original thread, no writer takeover or hidden fork. A stable client ID
        # identifies an already-pending submission; the native add endpoint itself
        # does not deduplicate this field.
        source=str(job.get('id') or hashlib.sha256((identifier+'\0'+job['text']).encode()).hexdigest())
        client_id='fatfish-'+source
        queued=None;cursor=None
        for _ in range(5):
            params={'threadId':identifier,'limit':50}
            if cursor:params['cursor']=cursor
            page=self.rpc('thread/queue/list',params)
            queued=next((item for item in page.get('data',[]) if item.get('clientUserMessageId')==client_id),None)
            cursor=page.get('nextCursor')
            if queued or not cursor:break
        if not queued and cursor:raise RuntimeError('Too many pending Codex messages to verify this submission; not added')
        existing=bool(queued)
        if not queued:
            response=self.rpc('thread/queue/add',{'threadId':identifier,'clientUserMessageId':client_id,
                'input':[{'type':'text','text':job['text']}]})
            queued=response.get('queuedSubmission') or {}
        if not queued.get('id'):raise RuntimeError('Codex did not confirm the queued submission; do not resend blindly')
        return {'thread_id':identifier,'state':'queued_on_owner','queued_submission_id':queued['id'],
            'client_user_message_id':client_id,'started':False,'already_queued':existing,'completion_tracking':'read_thread',
            'note':'已加入原会话的Codex原生队列，等待桌面执行端开启回合。可用read查看排队和实际进展。'}

    @staticmethod
    def observed_at():
        return dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')

    def rollout_lifecycle(self,thread_id,turn_ids,thread=None):
        """Read bounded durable lifecycle evidence for a registered native thread.

        Paginated desktop history can label a running turn interrupted. Never
        derive a terminal state from that projection alone. Paths come only from
        thread/read, must stay in this profile's sessions, and must identify the
        exact registered thread. A missing event in the 4 MiB tail is unknown.
        """
        managed=any(row.get('native') and row.get('thread_id')==thread_id for row in getattr(self,'routes',{}).values())
        wanted={str(x) for x in turn_ids if x}
        if not managed or not wanted:return {}
        try:
            if thread is None:
                thread=getattr(self,'thread_metadata',{}).get(thread_id)
                if thread is None:
                    thread=self.rpc('thread/read',{'threadId':thread_id},timeout=8)['thread']
                    if not hasattr(self,'thread_metadata'):self.thread_metadata={}
                    self.thread_metadata[thread_id]=thread
            raw=thread.get('path')
            if not isinstance(raw,str):return {}
            # Codex on Windows returns the extended-length local path spelling.
            if raw.startswith('\\\\?\\'):raw=raw[4:]
            home=Path(os.environ.get('CODEX_HOME') or Path.home()/'.codex').resolve()
            path=Path(raw).resolve()
            if not any(path.is_relative_to((home/folder).resolve()) for folder in ('sessions','archived_sessions')):return {}
            if not path.is_file() or path.is_symlink():return {}
            with path.open('rb') as source:
                first=source.readline(1024*1024)
                identity=json.loads(first)
                if identity.get('type')!='session_meta' or identity.get('payload',{}).get('id')!=thread_id:return {}
                source.seek(0,os.SEEK_END);end=source.tell();start=max(0,end-4*1024*1024);source.seek(start)
                if start:source.readline(1024*1024)
                data=source.read(4*1024*1024)
            found={}
            for line in data.splitlines():
                try:record=json.loads(line)
                except (ValueError,UnicodeDecodeError):continue
                if record.get('type')!='event_msg':continue
                event=record.get('payload') or {};identifier=event.get('turn_id');kind=event.get('type')
                if identifier not in wanted or kind not in ('task_started','task_complete','turn_aborted'):continue
                found[identifier]={'event':kind,'at':record.get('timestamp')}
                if kind=='task_complete' and isinstance(event.get('last_agent_message'),str):
                    found[identifier]['final_text']=event['last_agent_message'][:6001]
                if kind=='task_complete' and event.get('error'):
                    found[identifier]['error']=self.turn_error(event.get('error'))
            return found
        except (OSError,ValueError,TypeError,KeyError,RuntimeError):return {}

    @classmethod
    def turn_error(cls,error):
        if isinstance(error,dict):
            code=error.get('code') or error.get('codex_error_info') or error.get('codexErrorInfo')
            return {'code':str(code)[:100] if code is not None else None,
                'message':str(error.get('message') or 'Codex reported a turn error')[:500]}
        return {'code':None,'message':str(error)[:500]}

    @classmethod
    def turn_observation(cls,turn,lifecycle=None):
        reported=str(turn.get('status','unknown'));evidence=lifecycle or {};kind=evidence.get('event')
        explicit_final=any(item.get('type')=='agentMessage' and item.get('phase')=='final_answer' and
            str(item.get('text','')).strip() for item in turn.get('items',[]))
        reply_present=explicit_final or bool(str(evidence.get('final_text') or '').strip())
        error=evidence.get('error') or turn.get('error')
        value={'reported_status':reported,'status':'unknown','execution_state':'pending','terminal_confirmed':False,
            'status_source':'persisted_snapshot_unconfirmed','observed_at':cls.observed_at(),'task_goal_status':'not_evaluated',
            'reply_present':reply_present,'completion_outcome':'unknown'}
        if kind=='task_started':value.update(status='inProgress',execution_state='running',status_source='rollout_task_started')
        elif kind=='task_complete':
            # task_complete closes the execution lifecycle even when no answer
            # was generated (for example usage-limit failure). It is not proof
            # of a successful turn, still less acceptance of the user's goal.
            failed=bool(error) or reported=='failed'
            value.update(status='failed' if failed else ('completed' if reply_present else 'unknown'),
                execution_state='failed' if failed else 'completed',terminal_confirmed=True,
                status_source='rollout_task_complete_failed' if failed else
                    ('rollout_task_complete' if reply_present else 'rollout_task_complete_without_final'),
                completion_outcome='failed' if failed else ('succeeded' if reply_present else 'unknown'))
            if error:value['error']=cls.turn_error(error)
        elif kind=='turn_aborted':
            value.update(status='interrupted',execution_state='completed',terminal_confirmed=True,
                status_source='rollout_turn_aborted',completion_outcome='interrupted')
        elif reported=='failed' or error:
            value.update(status='failed',execution_state='failed',terminal_confirmed=True,
                status_source='persisted_failed',completion_outcome='failed')
            if error:value['error']=cls.turn_error(error)
        elif reported=='inProgress':value.update(status='inProgress',execution_state='running',status_source='persisted_in_progress')
        elif reported=='completed' and explicit_final:
            value.update(status='completed',execution_state='completed',terminal_confirmed=True,
                status_source='persisted_completed_and_final',completion_outcome='succeeded')
        if evidence.get('at'):value['lifecycle_at']=evidence['at']
        return value

    def inspect_submission(self,thread_id,client_id=None,turn_id=None):
        cursor=None;queued=False
        for _ in range(4):
            params={'threadId':thread_id,'limit':50}
            if cursor:params['cursor']=cursor
            page=self.rpc('thread/queue/list',params)
            queued=queued or bool(client_id and any(q.get('clientUserMessageId')==client_id for q in page.get('data',[])))
            cursor=page.get('nextCursor')
            if not cursor:break
        if queued:return {'state':'queued'}
        cursor=None
        for _ in range(4):
            params={'threadId':thread_id,'limit':50,'sortDirection':'desc','itemsView':'full'}
            if cursor:params['cursor']=cursor
            page=self.rpc('thread/turns/list',params)
            for turn in page.get('data',[]):
                user=next((item for item in turn.get('items',[]) if item.get('type')=='userMessage' and
                    (client_id is None or (item.get('clientId') or item.get('client_id'))==client_id)),None)
                if turn_id and turn.get('id')!=turn_id:continue
                if not user:continue
                finals=[item for item in turn.get('items',[]) if item.get('type')=='agentMessage' and item.get('phase')=='final_answer']
                evidence=self.rollout_lifecycle(thread_id,[turn.get('id')]).get(turn.get('id'))
                observation=self.turn_observation(turn,evidence)
                full='\n'.join(str(item.get('text','')) for item in finals)
                if not full and evidence:full=evidence.get('final_text','')
                return dict(observation,state=observation['execution_state'],turn_id=turn.get('id'),text=full[:6000],
                    phase='final_answer' if full else None,final_truncated=len(full)>6000)
            cursor=page.get('nextCursor')
            if not cursor:break
        return {'state':'unknown'}

    def poll_watchers(self,watches):
        # Watch definitions live in the server database, so a connector restart or
        # SSH outage cannot lose an accepted native job. Only read persisted turns.
        events=[];now=time.time()
        if not hasattr(self,'watcher_seen'):self.watcher_seen={}
        if not hasattr(self,'watcher_observations'):self.watcher_observations={}
        valid=[w for w in watches if isinstance(w,dict) and w.get('job_id') and w.get('thread_id')]
        valid.sort(key=lambda w:self.watcher_seen.get(w['job_id'],0))
        for watch in valid[:1]:
            if getattr(self,'wait_callback',None):self.wait_callback()
            key=watch['job_id']
            if now-self.watcher_seen.get(key,0)<15:continue
            self.watcher_seen[key]=now
            try:result=self.inspect_submission(watch['thread_id'],watch.get('client_user_message_id'),watch.get('turn_id'))
            except WatcherYield:raise
            except Exception:continue
            if result.get('terminal_confirmed') is True:
                events.append(dict(action='completed',job_id=key,thread_id=watch['thread_id'],
                    turn_id=result['turn_id'],client_user_message_id=watch.get('client_user_message_id'),
                    status=result['status'],text=result['text'],**{k:result.get(k) for k in
                        ('terminal_confirmed','status_source','reported_status','observed_at','lifecycle_at','phase','final_truncated',
                            'task_goal_status','execution_state','completion_outcome','reply_present','error')}))
            elif result['state'] in ('running','pending'):
                # Correct old projection-based false terminal reports without
                # treating an observation as a completion or repeatedly waking
                # the companion on every poll.
                signature=tuple(result.get(k) for k in ('turn_id','execution_state','status_source','reported_status'))
                if self.watcher_observations.get(key)!=signature:
                    self.watcher_observations[key]=signature
                    events.append(dict(action='observation',job_id=key,thread_id=watch['thread_id'],
                        turn_id=result.get('turn_id'),client_user_message_id=watch.get('client_user_message_id'),
                        **{k:result.get(k) for k in ('execution_state','terminal_confirmed','status_source','reported_status',
                            'observed_at','lifecycle_at','task_goal_status')}))
            elif result['state']=='unknown' and now-float(watch.get('created',now))>300:
                events.append(dict(action='unknown',job_id=key,client_user_message_id=watch.get('client_user_message_id'),
                    turn_id=watch.get('turn_id')))
        if len(self.watcher_seen)>200:
            self.watcher_seen={k:v for k,v in self.watcher_seen.items() if now-v<3600}
            self.watcher_observations={k:v for k,v in self.watcher_observations.items() if k in self.watcher_seen}
        return events

    def poll_native_threads(self,watches):
        events=[];now=time.time()
        if not hasattr(self,'native_watch_seen'):self.native_watch_seen={}
        registered={row['thread_id'] for row in self.routes.values() if row.get('native') and row.get('shared')}
        watches=sorted(watches,key=lambda w:self.native_watch_seen.get(w.get('thread_id'),0))
        for watch in watches[:1]:
            if getattr(self,'wait_callback',None):self.wait_callback()
            thread=watch.get('thread_id')
            if thread not in registered or now-self.native_watch_seen.get(thread,0)<15:continue
            self.native_watch_seen[thread]=now
            try:
                turns=self.rpc('thread/turns/list',{'threadId':thread,'limit':5,'sortDirection':'desc','itemsView':'full'}).get('data',[])
                lifecycle=self.rollout_lifecycle(thread,[turn.get('id') for turn in turns])
            except (RuntimeError,OSError,ValueError):continue
            cursor=watch.get('cursor_turn_id')
            boundary=next((i for i,turn in enumerate(turns) if turn.get('id')==cursor),None) if cursor else None
            candidates=turns[:boundary] if boundary is not None else turns
            eligible=[]
            for turn in candidates:
                # Native Codex turn IDs are time-ordered UUIDs. This also keeps a
                # briefly stale desktop snapshot from moving a durable cursor back.
                identifier=str(turn.get('id',''))
                if cursor and re.fullmatch(r'[0-9a-f-]{36}',identifier) and re.fullmatch(r'[0-9a-f-]{36}',cursor) and identifier<=cursor:continue
                evidence=lifecycle.get(identifier,{})
                observation=self.turn_observation(turn,evidence)
                if not observation['terminal_confirmed'] or observation['status'] not in ('completed','failed','unknown'):continue
                text='\n'.join(str(item.get('text','')) for item in turn.get('items',[]) if item.get('type')=='agentMessage' and item.get('phase')=='final_answer')
                if not text:text=str(evidence.get('final_text') or '')
                user='\n'.join(str(part.get('text','')) for item in turn.get('items',[]) if item.get('type')=='userMessage' for part in item.get('content',[]) if isinstance(part,dict))
                eligible.append(dict(observation,action='native_turn',thread_id=thread,turn_id=identifier,
                    text=text[:6000],user_text=user[:1500],phase='final_answer' if text else None,final_truncated=len(text)>6000))
            # First attachment backfills one latest result, then the durable
            # server cursor follows every new completed turn, including UI turns.
            events.extend(eligible[:1] if not cursor else reversed(eligible))
        return events

    def stop_delegated(self,identifier,job_id=None):
        expected='fatfish-'+job_id if job_id else None
        cursor=None;queued_rows=[]
        for _ in range(4):
            params={'threadId':identifier,'limit':50}
            if cursor:params['cursor']=cursor
            page=self.rpc('thread/queue/list',params)
            queued_rows.extend(page.get('data',[]))
            cursor=page.get('nextCursor')
            if not cursor:break
        if expected:queued_rows=[q for q in queued_rows if q.get('clientUserMessageId')==expected]
        turns=self.rpc('thread/turns/list',{'threadId':identifier,'limit':50,'sortDirection':'desc','itemsView':'full'})
        active=None
        for turn in turns.get('data',[]):
            if turn.get('status')!='inProgress':continue
            user=next((item for item in turn.get('items',[]) if item.get('type')=='userMessage' and
                str(item.get('clientId') or item.get('client_id') or '').startswith('fatfish-')),None)
            if not expected or (user and (user.get('clientId') or user.get('client_id'))==expected):
                active=turn.get('id');break
        if active:
            try:
                self.rpc('turn/interrupt',{'threadId':identifier,'turnId':active})
                return {'thread_id':identifier,'state':'interruption_requested','stopped':True,'turn_id':active}
            except Exception as exc:
                return {'thread_id':identifier,'state':'interrupt_failed','stopped':False,'error':type(exc).__name__}
        if queued_rows:
            # With no job filter, stop the oldest submission in this thread.
            targets=queued_rows if expected else queued_rows[:1]
            deleted=[];errors=[]
            for item in targets:
                try:
                    result=self.rpc('thread/queue/delete',{'threadId':identifier,'queuedSubmissionId':item.get('id')})
                    if result.get('deleted') is True:deleted.append(item.get('clientUserMessageId'))
                    else:errors.append('not_deleted')
                except Exception as exc:errors.append(type(exc).__name__)
            return {'thread_id':identifier,'state':'queue_cancelled' if deleted else 'queue_cancel_failed',
                'stopped':bool(deleted),'cancelled_count':len(deleted),'errors':errors[:3]}
        return {'thread_id':identifier,'state':'already_idle','stopped':False,'note':'该会话没有匹配的活动回合或待执行队列项。'}

    def fork_shared(self,requested,source_thread,identifier):
        params={'threadId':identifier,'excludeTurns':True,'deferGoalContinuation':True}
        # Prefer a completed boundary so an active desktop tool call is not copied
        # halfway through. The fork keeps the saved project/history configuration.
        try:
            recent=self.rpc('thread/turns/list',{'threadId':identifier,'limit':3,'sortDirection':'desc'})
            completed=next((turn['id'] for turn in recent.get('data',[]) if turn.get('status')=='completed'),None)
            if completed:params['lastTurnId']=completed
        except RuntimeError:pass
        remote=self.rpc('thread/fork',params)['thread'];remote_id=remote['id']
        name='大肥鱼共用 · '+str(source_thread.get('name') or Path(source_thread.get('cwd') or '.').name or '工作会话')[:90]
        try:self.rpc('thread/name/set',{'threadId':remote_id,'name':name})
        except RuntimeError:pass
        route={'thread_id':remote_id,'name':name,'source_thread_id':identifier,'created':time.time(),'shared':True}
        for source,old in list(self.routes.items()):
            if old.get('thread_id')==identifier:self.routes[source]=dict(route)
        self.routes[requested]=route;self.save_routes()
        return route

    def attach_shared(self,requested,thread=None):
        # The desktop remains the sole executor. Attaching only reads metadata;
        # it never resumes, takes a writer or forks another conversation.
        identifier=self.routes.get(requested,{}).get('thread_id',requested)
        if thread is None:thread=self.rpc('thread/read',{'threadId':identifier})['thread']
        route={'thread_id':identifier,'source_thread_id':identifier,'name':thread.get('name'),
            'created':time.time(),'shared':True,'native':True}
        self.routes[identifier]=route;self.save_routes()
        return dict(route,note='已连接原生Codex同一会话；桌面端负责执行，控制台通过原生队列追加消息。')

    def fork_and_send(self,job,requested,source_thread,identifier):
        route=self.fork_shared(requested,source_thread,identifier);remote_id=route['thread_id'];name=route['name']
        turn=self.rpc('turn/start',{'threadId':remote_id,'input':[{'type':'text','text':job['text']}]})['turn']
        return {'thread_id':remote_id,'source_thread_id':identifier,'turn_id':turn['id'],'name':name,'state':'submitted',
            'remote_session_created':True,'scope':'独立远程会话；复制原会话上下文，保留原桌面会话',
            'note':'任务已提交到独立远程会话，后续沿用此thread_id；原桌面会话未接管，提交不代表已完成。'}

    def execute(self,job):
        action=job['action']
        if action=='create':
            source=str(job['id'])
            existing=self.routes.get(source)
            if existing and existing.get('created_by_fatfish'):
                return {'thread_id':existing['thread_id'],'name':existing.get('name'),'state':'created','already_created':True}
            cwd=job.get('cwd')
            if not cwd:
                reference=job.get('source_thread_id') or next((r['thread_id'] for r in reversed(list(self.routes.values())) if r.get('native')),None)
                if reference:cwd=self.rpc('thread/read',{'threadId':reference})['thread'].get('cwd')
            if not cwd:raise ValueError('新建会话需要工作目录cwd')
            directory=Path(cwd)
            if not directory.is_absolute() or not directory.is_dir():raise ValueError('cwd必须是电脑上已存在的绝对目录')
            params={'cwd':str(directory)}
            if job.get('model'):params['model']=job['model']
            created=self.rpc('thread/start',params)['thread'];identifier=created['id']
            title=job.get('title') or '大肥鱼 · 新对话'
            self.routes[source]={'thread_id':identifier,'name':title,'created':time.time(),'shared':True,'created_by_fatfish':True}
            self.save_routes()
            warning=None
            try:self.rpc('thread/name/set',{'threadId':identifier,'name':title})
            except RuntimeError:warning='会话已创建，标题暂未保存；继续使用返回的thread_id。'
            return {'thread_id':identifier,'name':title,'cwd':str(directory),'state':'created','already_created':False,
                'note':warning or '已新建独立Codex对话；send可开始任务，read可查看，同一请求不会重复建会话。'}
        if action=='usage':return self.read_usage(force=True)
        if action=='list':
            args={'limit':8,'archived':False}
            if job.get('query'):args['searchTerm']=job['query']
            result=self.rpc('thread/list',args)
            rows=[];seen=set()
            for thread in result.get('data',[]):
                identifier=thread.get('id')
                if not identifier or identifier in seen:continue
                seen.add(identifier)
                rows.append({key:thread.get(key) for key in ('id','name','cwd','status')})
            owned={row['thread_id'] for row in self.routes.values()}
            native_routes={row['thread_id']:row for row in self.routes.values() if row.get('native') and row.get('shared')}
            for row in rows:
                if row['id'] in self.routes:
                    row['remote_thread_id']=self.routes[row['id']]['thread_id']
                    row['shared_thread_id']=self.routes[row['id']]['thread_id']
                if row['id'] in owned:row['remote_controlled']=True
                if row['id'] in native_routes:
                    row.update(remote_controlled=True,native=True,shared_thread_id=row['id'])
            native=next((r for r in reversed(list(self.routes.values())) if r.get('native')),None)
            query=str(job.get('query') or '').strip().casefold()
            missing=[];unavailable=0
            for identifier,route in reversed(list(native_routes.items())):
                if identifier in seen:continue
                if query and query not in str(route.get('name') or '').casefold() and query not in identifier.casefold():continue
                try:meta=self.rpc('thread/read',{'threadId':identifier})['thread']
                except RuntimeError:
                    unavailable+=1;continue
                row={key:meta.get(key) for key in ('id','name','cwd','status')}
                row.update(remote_controlled=True,native=True,shared_thread_id=identifier)
                missing.append(row);seen.add(identifier)
            return {'threads':missing+rows,'default_thread_id':native['thread_id'] if native else None,
                    'recent_results_have_more':bool(result.get('nextCursor')),'unavailable_native_count':unavailable,
                    'note':'显示最近一页和匹配的已登记原生共用会话；最近一页不代表全部历史。'}
        if action=='probe':
            directory=Path(__file__).resolve().parent/'smoke';directory.mkdir(exist_ok=True)
            thread=self.rpc('thread/start',{'ephemeral':True,'cwd':str(directory),'sandbox':'read-only','approvalPolicy':'never'})['thread']
            identifier=thread['id']
            turn=self.rpc('turn/start',{'threadId':identifier,'input':[{'type':'text','text':'连接验收。只回复“连接成功”，不要调用任何工具，不读写文件。'}]})['turn']
            return {'thread_id':identifier,'turn_id':turn['id'],'ephemeral':True,'state':'submitted'}
        requested=job['thread_id'];identifier=requested
        if action in ('read','send','stop','artifact'):identifier=self.routes.get(requested,{}).get('thread_id',requested)
        thread=self.rpc('thread/read',{'threadId':identifier})['thread']
        if not hasattr(self,'thread_metadata'):self.thread_metadata={}
        self.thread_metadata[identifier]=thread
        if action=='artifact':
            root=Path(thread.get('cwd') or '').resolve();path=Path(str(job.get('path','')))
            if not path.is_absolute():path=root/path
            path=path.resolve()
            if not path.is_relative_to(root) or not path.is_file():raise ValueError('artifact需要当前会话目录内的真实文件')
            if path.stat().st_size>128*1024:raise ValueError('artifact最多128KiB')
            raw=path.read_bytes()
            return {'thread_id':identifier,'path':str(path),'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest(),
                'content_base64':base64.b64encode(raw).decode()}
        if action=='read' and job.get('detail')=='status':
            result={'thread_id':identifier,'name':thread.get('name'),'cwd':thread.get('cwd'),'status':thread.get('status'),
                'request_state':'done','observed_at':self.observed_at(),'detail':'status',
                'note':'轻量读取会话与队列状态；执行终态以桥接账本中的真实turn回执为准，正文需要时使用detail=history。'}
            page=self.rpc('thread/queue/list',{'threadId':identifier,'limit':50})
            result['pending_messages']=[{'id':q.get('id'),'client_user_message_id':q.get('clientUserMessageId')}for q in page.get('data',[])]
            result['more_pending_messages']=bool(page.get('nextCursor'))
            return result
        if action=='read':
            result=self.rpc('thread/turns/list',{'threadId':identifier,'limit':3,'sortDirection':'desc','itemsView':'full'})
            lifecycle=self.rollout_lifecycle(identifier,[t.get('id') for t in result.get('data',[])],thread)
            turns=[];remaining=6000
            for turn in result.get('data',[]):
                messages=[]
                for item in turn.get('items',[]):
                    if item.get('type')=='agentMessage':messages.append({'role':'assistant','phase':item.get('phase'),
                        'is_final':item.get('phase')=='final_answer','text':str(item.get('text',''))})
                    elif item.get('type')=='userMessage':
                        content=item.get('content') or []
                        messages.append({'role':'user','phase':None,'is_final':False,
                            'text':' '.join(str(x.get('text','')) for x in content if isinstance(x,dict))})
                selected=[i for i,m in enumerate(messages) if m['is_final']][-2:]
                for role in ('user','assistant'):
                    choices=[i for i,m in enumerate(messages) if m['role']==role and not m['is_final']]
                    if choices:selected.append(choices[-1])
                bounded={}
                # Spend each turn's budget on its final first, then retain the
                # triggering question and latest progress; preserve display order.
                for index in selected:
                    if remaining<=0:break
                    message=dict(messages[index]);full=message['text'];cap=min(remaining,4000 if message['is_final'] else 1000)
                    message.update(text=full[:cap],truncated=len(full)>cap,original_chars=len(full))
                    remaining-=len(message['text']);bounded[index]=message
                observation=self.turn_observation(turn,lifecycle.get(turn.get('id')))
                user=next((item for item in turn.get('items',[]) if item.get('type')=='userMessage'),{})
                turns.append(dict(observation,id=turn.get('id'),client_user_message_id=user.get('clientId') or user.get('client_id'),messages=[bounded[i] for i in sorted(bounded)],
                    messages_omitted=len(messages)-len(bounded)))
            result={'thread_id':identifier,'name':thread.get('name'),'cwd':thread.get('cwd'),'status':thread.get('status'),'turns':turns,
                'request_state':'done','observed_at':self.observed_at(),'task_goal_status':'not_evaluated',
                'note':'读取请求已返回。commentary记录过程，final_answer记录最终答复；按对应turn的状态和证据确认进度，再结合jg的目标判断是否收尾。'}
            if requested in self.routes:
                result['remote_thread_id']=self.routes[requested]['thread_id']
                result['shared_thread_id']=self.routes[requested]['thread_id']
            try:
                page=self.rpc('thread/queue/list',{'threadId':identifier,'limit':5})
                result['pending_messages']=[{'id':q.get('id'),'client_user_message_id':q.get('clientUserMessageId'),
                    'text':' '.join(str(x.get('text','')) for x in q.get('input',[]) if isinstance(x,dict))[:500]} for q in page.get('data',[])]
                result['more_pending_messages']=bool(page.get('nextCursor'))
            except RuntimeError:result['queue_status']='unavailable'
            return result
        if action not in ('send','stop'):raise ValueError('Unsupported controller action')
        # Queue mode means an existing, unowned conversation belongs to the
        # desktop. Register it before any resume can acquire its writer. Only
        # worker-created/routed conversations keep independent execution.
        owned=identifier in self.active or any(row.get('thread_id')==identifier for row in self.routes.values())
        if getattr(self,'writer_strategy','fork')=='queue' and not owned:
            self.attach_shared(requested,thread)
        if action=='stop' and any(row.get('native') and row['thread_id']==identifier for row in self.routes.values()):
            return self.stop_delegated(identifier,job.get('job_id'))
        usage=None;usage_error=None
        if action=='send':
            try:
                usage=self.read_usage()
            except Exception:
                usage_error='Codex usage could not be read; ordinary-use availability is unknown.'
        if any(row.get('native') and row['thread_id']==identifier for row in self.routes.values()):
            result=self.queue_on_owner(job,identifier)
            if usage:result['usage']=usage
            if usage_error:result['usage_error']=usage_error
            return result
        status=thread.get('status') or {}
        if status.get('type')=='notLoaded':
            try:thread=self.rpc('thread/resume',{'threadId':identifier,'excludeTurns':True})['thread']
            except RuntimeError as error:
                if 'already has an active writer' not in str(error):raise
                if action=='send':
                    if any(row.get('shared') and row['thread_id']==identifier for row in self.routes.values()):
                        result=self.queue_on_owner(job,identifier)
                        if usage:result['usage']=usage
                        if usage_error:result['usage_error']=usage_error
                        return result
                    if self.writer_strategy=='fork':return self.fork_and_send(job,requested,thread,identifier)
                    return self.queue_on_owner(job,identifier)
                return self.stop_delegated(identifier,job.get('job_id'))
        active=self.active.get(identifier)
        if not active and (thread.get('status') or {}).get('type')=='active':
            recent=self.rpc('thread/turns/list',{'threadId':identifier,'limit':1,'sortDirection':'desc'})
            active=next((t['id'] for t in recent.get('data',[]) if t.get('status')=='inProgress'),None)
        if action=='stop':
            if active:self.rpc('turn/interrupt',{'threadId':identifier,'turnId':active})
            return {'thread_id':identifier,'state':'interruption_requested' if active else 'already_idle'}
        self.final.pop(identifier,None)
        try:
            if active:
                self.rpc('turn/steer',{'threadId':identifier,'expectedTurnId':active,'input':[{'type':'text','text':job['text']}]})
                turn_id=active
            else:turn_id=self.rpc('turn/start',{'threadId':identifier,'input':[{'type':'text','text':job['text']}]})['turn']['id']
        except RuntimeError as error:
            if action!='send' or 'already has an active writer' not in str(error):raise
            if not any(row.get('shared') and row['thread_id']==identifier for row in self.routes.values()):raise
            result=self.queue_on_owner(job,identifier)
            if usage:result['usage']=usage
            if usage_error:result['usage_error']=usage_error
            return result
        result={'thread_id':identifier,'requested_thread_id':requested,'turn_id':turn_id,'name':thread.get('name'),'state':'submitted','scope':'Codex原生会话；沿用本机会话配置'}
        if usage:result['usage']=usage
        if usage_error:result['usage_error']=usage_error
        return result

class SshBridge:
    """Fixed forced-command SSH session; no shell, forwarding or local listener."""
    def __init__(self,config,token):
        self.token=token;self.counter=0;self.responses=queue.Queue(maxsize=4)
        alias=config.get('ssh_alias')
        alias_options=[]
        if alias:
            if not re.fullmatch(r'[A-Za-z0-9_.-]{1,80}',alias):raise ValueError('Invalid SSH alias')
            # Read the user's existing alias without changing SSH/network settings.
            # Do not inherit its management identity: this bridge still uses only
            # its existing forced-command key, with strict host-key verification.
            resolved=subprocess.run([config['ssh'],'-G',alias],capture_output=True,text=True,
                encoding='utf-8',timeout=8,creationflags=HIDDEN,check=True)
            effective=dict(line.split(None,1) for line in resolved.stdout.splitlines() if ' ' in line)
            host=effective.get('hostname','');user=effective.get('user','');port=effective.get('port','22')
            if not re.fullmatch(r'[A-Za-z0-9_.:-]+',host) or not re.fullmatch(r'[A-Za-z0-9_.-]+',user) or not port.isdecimal():
                raise ValueError('Unsupported SSH alias target')
            config=dict(config,destination=user+'@'+host,host_key_alias=None,proxy_command=None)
            alias_options=['-F','none','-p',port]
            if effective.get('hostkeyalias') not in (None,'none'):config['host_key_alias']=effective['hostkeyalias']
            if effective.get('proxycommand') not in (None,'none'):config['proxy_command']=effective['proxycommand']
        command=[config['ssh'],'-T','-4','-o','BatchMode=yes','-o','IdentitiesOnly=yes','-o','StrictHostKeyChecking=yes',
        '-o','ConnectTimeout=12','-o','ServerAliveInterval=15','-o','ServerAliveCountMax=3','-i',config['key'],
        config['destination'],'fatfish-codex-bridge']
        if config.get('host_key_alias'):command[1:1]=['-o','HostKeyAlias='+config['host_key_alias']]
        if config.get('proxy_command'):command[1:1]=['-o','ProxyCommand='+config['proxy_command']]
        command[1:1]=alias_options
        self.process=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
            text=True,encoding='utf-8',bufsize=1,creationflags=HIDDEN)
        threading.Thread(target=self.reader,daemon=True).start()

    def reader(self):
        try:
            for line in self.process.stdout:
                try:response=json.loads(line)
                except ValueError:continue
                try:self.responses.put_nowait(response)
                except queue.Full:break
        finally:
            try:self.responses.put_nowait({'error':'SSH bridge closed'})
            except queue.Full:pass

    def post(self,body):
        self.counter+=1
        self.process.stdin.write(json.dumps({'id':self.counter,'token':self.token,'data':body},ensure_ascii=False)+'\n');self.process.stdin.flush()
        result=self.responses.get(timeout=30)
        if result.get('id')!=self.counter or 'error' in result:raise RuntimeError('SSH bridge request failed')
        return result['result']

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:self.process.wait(timeout=8)
            except subprocess.TimeoutExpired:self.process.kill()

def poll_background(app,control,post,watches,native_watches):
    """Keep liveness fresh during slow read RPCs without claiming another job."""
    last=[0.0]
    def pulse():
        now=time.monotonic()
        if now-last[0]>=1:
            response=post({'action':'poll','protocol_version':5,'active_id':'watcher-heartbeat','claim':''})
            if response.get('work_waiting') is True:raise WatcherYield()
            last[0]=now
    with control.lock:
        previous=app.wait_callback;app.wait_callback=pulse
        try:
            pulse()
            return app.poll_watchers(watches)+app.poll_native_threads(native_watches)
        except WatcherYield:return []
        finally:app.wait_callback=previous

def run(config):
    from shared_control import SharedControl,start_control
    token=Path(config['token_file']).read_text().strip()
    state_path=Path(config['state_file']);jobs={};pending_events=[];video_check_at=0.0
    def state(value):
        pending=state_path.with_suffix('.pending');pending.write_text(json.dumps(value,ensure_ascii=False),encoding='utf-8');os.replace(pending,state_path)
    # Keep the page available even while Codex initializes or SSH reconnects.
    control=SharedControl()
    try:ui=start_control(control,state_path.with_name('control-ui.json'),config.get('control_port',55357))
    except Exception as exc:
        state({'online':False,'pid':os.getpid(),'control_page_online':False,'error':type(exc).__name__,'at':time.time()});raise
    from phone_access import PhoneAccess
    phone=PhoneAccess(control,state_path.parent);control.phone_access=phone
    try:phone.start()
    except Exception:
        phone.last_error='手机入口启动失败，本机控制台不受影响。';phone.state='blocked';phone.publish_status()
    import atexit
    atexit.register(phone.close)
    while True:
        bridge=None;app=None
        state({'online':False,'pid':os.getpid(),'control_page_online':True,'engine_ready':False,'at':time.time()})
        try:
            app=AppServer(config['codex'],state_path.with_name('thread-routes.json'),config.get('writer_conflict_strategy','fork'))
            control.set_app(app)
            state({'online':False,'pid':os.getpid(),'control_page_online':True,'started':time.time()})
            # Local conversation ownership survives SSH reconnects. Both inputs use control.lock.
            while app.process.poll() is None:
                try:
                    bridge=SshBridge(config,token);post=bridge.post
                    response=post({'action':'poll','protocol_version':5})
                    job=response.get('job');watches=response.get('watches',[]);native_watches=response.get('native_watches',[])
                    control.bridge_online=True
                    state({'online':True,'pid':os.getpid(),'control_page_online':True,'started':time.time()})
                    while bridge.process.poll() is None and app.process.poll() is None:
                        if time.monotonic()-video_check_at>=30:
                            video_check_at=time.monotonic();media=ensure_video_reader(config)
                            if media['state'] in ('started','start_failed'):
                                state_path.with_name('video-autostart.json').write_text(json.dumps(dict(media,at=time.time())),encoding='utf-8')
                        while not app.events.empty():
                            event=app.events.get_nowait();entries=jobs.pop(event['turn_id'],[])
                            for entry in entries:pending_events.append(dict(event,job_id=entry,action='completed'))
                        for event in pending_events[:]:
                            post(event);pending_events.remove(event)
                        if job:
                            try:
                                callback=lambda:post({'action':'poll','protocol_version':5,'active_id':job['id'],'claim':job['claim']})
                                result=control.execute(job,callback);payload={'action':'result','id':job['id'],'claim':job['claim'],'result':result}
                                if job['action'] in ('send','probe') and result.get('turn_id'):jobs.setdefault(result['turn_id'],[]).append(job['id'])
                                if job['action']=='probe':state({'online':True,'pid':os.getpid(),'control_page_online':True,'probe':result})
                            except Exception as exc:payload={'action':'result','id':job['id'],'claim':job['claim'],'error':True,'result':{'error':type(exc).__name__,'message':str(exc)[:600]}}
                            # A lost result is never converted into a new task.
                            post(payload)
                        else:time.sleep(1)
                        response=post({'action':'poll','protocol_version':5})
                        job=response.get('job');watches=response.get('watches',[]);native_watches=response.get('native_watches',[])
                        if not job:
                            pending_events.extend(poll_background(app,control,post,watches,native_watches))
                            response=post({'action':'poll','protocol_version':5})
                            job=response.get('job');watches=response.get('watches',[]);native_watches=response.get('native_watches',[])
                        state({'online':True,'pid':os.getpid(),'control_page_online':True,'at':time.time()})
                except Exception as exc:
                    control.bridge_online=False
                    state({'online':False,'pid':os.getpid(),'control_page_online':app.process.poll() is None,'error':type(exc).__name__,'at':time.time()})
                finally:
                    if bridge:bridge.close();bridge=None
                time.sleep(5)
        except Exception as exc:
            state({'online':False,'pid':os.getpid(),'control_page_online':True,'engine_ready':False,'error':type(exc).__name__,'at':time.time()})
        finally:
            control.set_app(None);control.bridge_online=False
            if app:app.close()
            if bridge:bridge.close()
        time.sleep(15)

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--config',required=True);args=parser.parse_args()
    config=json.loads(Path(args.config).read_text(encoding='utf-8'))
    # One local connector per profile; no duplicated workers after another login.
    import msvcrt
    lock=Path(config['state_file']).with_suffix('.lock').open('a+b');lock.seek(0);lock.write(b'0');lock.flush();lock.seek(0)
    try:msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
    except OSError:raise SystemExit(0)
    run(config)
