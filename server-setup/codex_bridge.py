"""Owner-private Codex job queue. Worker credential has no runtime/MCP authority."""
import datetime as dt,hashlib,json,math,re,secrets,time
from execution_status import normalized_output

def redact(text):
    text=re.sub(r'\b\d{6,14}:[A-Za-z0-9_-]{20,}\b','[凭据已隐藏]',str(text))
    return re.sub(r'\bsk-[A-Za-z0-9_-]{12,}\b','[凭据已隐藏]',text)

def _finite_number(value):
    return value if type(value) in (int,float) and math.isfinite(value) else None

def _usage_window(value,*,projected=False):
    if value is None:return None
    if not isinstance(value,dict):value={}
    used=_finite_number(value.get('used_percent' if projected else 'usedPercent'))
    duration=value.get('window_duration_mins' if projected else 'windowDurationMins')
    resets=value.get('resets_at' if projected else 'resetsAt')
    return {'used_percent':used,'remaining_percent':max(0,min(100,100-used)) if used is not None else None,
        'window_duration_mins':duration if type(duration) is int and duration>0 else None,
        'resets_at':resets if type(resets) is int and resets>=0 else None}

def sanitize_usage(payload,observed_at=None):
    """Project account/rateLimits/read; absence means unknown, not zero or permission."""
    if not isinstance(payload,dict) or payload.get('error'):
        raise ValueError('Codex usage response was not usable')
    multi=payload.get('rateLimitsByLimitId')
    source=multi if isinstance(multi,dict) and multi else {'codex':payload.get('rateLimits')}
    limits=[]
    for key,snapshot in list(source.items())[:50]:
        if not isinstance(snapshot,dict):continue
        name=snapshot.get('limitName');plan=snapshot.get('planType');reached=snapshot.get('rateLimitReachedType')
        limits.append({'limit_id':str(snapshot.get('limitId') or key)[:80],
            'limit_name':name[:120] if isinstance(name,str) else None,
            'plan_type':plan[:40] if isinstance(plan,str) else None,
            'primary':_usage_window(snapshot.get('primary')),'secondary':_usage_window(snapshot.get('secondary')),
            'rate_limit_reached_type':reached[:80] if isinstance(reached,str) else None})
    allowed=payload.get('ordinaryUsageAllowed')
    return {'observed_at':observed_at or dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds'),
        'ordinary_usage_allowed':allowed if type(allowed) is bool else None,'limits':limits}

def sanitize_usage_summary(value):
    """Validate a desktop projection and drop every unrelated account/credential field."""
    if not isinstance(value,dict) or value.get('error') or not isinstance(value.get('limits'),list):
        raise ValueError('invalid usage summary')
    observed=value.get('observed_at')
    if not isinstance(observed,str) or len(observed)>64:observed=dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')
    allowed=value.get('ordinary_usage_allowed');limits=[]
    for item in value['limits'][:50]:
        if not isinstance(item,dict):continue
        reached=item.get('rate_limit_reached_type')
        limits.append({'limit_id':item['limit_id'][:80] if isinstance(item.get('limit_id'),str) else None,
            'limit_name':item['limit_name'][:120] if isinstance(item.get('limit_name'),str) else None,
            'plan_type':item['plan_type'][:40] if isinstance(item.get('plan_type'),str) else None,
            'rate_limit_reached_type':reached[:80] if isinstance(reached,str) else None,
            'primary':_usage_window(item.get('primary'),projected=True),
            'secondary':_usage_window(item.get('secondary'),projected=True)})
    return {'observed_at':observed,'ordinary_usage_allowed':allowed if type(allowed) is bool else None,'limits':limits}

class CodexBridge:
    def __init__(self,runtime):
        self.r=runtime
        # Backend adapters share this class. Enable only the Codex namespace.
        self.result_dedup=(runtime.settings.get('codex_query_dedup_enabled') is True
                           and getattr(runtime,'backend','codex')=='codex')
        with runtime.db() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS codex_jobs(
                id TEXT PRIMARY KEY,source TEXT,fingerprint TEXT,action TEXT,args TEXT,
                state TEXT,claim TEXT,created REAL,updated REAL,output TEXT,chain_id TEXT,
                auto_reply INTEGER NOT NULL DEFAULT 0,UNIQUE(source,fingerprint));
                CREATE TABLE IF NOT EXISTS codex_completions(turn TEXT PRIMARY KEY,job TEXT,created REAL);
                CREATE TABLE IF NOT EXISTS codex_chains(
                    id TEXT PRIMARY KEY,thread_id TEXT NOT NULL,initial_job TEXT NOT NULL,state TEXT NOT NULL,
                    automatic_exchanges INTEGER NOT NULL DEFAULT 0,last_turn TEXT,created REAL NOT NULL,updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS codex_events(
                    id TEXT PRIMARY KEY,chain_id TEXT NOT NULL,job_id TEXT NOT NULL,turn_id TEXT NOT NULL UNIQUE,
                    turn_status TEXT NOT NULL,content TEXT NOT NULL,status TEXT NOT NULL,claim TEXT,
                    claimed_until REAL,created REAL NOT NULL,delivered REAL,reply_job TEXT,
                    processing_started REAL,outcome TEXT);
                CREATE INDEX IF NOT EXISTS codex_events_ready ON codex_events(status,created);
                CREATE TABLE IF NOT EXISTS codex_usage_cache(
                    id INTEGER PRIMARY KEY CHECK(id=1),observed_at TEXT NOT NULL,payload TEXT NOT NULL,created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS codex_native_threads(
                    thread_id TEXT PRIMARY KEY,cursor_turn_id TEXT,created REAL NOT NULL,updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS codex_native_receipts(
                    turn_id TEXT PRIMARY KEY,thread_id TEXT NOT NULL,event_id TEXT,created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS codex_thread_bindings(scope TEXT PRIMARY KEY,thread_id TEXT NOT NULL,updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS codex_query_cache(cache_key TEXT PRIMARY KEY,payload TEXT NOT NULL,created REAL NOT NULL);''')
            if self.result_dedup:
                db.execute("CREATE TABLE IF NOT EXISTS codex_query_observed(scope TEXT,request_key TEXT,digest TEXT NOT NULL,job_id TEXT,observed REAL NOT NULL,PRIMARY KEY(scope,request_key))")
                db.execute("CREATE TABLE IF NOT EXISTS codex_query_cache_jobs(cache_key TEXT PRIMARY KEY,job_id TEXT NOT NULL)")
            cols={row['name'] for row in db.execute('PRAGMA table_info(codex_jobs)')}
            if 'chain_id' not in cols:db.execute('ALTER TABLE codex_jobs ADD COLUMN chain_id TEXT')
            if 'auto_reply' not in cols:db.execute('ALTER TABLE codex_jobs ADD COLUMN auto_reply INTEGER NOT NULL DEFAULT 0')
            if 'notify_pending' not in cols:db.execute('ALTER TABLE codex_jobs ADD COLUMN notify_pending INTEGER NOT NULL DEFAULT 0')
            if 'origin' not in cols:db.execute("ALTER TABLE codex_jobs ADD COLUMN origin TEXT NOT NULL DEFAULT '{}'")
            cols={row['name'] for row in db.execute('PRAGMA table_info(codex_events)')}
            for name,kind in (('processing_started','REAL'),('outcome','TEXT')):
                if name not in cols:db.execute('ALTER TABLE codex_events ADD COLUMN '+name+' '+kind)
            if 'kind' not in cols:db.execute("ALTER TABLE codex_events ADD COLUMN kind TEXT NOT NULL DEFAULT 'turn'")
            if 'silent' not in cols:db.execute('ALTER TABLE codex_events ADD COLUMN silent INTEGER NOT NULL DEFAULT 0')
            for name,kind in (('processing_attempts','INTEGER NOT NULL DEFAULT 0'),('recovery','INTEGER NOT NULL DEFAULT 0')):
                if name not in cols:db.execute('ALTER TABLE codex_events ADD COLUMN '+name+' '+kind)
            chain_cols={row['name'] for row in db.execute('PRAGMA table_info(codex_chains)')}
            for name,default in (('execution_state','unknown'),('receipt_state','pending'),('delivery_state','not_verified')):
                if name not in chain_cols:db.execute('ALTER TABLE codex_chains ADD COLUMN '+name+" TEXT NOT NULL DEFAULT '"+default+"'")
            for row in db.execute("SELECT output FROM codex_jobs WHERE action='send' AND state='done' ORDER BY created DESC LIMIT 40"):
                try:value=json.loads(row['output'] or '{}')
                except ValueError:continue
                if value.get('state')=='queued_on_owner' and value.get('thread_id'):
                    db.execute('INSERT OR IGNORE INTO codex_native_threads VALUES(?,NULL,?,?)',(value['thread_id'],time.time(),time.time()))

    def online(self,db):return time.time()-float(self.r._get(db,'codex_worker_seen','0'))<30

    def resolve_thread(self,db,args,context,*,remember=True):
        """Resolve real IDs once and carry them through subsequent operations."""
        thread=args.get('thread_id')
        if thread:
            if not isinstance(thread,str) or not re.fullmatch(r'[a-zA-Z0-9_-]{12,80}',thread):raise ValueError('thread_id格式无效')
            # A model may put a returned job/chain ID in the thread field.
            job=db.execute('SELECT args,output FROM codex_jobs WHERE id=?',(thread,)).fetchone()
            chain=db.execute('SELECT thread_id FROM codex_chains WHERE id=?',(thread,)).fetchone()
            if job:thread=json.loads(job['args'] or '{}').get('thread_id') or json.loads(job['output'] or '{}').get('thread_id') or (chain['thread_id'] if chain else None)
            elif chain:thread=chain['thread_id']
        elif args.get('job_id'):
            job=db.execute('SELECT args,output,chain_id FROM codex_jobs WHERE id=?',(args['job_id'],)).fetchone()
            chain=db.execute('SELECT thread_id FROM codex_chains WHERE id=? OR initial_job=?',(args['job_id'],args['job_id'])).fetchone()
            if job:thread=json.loads(job['args'] or '{}').get('thread_id') or json.loads(job['output'] or '{}').get('thread_id')
            if not thread and chain:thread=chain['thread_id']
        if not thread:
            event_id=self._event_from_context(context)
            event=db.execute('SELECT c.thread_id FROM codex_events e JOIN codex_chains c ON c.id=e.chain_id WHERE e.id=?',(event_id,)).fetchone() if event_id else None
            if event:thread=event['thread_id']
        scope=str(context.get('chat_id') or self.r.owner).split('/',1)[0]
        if context.get('task_id') and (str(context.get('session_key','')).startswith(('agent:task-','agent:cron-','agent:autonomy-')) or str(context.get('message_id','')).startswith(('heartbeat:','codex-event:'))):scope+=':task:'+context['task_id']
        if not thread:
            binding=db.execute('SELECT thread_id FROM codex_thread_bindings WHERE scope=?',(scope,)).fetchone()
            thread=binding['thread_id'] if binding else self.r.settings.get('codex_default_thread_id')
        if thread and remember:
            db.execute('INSERT OR REPLACE INTO codex_thread_bindings VALUES(?,?,?)',(scope,thread,time.time()))
        return thread

    @staticmethod
    def query_key(clean):return json.dumps(clean,ensure_ascii=False,sort_keys=True,separators=(',',':'))

    @staticmethod
    def invalidate_read_cache(db,thread):
        if thread:db.execute('DELETE FROM codex_query_cache WHERE instr(cache_key,?)>0',(json.dumps(thread,ensure_ascii=False),))

    @staticmethod
    def stable_query_digest(output):
        # Keep actual status, turns, commentary and final text. Only observation
        # timestamps/read-cache metadata vary without a new execution result.
        def stable(value):
            if isinstance(value,dict):
                return {key:stable(item) for key,item in value.items() if key not in ('observed_at','read_cache','age_seconds')}
            if isinstance(value,list):return [stable(item) for item in value]
            return value
        return hashlib.sha256(json.dumps(stable(output),ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()

    def query_identity(self,row,output=None):
        origin=json.loads(row['origin'] or '{}')
        scope=str(origin.get('chat_id') or self.r.owner).split('/',1)[0]
        args=json.loads(row['args'])
        value=json.loads(row['output'] or '{}') if output is None else output
        return scope,self.query_key(args),self.stable_query_digest(value)

    def is_background_query(self,row):
        origin=json.loads(row['origin'] or '{}')
        return origin.get('session_key')=='heartbeat' or bool(self._event_from_context(origin))

    def unchanged_background_read(self,db,row,output=None):
        if not self.result_dedup or row['action']!='read' or not self.is_background_query(row):return False
        scope,key,digest=self.query_identity(row,output)
        seen=db.execute('SELECT digest FROM codex_query_observed WHERE scope=? AND request_key=?',(scope,key)).fetchone()
        if seen and seen['digest']==digest:return True
        value=json.loads(row['output'] or '{}') if output is None else output
        turns=value.get('turns')
        return bool(turns and all(self.read_final_seen(db,scope,value,t) for t in turns))

    @staticmethod
    def final_version(thread,turn,status,text):
        key=json.dumps({'receipt':'turn_final','thread_id':thread,'turn_id':turn,'status':status},sort_keys=True,separators=(',',':'))
        digest=hashlib.sha256(redact(text).strip()[:6000].encode()).hexdigest()
        return key,digest

    @staticmethod
    def read_final(output,turn):
        if turn.get('terminal_confirmed') is False:return None
        status=turn.get('status')
        text='\n\n'.join(str(m.get('text','')).strip() for m in turn.get('messages',[]) if m.get('phase')=='final_answer' or m.get('is_final') is True).strip()
        if status not in ('completed','failed') or not text or not turn.get('id'):return None
        return output.get('thread_id'),turn['id'],status,text

    def read_final_seen(self,db,scope,output,turn):
        final=self.read_final(output,turn)
        messages=turn.get('messages',[])
        finals=[n for n,m in enumerate(messages) if m.get('phase')=='final_answer' or m.get('is_final') is True]
        # A commentary/user message after the final answer is a new update,
        # even when that final text itself has already been observed.
        if finals and any(str(m.get('text','')).strip() for m in messages[finals[-1]+1:]):return False
        return bool(final and self.final_seen(db,scope,*final))

    def final_seen(self,db,scope,thread,turn,status,text):
        if not self.result_dedup:return False
        key,digest=self.final_version(thread,turn,status,text)
        found=db.execute('SELECT digest FROM codex_query_observed WHERE scope=? AND request_key=?',(scope,key)).fetchone()
        return bool(found and found['digest']==digest)

    def remember_final(self,db,scope,thread,turn,status,text,job_id):
        key,digest=self.final_version(thread,turn,status,text)
        db.execute('INSERT OR REPLACE INTO codex_query_observed VALUES(?,?,?,?,?)',(scope,key,digest,job_id,time.time()))

    def retire_seen_notifications(self,db):
        queries=db.execute("SELECT e.id AS event_id,j.* FROM codex_events e JOIN codex_jobs j ON j.id=e.job_id WHERE e.kind='query' AND e.status IN ('pending','claimed') AND j.action='read'").fetchall()
        for item in queries:
            if self.unchanged_background_read(db,item):
                self.close_query_notification(db,item['event_id'],item['id'],'same_read_result_observed')
        native=db.execute("SELECT e.*,c.thread_id FROM codex_events e JOIN codex_chains c ON c.id=e.chain_id WHERE e.kind='turn' AND e.status IN ('pending','claimed')").fetchall()
        for item in native:
            text=item['content'].split('Codex回复：\n',1)[-1]
            if self.final_seen(db,self.r.owner,item['thread_id'],item['turn_id'],item['turn_status'],text):
                db.execute("UPDATE codex_events SET status='observed',outcome='turn_final_observed',delivered=?,claimed_until=NULL WHERE id=? AND status IN ('pending','claimed')",(time.time(),item['id']))

    def remember_native_final(self,db,event):
        if not self.result_dedup:return
        chain=db.execute('SELECT thread_id FROM codex_chains WHERE id=?',(event['chain_id'],)).fetchone()
        if not chain:return
        text=event['content'].split('Codex回复：\n',1)[-1]
        self.remember_final(db,self.r.owner,chain['thread_id'],event['turn_id'],event['turn_status'],text,event['job_id'])
        self.retire_seen_notifications(db)

    @staticmethod
    def close_query_notification(db,event_id,job_id,reason):
        # A running callback is never cancelled or replayed by observation.
        changed=db.execute("UPDATE codex_events SET status='observed',outcome=?,delivered=?,claimed_until=NULL WHERE id=? AND kind='query' AND status IN ('pending','claimed')",
                           (reason,time.time(),event_id)).rowcount
        if changed:
            db.execute("UPDATE codex_chains SET state='completed',updated=? WHERE id=? AND state IN ('event_pending','pending')",(time.time(),job_id))
        return bool(changed)

    def remember_read_result(self,db,row):
        if not self.result_dedup or row['action']!='read' or not row['output']:return
        scope,key,digest=self.query_identity(row)
        db.execute('INSERT OR REPLACE INTO codex_query_observed VALUES(?,?,?,?,?)',(scope,key,digest,row['id'],time.time()))
        output=json.loads(row['output'])
        for turn in output.get('turns',[]):
            final=self.read_final(output,turn)
            if final:self.remember_final(db,scope,*final,row['id'])
        self.retire_seen_notifications(db)

    def query_result_view(self,db,row,context):
        result=self.view(row)
        if not self.result_dedup or row['action'] not in ('list','read','usage','create','artifact') or row['state'] in ('queued','claimed') or not row['output']:return result
        origin=json.loads(row['origin'] or '{}')
        if str(origin.get('chat_id') or self.r.owner)!=str(context.get('chat_id') or self.r.owner):return result
        db.execute('UPDATE codex_jobs SET notify_pending=0 WHERE id=?',(row['id'],))
        event=db.execute("SELECT id FROM codex_events WHERE job_id=? AND kind='query'",(row['id'],)).fetchone()
        if event:self.close_query_notification(db,event['id'],row['id'],'result_returned_to_tool')
        self.remember_read_result(db,row)
        return result

    def cached_query(self,db,clean,context=None):
        if clean['action']=='list' and not clean.get('query'):
            thread=self.resolve_thread(db,{},context or {},remember=False)
            if thread:
                return {'default_thread_id':thread,'threads':[{'id':thread,'name':self.r.settings.get('codex_default_thread_name','当前绑定会话')}],
                        'cache_state':'registered_binding','note':'这是当前聊天绑定的会话；read可省略thread_id，refresh=true另取完整列表。'}
        row=db.execute('SELECT * FROM codex_query_cache WHERE cache_key=?',(self.query_key(clean),)).fetchone()
        ttl=120 if clean['action']=='list' else 15
        if row and time.time()-row['created']<=ttl:
            if self.result_dedup:
                ref=db.execute('SELECT job_id FROM codex_query_cache_jobs WHERE cache_key=?',(self.query_key(clean),)).fetchone()
                if ref:
                    job=db.execute('SELECT * FROM codex_jobs WHERE id=?',(ref['job_id'],)).fetchone()
                    if job:self.query_result_view(db,job,context or {})
            return dict(json.loads(row['payload']),cache_state='cached',observed_at=dt.datetime.fromtimestamp(row['created'],dt.timezone.utc).isoformat())
        if clean['action']=='list' and not clean.get('query') and self.r.settings.get('codex_default_thread_id'):
            thread=self.r.settings['codex_default_thread_id']
            return {'default_thread_id':thread,'threads':[{'id':thread,'name':self.r.settings.get('codex_default_thread_name','原生共用会话'),'native':True}],
                    'cache_state':'registered_binding','note':'这是已登记的共用会话；read可直接省略thread_id，refresh=true可另取完整会话列表。'}

    def query_event(self,db,row,output):
        if row['action'] not in ('list','read','usage','create','artifact') or not row['notify_pending']:return None
        event_turn='query-'+row['id']
        old=db.execute('SELECT id FROM codex_events WHERE turn_id=?',(event_turn,)).fetchone()
        if old:return old['id']
        if self.unchanged_background_read(db,row,output):
            db.execute('UPDATE codex_jobs SET notify_pending=0 WHERE id=?',(row['id'],))
            return None
        args=json.loads(row['args']);origin=json.loads(row['origin'] or '{}')
        thread=output.get('thread_id') or args.get('thread_id') or self.r.settings.get('codex_default_thread_id') or ''
        # Keep the payload valid JSON and retain the most recent final answer.
        brief=dict(output)
        if self.result_dedup and row['action']=='read' and self.is_background_query(row) and isinstance(brief.get('turns'),list):
            scope=str(origin.get('chat_id') or self.r.owner).split('/',1)[0]
            seen=[t['id'] for t in brief['turns'] if self.read_final_seen(db,scope,brief,t)]
            if seen:
                brief['turns']=[t for t in brief['turns'] if t.get('id') not in seen]
                brief['observed_final_turn_ids']=seen
        if len(json.dumps(brief,ensure_ascii=False))>16000 and isinstance(brief.get('turns'),list):
            turns=[]
            for turn in brief['turns'][-3:]:
                copy={k:v for k,v in turn.items() if k!='messages'}
                messages=turn.get('messages',[])
                copy['messages']=[dict(m,text=str(m.get('text',''))[:5000],truncated=len(str(m.get('text','')))>5000) for m in messages if m.get('phase')=='final_answer' or m.get('type')=='userMessage'][-3:]
                turns.append(copy)
            brief['turns']=turns;brief['callback_truncated']=True
        encoded=json.dumps(brief,ensure_ascii=False)
        if len(encoded)>18000:
            brief={'thread_id':thread,'state':output.get('state'),'callback_truncated':True,'note':'完整结果已保存，可用status和job_id本地读取。'}
            encoded=json.dumps(brief,ensure_ascii=False)
        source=db.execute("SELECT content FROM events WHERE chat=? AND msg=? AND kind IN ('user','member') ORDER BY id DESC LIMIT 1",(str(origin.get('chat_id') or self.r.owner),str(origin.get('message_id','')))).fetchone()
        request_summary=redact(source['content'][:1200] if source else origin.get('request_summary',''))
        content=('先前的Codex查询已返回，接着原问题处理；这是查询结果，任务进展以具体内容为准。\n'
                 '查询：'+json.dumps(args,ensure_ascii=False)+'\n原问题：'+request_summary+'\njob_id：'+row['id']+'\nthread_id：'+thread+'\n结果：\n'+encoded)
        now=time.time();event_id=secrets.token_hex(12)
        db.execute("INSERT OR IGNORE INTO codex_chains(id,thread_id,initial_job,state,automatic_exchanges,last_turn,created,updated) VALUES(?,?,?,'event_pending',0,?,?,?)",(row['id'],thread,row['id'],event_turn,now,now))
        db.execute("INSERT INTO codex_events(id,chain_id,job_id,turn_id,turn_status,content,status,created,kind,silent) VALUES(?,?,?,?,?,?,'pending',?,'query',?)",
                   (event_id,row['id'],row['id'],event_turn,'completed',content,now,int(origin.get('session_key')=='heartbeat' or origin.get('codex_event_silent') is True)))
        return event_id

    def cached_usage(self):
        with self.r.db() as db:row=db.execute('SELECT observed_at,payload,created FROM codex_usage_cache WHERE id=1').fetchone()
        if not row or time.time()-row['created']>120:return None
        result=sanitize_usage_summary(json.loads(row['payload']))
        result['cache_state']='cached'
        return result

    def store_usage(self,db,value):
        clean=sanitize_usage_summary(value)
        db.execute('INSERT OR REPLACE INTO codex_usage_cache(id,observed_at,payload,created) VALUES(1,?,?,?)',
            (clean['observed_at'],json.dumps(clean,ensure_ascii=False),time.time()))
        if hasattr(self.r,'loops'):self.r.loops.observe_usage(db,getattr(self.r,'backend','codex'),clean)
        return clean

    def enqueue_event(self,db,job,turn,turn_status,final):
        chain_id=job['chain_id']
        if not chain_id:return None
        chain=db.execute('SELECT * FROM codex_chains WHERE id=?',(chain_id,)).fetchone()
        if not chain or chain['state'] in ('completed','failed','stopped','auto_limit','unknown','usage_blocked'):return None
        status=str(turn_status or 'unknown')[:40]
        text=redact(final).strip()[:6000]
        if not text and status=='completed':return None
        if self.final_seen(db,self.r.owner,chain['thread_id'],turn,status,text):return None
        event_id=secrets.token_hex(12)
        content=('Codex回合状态：'+status+'\n任务ID：'+str(chain['initial_job'])+'\nthread_id：'+chain['thread_id']+'\n回合ID：'+turn+'\n'
            '你负责将原目标推进到完成。复用已有结果和有效验证判断：达标就finish收口，有具体缺口才continue；新改动、新失败或关键证据不足时补充相称的验证。确需jg提供信息或决定目标变更时用wait_for_jg。'
            '向jg简短回报实质结果或阻断，细节按需展开。\n'
            'Codex回复：\n'+text)
        inserted=db.execute('INSERT OR IGNORE INTO codex_events(id,chain_id,job_id,turn_id,turn_status,content,status,created) VALUES(?,?,?,?,?,?,"pending",?)',
            (event_id,chain_id,job['id'],turn,status,content,time.time())).rowcount
        if not inserted:
            old=db.execute('SELECT id,status FROM codex_events WHERE turn_id=?',(turn,)).fetchone()
            if old and old['status']=='superseded':
                db.execute("UPDATE codex_events SET turn_status=?,content=?,status='pending',created=?,claim=NULL,claimed_until=NULL,delivered=NULL,processing_started=NULL,outcome=NULL,reply_job=NULL WHERE id=?",
                    (status,content,time.time(),old['id']))
                event_id=old['id'];inserted=True
        if inserted:
            db.execute('UPDATE codex_chains SET state="event_pending",execution_state=?,receipt_state="pending",last_turn=?,updated=? WHERE id=?',
                (status,turn,time.time(),chain_id))
            return event_id
        return db.execute('SELECT id FROM codex_events WHERE turn_id=?',(turn,)).fetchone()['id']

    @staticmethod
    def _event_from_context(context):
        message=str(context.get('message_id',''))
        prefix='codex-event:'
        event_id=message[len(prefix):] if message.startswith(prefix) else ''
        return event_id if re.fullmatch(r'[a-f0-9]{24}',event_id) else None

    def reuse_view(self,row):
        result=self.view(row);output=normalized_output(json.loads(row['output'] or '{}'))
        if output.get('terminal_confirmed') is True:
            return dict(result,reused_completed_result=True,note='同一指令和要求版本已有真实完成回执，复用该结果整理交付；新的补充要求可单独提交。')
        return dict(result,reused_pending_request=True)

    def _create_followup(self,db,chain,event,text,source,auto_reply,origin=None):
        if not isinstance(text,str) or not text.strip() or len(text)>12000:raise ValueError('需要有效的Codex回复文字，最多12000字')
        prefix='[Fatfish 自动回复（不是 jg）]\n' if auto_reply else '[jg 补充]\n'
        clean_text=prefix+text.strip()
        clean={'action':'send','thread_id':chain['thread_id'],'text':clean_text}
        if hasattr(self.r,'loops'):
            pending=self.r.loops.pending_request(db,origin or {},getattr(self.r,'backend','codex'),chain['thread_id'],clean,include_completed=True)
            if pending:return self.reuse_view(pending)
        fingerprint=hashlib.sha256(json.dumps(clean,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        previous=db.execute('SELECT * FROM codex_jobs WHERE source=? AND fingerprint=?',(source,fingerprint)).fetchone()
        if previous:
            return dict(self.view(previous),duplicate=True)
        else:
            now=time.time();job_id=secrets.token_hex(12)
            db.execute('INSERT INTO codex_jobs(id,source,fingerprint,action,args,state,claim,created,updated,output,chain_id,auto_reply) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                (job_id,source,fingerprint,'send',json.dumps(clean,ensure_ascii=False),'queued','',now,now,'',chain['id'],1 if auto_reply else 0))
            db.execute('UPDATE codex_jobs SET origin=? WHERE id=?',(json.dumps(origin or {},ensure_ascii=False),job_id))
            if hasattr(self.r,'loops'):self.r.loops.bind_job(db,origin or {},getattr(self.r,'backend','codex'),job_id)
        if auto_reply:
            count=chain['automatic_exchanges']+1
            db.execute('UPDATE codex_chains SET state="queued",automatic_exchanges=?,updated=? WHERE id=?',(count,time.time(),chain['id']))
        else:
            db.execute('UPDATE codex_chains SET state="queued",updated=? WHERE id=?',(time.time(),chain['id']))
        if event:db.execute('UPDATE codex_events SET status="reply_queued",reply_job=? WHERE id=?',(job_id,event['id']))
        return {'job_id':job_id,'thread_id':chain['thread_id'],'chain_id':chain['id'],'state':'queued','completion_notification':True,'automatic_exchanges':chain['automatic_exchanges']+(1 if auto_reply else 0),
            'note':'回复已排入同一 Codex thread；'+('此消息由Fatfish代答，不代表jg本人发言。' if auto_reply else '此消息是jg的补充。')}

    def _close_chain(self,db,chain_id,state):
        now=time.time()
        db.execute('UPDATE codex_chains SET state=?,receipt_state="closed",updated=? WHERE id=?',(state,now,chain_id))
        db.execute("UPDATE codex_events SET status='closed' WHERE chain_id=? AND status IN ('pending','claimed','processing','delivered','waiting_user')",(chain_id,))
        # A task closed by the owner/model must not leave an automatic reply queued.
        db.execute("UPDATE codex_jobs SET state='cancelled',updated=? WHERE chain_id=? AND auto_reply=1 AND state='queued'",(now,chain_id))

    def chain_action(self,action,args,context):
        event_id=self._event_from_context(context)
        with self.r.db() as db:
            db.execute('BEGIN IMMEDIATE')
            event=db.execute('SELECT * FROM codex_events WHERE id=?',(event_id,)).fetchone() if event_id else None
            chain=db.execute('SELECT * FROM codex_chains WHERE id=?',(event['chain_id'],)).fetchone() if event else None
            if event and event['kind']=='query' and action=='continue':chain=None;event=None
            if args.get('job_id'):
                job=args['job_id']
                chain=db.execute('SELECT * FROM codex_chains WHERE id=? OR initial_job=? OR id=(SELECT chain_id FROM codex_jobs WHERE id=?)',(job,job,job)).fetchone()
                event=None
            task=context.get('task_id')
            def belongs(candidate):
                if not candidate or not task:return bool(candidate)
                binding=db.execute("SELECT task_id FROM loop_bindings WHERE kind=? AND key=?",(getattr(self.r,'backend','codex'),candidate['initial_job'])).fetchone()
                return bool(binding and binding['task_id']==task)
            if chain and not belongs(chain):chain=None;event=None
            if action=='continue' and chain:
                initial=db.execute('SELECT action FROM codex_jobs WHERE id=?',(chain['initial_job'],)).fetchone()
                if initial and initial['action'] in ('list','read','usage'):chain=None
            thread=self.resolve_thread(db,args,context)
            if thread and (not chain or (args.get('thread_id') and chain['thread_id']!=thread)):
                candidates=db.execute("SELECT c.* FROM codex_chains c JOIN codex_jobs j ON j.id=c.initial_job WHERE c.thread_id=? AND j.action IN ('send','observe') ORDER BY c.updated DESC",(thread,)).fetchall()
                chain=next((candidate for candidate in candidates if belongs(candidate)),None)
                event=None
            if not chain:
                if action=='continue' and thread:
                    # Direct continuation needs a thread and text, not a prior event.
                    args['thread_id']=thread
                    return None
                raise ValueError('需要真实job_id或thread_id来定位任务')
            if event and event['chain_id']!=chain['id']:event=None
            if event is None:event=db.execute('SELECT * FROM codex_events WHERE chain_id=? ORDER BY created DESC LIMIT 1',(chain['id'],)).fetchone()
            if action=='continue':
                source=str(context.get('message_id') or context.get('session_key') or 'agent')
                if context.get('session_key')=='heartbeat':source='background:'+str(self.r._get(db,'current_wake') or int(time.time()//300))+':'+source
                human=bool(self.r.loops.source(db,context).get('id')) if hasattr(self.r,'loops') else False
                return self._create_followup(db,chain,event,args.get('text'),source,not human,context)
            if action=='wait_for_jg':
                db.execute('UPDATE codex_chains SET state="waiting_user",updated=? WHERE id=?',(time.time(),chain['id']))
                if event:db.execute('UPDATE codex_events SET status="waiting_user" WHERE id=?',(event['id'],))
                return {'state':'waiting_user','job_id':chain['initial_job']}
            state=args.get('status','completed')
            if state not in ('completed','failed','stopped'):raise ValueError('finish status must be completed, failed, or stopped')
            if state=='completed' and context.get('task_id') and hasattr(self.r,'loops'):
                self.r.loops.require_deliveries(db,context['task_id'])
            self._close_chain(db,chain['id'],state)
            return {'state':state,'job_id':chain['initial_job']}

    def receipt_retry(self,db,row,outcome,now):
        retry=row['processing_attempts']<3
        status='pending' if retry else 'receipt_failed'
        delay=60 if row['processing_attempts']<=1 else 300
        db.execute('UPDATE codex_events SET status=?,outcome=?,recovery=1,claim=NULL,claimed_until=?,delivered=? WHERE id=?',
            (status,outcome,now+delay if retry else None,now,row['id']))
        db.execute("UPDATE codex_chains SET receipt_state=?,state=?,updated=? WHERE id=? AND state NOT IN ('completed','failed','stopped','auto_limit')",
            ('retry_wait' if retry else 'failed','event_pending' if retry else 'needs_review',now,row['chain_id']))

    def runtime_action(self,name,data):
        now=time.time()
        if name=='codex_event_poll':
            with self.r.db() as db:
                db.execute('BEGIN IMMEDIATE')
                # Recovery processes the stored receipt, with executor mutations
                # disabled. Losing a handler never rolls execution back to unknown.
                for row in db.execute("SELECT * FROM codex_events WHERE status='processing' AND claimed_until<?",(now,)).fetchall():
                    self.receipt_retry(db,row,'processing_expired',now)
                if self.r._get(db,'paused','0')=='1':return {'event':None}
                if self.result_dedup:
                    pending=db.execute("SELECT e.id AS event_id,j.* FROM codex_events e JOIN codex_jobs j ON j.id=e.job_id WHERE e.kind='query' AND e.status IN ('pending','claimed')").fetchall()
                    for item in pending:
                        if self.unchanged_background_read(db,item):
                            self.close_query_notification(db,item['event_id'],item['id'],'same_read_result_observed')
                row=db.execute("SELECT e.*,c.thread_id AS resolved_thread_id FROM codex_events e JOIN codex_chains c ON c.id=e.chain_id WHERE ((e.status='pending' AND COALESCE(e.claimed_until,0)<=?) OR (e.status='claimed' AND e.claimed_until<?)) AND c.state NOT IN ('completed','failed','stopped','auto_limit','unknown','usage_blocked') ORDER BY e.created LIMIT 1",(now,now)).fetchone()
                if not row:return {'event':None}
                route=self.r.loops.event_context(db,getattr(self.r,'backend','codex'),row['job_id']) if hasattr(self.r,'loops') else {}
                if route.get('revoked') or route.get('archived'):
                    db.execute("UPDATE codex_events SET status='delivered',outcome=?,delivered=? WHERE id=?",('autonomy_revoked' if route.get('revoked') else 'task_closed',now,row['id']))
                    return {'event':None}
                claim=secrets.token_hex(12)
                db.execute("UPDATE codex_events SET status='claimed',claim=?,claimed_until=? WHERE id=?",(claim,now+60,row['id']))
                return {'event':{'id':row['id'],'claim':claim,'chat_id':self.r.owner,'chain_id':row['chain_id'],
                    'job_id':row['job_id'],'turn_id':row['turn_id'],'thread_id':row['resolved_thread_id'],'turn_status':row['turn_status'],'content':row['content'],'kind':row['kind'],'silent':bool(row['silent']),**route}}
        event_id=str(data.get('event_id',''));claim=str(data.get('claim',''))
        if not re.fullmatch(r'[a-f0-9]{24}',event_id) or not re.fullmatch(r'[a-f0-9]{24}',claim):raise ValueError('invalid Codex event receipt')
        with self.r.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM codex_events WHERE id=? AND claim=?',(event_id,claim)).fetchone()
            if not row:return {'accepted':False}
            if name=='codex_event_start':
                if row['status']=='processing':return {'accepted':True,'duplicate':True}
                if row['status']!='claimed' or row['claimed_until']<now:return {'accepted':False}
                if self.r._get(db,'paused','0')=='1':return {'accepted':False}
                route=self.r.loops.event_context(db,getattr(self.r,'backend','codex'),row['job_id']) if hasattr(self.r,'loops') else {}
                if route.get('revoked') or route.get('archived'):
                    db.execute("UPDATE codex_events SET status='delivered',outcome='revoked_before_start',delivered=?,claimed_until=NULL WHERE id=?",(now,event_id))
                    return {'accepted':False}
                db.execute("UPDATE codex_events SET status='processing',processing_started=?,claimed_until=?,processing_attempts=processing_attempts+1 WHERE id=?",(now,now+1800,event_id))
                db.execute("UPDATE codex_chains SET receipt_state='processing' WHERE id=?",(row['chain_id'],))
                return {'accepted':True,'processing_timeout_seconds':1800}
            if name=='codex_event_touch':
                changed=db.execute("UPDATE codex_events SET claimed_until=? WHERE id=? AND claim=? AND status='processing'",(now+1800,event_id,claim)).rowcount
                return {'accepted':bool(changed)}
            if name=='codex_event_ack':
                outcome=data.get('outcome','completed')
                if outcome not in ('completed','error','interrupted','panicked','yielded'):raise ValueError('invalid Codex event outcome')
                if row['outcome'] is not None and row['status'] not in ('processing','claimed'):return {'accepted':True,'duplicate':True}
                if row['status']=='claimed':return {'accepted':False}
                if row['status']=='processing' and outcome=='yielded':
                    db.execute("UPDATE codex_events SET status='pending',outcome=NULL,claim=NULL,claimed_until=? WHERE id=?",(now+15,event_id))
                    db.execute("UPDATE codex_chains SET receipt_state='waiting_for_chat' WHERE id=?",(row['chain_id'],))
                    return {'accepted':True,'yielded':True}
                if row['status']=='processing' and outcome!='completed':
                    self.receipt_retry(db,row,outcome,now)
                    return {'accepted':True,'execution_preserved':True,'receipt_recovery':True}
                db.execute("UPDATE codex_chains SET receipt_state='processed' WHERE id=?",(row['chain_id'],))
                if row['kind']=='turn' and outcome=='completed':self.remember_native_final(db,row)
                if row['kind']=='query' and row['status']=='processing':
                    if outcome=='completed':
                        job=db.execute('SELECT * FROM codex_jobs WHERE id=?',(row['job_id'],)).fetchone()
                        if job:self.remember_read_result(db,job)
                    db.execute("UPDATE codex_events SET status='delivered',delivered=?,outcome=?,claimed_until=NULL WHERE id=?",(now,outcome,event_id))
                    db.execute("UPDATE codex_chains SET state='completed',updated=? WHERE id=?",(now,row['chain_id']))
                    return {'accepted':True,'kind':'query'}
                if row['status']=='processing':
                    # A successful model round without continue/finish/wait is not
                    # a completed delegation. Do not replay possible external writes.
                    state='needs_review'
                    db.execute('UPDATE codex_events SET status=?,delivered=?,outcome=?,claimed_until=NULL WHERE id=?',(state,now,outcome,event_id))
                    if outcome=='completed':
                        db.execute("UPDATE codex_chains SET state='needs_review',updated=? WHERE id=? AND state NOT IN ('completed','failed','stopped','auto_limit')",(now,row['chain_id']))
                elif row['status'] in ('reply_queued','closed','waiting_user','auto_limit','delivered'):
                    # The model may already have decided continue/finish/wait_for_jg in this turn.
                    db.execute('UPDATE codex_events SET delivered=?,outcome=?,claimed_until=NULL WHERE id=?',(now,outcome,event_id))
                else:return {'accepted':False}
                return {'accepted':True}
            if name=='codex_event_release':
                changed=db.execute("UPDATE codex_events SET status='pending',claim=NULL,claimed_until=NULL WHERE id=? AND status='claimed' AND claim=?",(event_id,claim)).rowcount
                return {'accepted':bool(changed)}
        raise ValueError('unknown Codex event action')

    def pending_watchers(self,db):
        watches=[]
        rows=db.execute("SELECT j.id,j.output,j.created FROM codex_jobs j JOIN codex_chains c ON c.id=j.chain_id WHERE j.action='send' AND j.state='done' AND c.state NOT IN ('completed','failed','stopped','auto_limit','usage_blocked') ORDER BY j.created DESC LIMIT 40").fetchall()
        for row in rows:
            try:result=json.loads(row['output'] or '{}')
            except ValueError:continue
            if result.get('turn_status') and (result.get('terminal_confirmed') is True or result.get('state')!='queued_on_owner'):continue
            client_id=result.get('client_user_message_id');turn_id=result.get('turn_id')
            if not client_id and not turn_id:continue
            watches.append({'job_id':row['id'],'thread_id':result.get('thread_id'),
                'client_user_message_id':client_id,'turn_id':turn_id,'created':row['created']})
        return watches

    def native_turn(self,data):
        thread=str(data.get('thread_id',''));turn=str(data.get('turn_id',''));now=time.time()
        if not re.fullmatch(r'[a-zA-Z0-9_-]{12,80}',thread) or not re.fullmatch(r'[a-zA-Z0-9_-]{6,100}',turn):
            raise ValueError('无效原生会话回执')
        if data.get('terminal_confirmed') is not True or data.get('status') not in ('completed','failed','unknown'):
            raise ValueError('需要已核实的原生回合结果')
        known=None
        with self.r.db() as db:
            if not db.execute('SELECT 1 FROM codex_native_threads WHERE thread_id=?',(thread,)).fetchone():
                return {'accepted':False,'reason':'unregistered_native_thread'}
            if db.execute('SELECT 1 FROM codex_native_receipts WHERE turn_id=?',(turn,)).fetchone():
                return {'accepted':True,'duplicate':True}
            for row in db.execute("SELECT * FROM codex_jobs WHERE action='send' AND state='done' ORDER BY created DESC LIMIT 100"):
                output=json.loads(row['output'] or '{}')
                if output.get('thread_id')==thread and output.get('turn_id')==turn:
                    known=(row['id'],output.get('client_user_message_id'));break
        if known:
            result=self.worker(dict(data,action='completed',job_id=known[0],client_user_message_id=known[1]))
            if not result.get('accepted'):return result
        else:
            # A registered shared thread is readable, not a blanket subscription.
            # Agent-submitted jobs above and explicit query callbacks keep their
            # own receipts; ordinary desktop turns only advance the read cursor.
            result={'accepted':True,'event_id':None,'model_wake_required':False,
                    'origin':'native_desktop_observation','record_only':True}
        with self.r.db() as db:
            db.execute('INSERT OR IGNORE INTO codex_native_receipts VALUES(?,?,?,?)',(turn,thread,result.get('event_id'),now))
            db.execute('UPDATE codex_native_threads SET cursor_turn_id=?,updated=? WHERE thread_id=?',(turn,now,thread))
            self.invalidate_read_cache(db,thread)
        return result

    def view(self,row,include_output=True):
        if not row:raise ValueError('未找到这项Codex请求')
        args=json.loads(row['args'] or '{}')
        result={'job_id':row['id'],'action':row['action'],'state':row['state'],'created':row['created'],'thread_id':args.get('thread_id'),'chain_id':row['chain_id'],
                'request_state':row['state'],'state_scope':'bridge_request'}
        if row['state'] in ('queued','claimed'):
            result.update(completion_notification=bool(row['notify_pending'] or row['action']=='send'),
                note='请求处理中；结果返回后程序会自动接续，可先继续聊天或安静结束。status可按需查看。')
        if row['output']:
            output=normalized_output(json.loads(row['output']))
            if output.get('state')=='queued_on_owner' and output.get('turn_status') and output.get('terminal_confirmed') is not True:
                output['reported_turn_status']=output.pop('turn_status')
                output.update(execution_state='pending_verification',terminal_confirmed=False,task_goal_status='not_evaluated',
                    note='旧快照待核实；用read查看对应回合、最终正文与状态证据。done指本次请求返回。')
            if include_output:result['result']=output
            else:result.update({key:output[key] for key in ('thread_id','turn_id','turn_status') if key in output})
            result['execution_state']=output.get('execution_state',output.get('state','unknown'))
            if 'terminal_confirmed' in output:result['terminal_confirmed']=output['terminal_confirmed']
            if output.get('thread_id'):result['thread_id']=output['thread_id']
            if output.get('turn_id'):result['turn_id']=output['turn_id']
        return result

    def call(self,args,context,chat):
        action=args.get('action')
        background=context.get('session_key')=='heartbeat'
        if action=='status':
            with self.r.db() as db:
                if args.get('job_id'):return self.query_result_view(db,db.execute('SELECT * FROM codex_jobs WHERE id=?',(args['job_id'],)).fetchone(),context)
                return {'online':self.online(db),'last_seen':self.r._get(db,'codex_worker_seen','0'),
                    'default_thread_id':self.resolve_thread(db,{},context,remember=False),
                    'supports_desktop_queue':self.r._get(db,'codex_worker_protocol','1') in ('2','3','4','5'),
                    'supports_independent_remote_session':self.r._get(db,'codex_worker_protocol','1') in ('3','4','5'),
                    'supports_shared_control':self.r._get(db,'codex_worker_protocol','1') in ('4','5'),
                    'supports_native_collaboration':self.r._get(db,'codex_worker_protocol','1')=='5',
                    'chains':[dict(row) for row in db.execute('SELECT id,thread_id,state,execution_state,receipt_state,delivery_state,automatic_exchanges,updated FROM codex_chains ORDER BY updated DESC LIMIT 5')],
                    'jobs':[self.view(r,include_output=False) for r in db.execute('SELECT * FROM codex_jobs ORDER BY created DESC LIMIT 5')],
                    'background_read_only':False,
                    'note':'这是连接、请求与协作台账。任务进展用read核验，额度用usage查询；按本次目标与实际结果继续协作。'}
        if action in ('continue','finish','wait_for_jg'):
            result=self.chain_action(action,args,context)
            if result is not None:return result
            action='send'
        if action not in ('list','read','send','stop','usage','create','artifact'):raise ValueError('action使用status/list/read/create/send/stop/usage/continue/finish/wait_for_jg/artifact')
        if action=='usage':
            cached=None if args.get('refresh') else self.cached_usage()
            if cached:return cached
        clean={'action':action}
        if action=='create':
            for key,limit in (('title',160),('cwd',2048),('model',120)):
                if key in args:
                    value=args[key]
                    if not isinstance(value,str) or not value.strip() or len(value)>limit:raise ValueError('无效的新建会话参数：'+key)
                    clean[key]=value.strip()
            with self.r.db() as db:reference=self.resolve_thread(db,args,context,remember=False)
            if reference:clean['source_thread_id']=reference
        if action in ('read','send','stop','artifact'):
            with self.r.db() as db:identifier=self.resolve_thread(db,args,context)
            if not identifier:raise ValueError('当前尚未绑定Codex会话；可用list选择一次会话或提供thread_id')
            clean['thread_id']=identifier
        if action=='read' and getattr(self.r,'backend','codex')=='codex':
            detail=args.get('detail','status')
            if detail not in ('status','history'):raise ValueError('detail使用status或history')
            clean['detail']=detail
        if action=='artifact':
            path=args.get('path')
            if not isinstance(path,str) or not path.strip() or len(path)>2048:raise ValueError('artifact需要执行端文件path')
            clean['path']=path
        if action=='stop' and args.get('job_id'):
            if not isinstance(args['job_id'],str) or not re.fullmatch(r'[a-zA-Z0-9_-]{6,100}',args['job_id']):raise ValueError('job_id格式无效')
            clean['job_id']=args['job_id']
        if action=='send':
            text=args.get('text')
            if not isinstance(text,str) or not text.strip() or len(text)>12000:raise ValueError('需要有效的Codex任务文字，最多12000字')
            clean['text']=text
            with self.r.db() as db:human_source=bool(self.r.loops.source(db,context).get('id')) if hasattr(self.r,'loops') else True
            if not human_source and not clean['text'].startswith('[Fatfish 自动回复（不是 jg）]'):
                clean['text']='[Fatfish 自动回复（不是 jg）]\n'+clean['text']
        if action=='list':clean['query']=str(args.get('query',''))[:120]
        source=str(context.get('message_id',''))
        if background:
            # Heartbeats have no user message. Keep retries within one check idempotent,
            # but never pin a later check to an old result through a fixed message_id.
            now=time.time()
            with self.r.db() as db:
                wake=db.execute('SELECT id,COALESCE(heartbeat_at,created) AS active_at,finished FROM wakes WHERE id=?',
                    (self.r._get(db,'current_wake'),)).fetchone()
            current=bool(wake and not wake['finished'] and 0<=now-wake['active_at']<900)
            scope='wake:'+wake['id'] if current else 'bucket:'+str(int(now//300))
            source='background:'+chat+':'+scope
        if not source:source='agent:'+str(context.get('session_key') or int(time.time()//300))
        fingerprint_args={k:v for k,v in clean.items() if action!='create' or k!='source_thread_id'}
        fingerprint=hashlib.sha256(json.dumps(fingerprint_args,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        now=time.time()
        with self.r.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if action=='send' and hasattr(self.r,'loops'):
                pending=self.r.loops.pending_request(db,context,getattr(self.r,'backend','codex'),clean.get('thread_id'),clean,include_completed=True)
                if pending:return self.reuse_view(pending)
            if action in ('list','read') and not args.get('refresh'):
                cached=self.cached_query(db,clean,context)
                if cached:return cached
            if action in ('list','read','usage'):
                # Share an in-flight query across messages in the same conversation.
                # A new origin must not add another identical desktop queue job.
                pending=db.execute("SELECT * FROM codex_jobs WHERE action=? AND fingerprint=? AND state IN ('queued','claimed') ORDER BY created DESC LIMIT 12",(action,fingerprint)).fetchall()
                for candidate in pending:
                    origin=json.loads(candidate['origin'] or '{}')
                    if str(origin.get('chat_id') or self.r.owner)!=str(context.get('chat_id') or self.r.owner):continue
                    db.execute('UPDATE codex_jobs SET notify_pending=1 WHERE id=?',(candidate['id'],))
                    current=db.execute('SELECT * FROM codex_jobs WHERE id=?',(candidate['id'],)).fetchone()
                    return dict(self.view(current),reused_pending_query=True)
            if not self.online(db) and action not in ('send','stop','create'):
                return {'online':False,'state':'offline','note':'电脑连接器暂未在线，可稍后查询；任务提交仍可排队。'}
            previous=db.execute('SELECT * FROM codex_jobs WHERE source=? AND fingerprint=?',(source,fingerprint)).fetchone()
            if previous:
                if action in ('list','read','usage') and previous['state'] not in ('queued','claimed'):
                    source+=':refresh:'+secrets.token_hex(4)
                else:return self.view(previous)
            identifier=secrets.token_hex(12)
            chain_id=identifier if action=='send' else None
            db.execute('INSERT INTO codex_jobs(id,source,fingerprint,action,args,state,claim,created,updated,output,chain_id,auto_reply) VALUES(?,?,?,?,?,?,?,?,?,?,?,0)',
                (identifier,source,fingerprint,action,json.dumps(clean,ensure_ascii=False),'queued','',now,now,'',chain_id))
            db.execute('UPDATE codex_jobs SET origin=? WHERE id=?',(json.dumps(context,ensure_ascii=False),identifier))
            if action=='send' and not human_source:db.execute('UPDATE codex_jobs SET auto_reply=1 WHERE id=?',(identifier,))
            if hasattr(self.r,'loops'):self.r.loops.bind_job(db,context,getattr(self.r,'backend','codex'),identifier)
            if chain_id:
                db.execute('INSERT INTO codex_chains(id,thread_id,initial_job,state,automatic_exchanges,created,updated) VALUES(?,?,?,"queued",0,?,?)',
                    (chain_id,clean['thread_id'],identifier,now,now))
            db.execute("DELETE FROM codex_jobs WHERE state NOT IN ('queued','claimed') AND id NOT IN (SELECT id FROM codex_jobs ORDER BY created DESC LIMIT 200)")
        for _ in range(5 if action in ('list','read','usage','artifact') else 40):
            time.sleep(.2)
            with self.r.db() as db:row=db.execute('SELECT * FROM codex_jobs WHERE id=?',(identifier,)).fetchone()
            if row['state'] not in ('queued','claimed'):
                with self.r.db() as db:return self.query_result_view(db,row,context)
        with self.r.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM codex_jobs WHERE id=?',(identifier,)).fetchone()
            if row['state'] in ('queued','claimed') and action in ('list','read','usage','create','artifact'):
                db.execute('UPDATE codex_jobs SET notify_pending=1 WHERE id=?',(identifier,))
                row=db.execute('SELECT * FROM codex_jobs WHERE id=?',(identifier,)).fetchone()
            return self.query_result_view(db,row,context)

    def reconcile_read_result(self,db,output):
        thread=output.get('thread_id')
        if not thread:return
        queue_complete='pending_messages' in output and output.get('more_pending_messages') is False
        queued={p.get('client_user_message_id') for p in output.get('pending_messages',[])}
        for job in db.execute("SELECT * FROM codex_jobs WHERE action='send' AND state='done' ORDER BY created DESC LIMIT 80").fetchall():
            old=json.loads(job['output'] or '{}');args=json.loads(job['args'])
            if (old.get('thread_id') or args.get('thread_id'))!=thread:continue
            if old.get('submission_status') in ('superseded','cancelled'):continue
            current=normalized_output(old)
            for turn in output.get('turns',[]):
                matches=(current.get('turn_id') and current['turn_id']==turn.get('id')) or (current.get('client_user_message_id') and current['client_user_message_id']==turn.get('client_user_message_id'))
                if matches and turn.get('terminal_confirmed') is True:
                    current.update({k:turn[k] for k in ('execution_state','terminal_confirmed','completion_outcome','observed_at','status_source') if k in turn})
                    current.update(turn_id=turn['id'],turn_status=turn['status'])
                    final='\n\n'.join(m.get('text','') for m in turn.get('messages',[]) if m.get('is_final') or m.get('phase')=='final_answer')
                    if final:current['final']=final[:6000]
                    current=normalized_output(current)
                    db.execute('UPDATE codex_chains SET execution_state=?,last_turn=? WHERE id=?',(current.get('execution_state'),turn['id'],job['chain_id']))
            if queue_complete and current.get('client_user_message_id') and current['client_user_message_id'] not in queued and not current.get('turn_id') and current.get('terminal_confirmed') is not True:
                current.update(submission_state=current.get('submission_state',current.get('state')),state='unconfirmed',execution_state='unconfirmed',
                    native_queue_status='absent',requires_reconciliation=True,observed_at=output.get('observed_at'))
            if current!=old:db.execute('UPDATE codex_jobs SET output=?,updated=? WHERE id=?',(json.dumps(current,ensure_ascii=False),time.time(),job['id']))
        latest=db.execute("SELECT output FROM codex_jobs WHERE action='send' AND state='done' AND json_extract(args,'$.thread_id')=? ORDER BY created DESC LIMIT 1",(thread,)).fetchone()
        if latest:
            value=normalized_output(json.loads(latest['output'] or '{}'))
            output['last_execution']={k:value[k] for k in ('turn_id','execution_state','terminal_confirmed','completion_outcome','observed_at') if k in value}

    def worker(self,data):
        action=data.get('action');now=time.time()
        if action=='native_turn':return self.native_turn(data)
        if action=='poll':
            with self.r.db() as db:
                db.execute('BEGIN IMMEDIATE');self.r._set(db,'codex_worker_seen',now)
                if data.get('protocol_version') in (2,3,4,5):self.r._set(db,'codex_worker_protocol',data['protocol_version'])
                watches=self.pending_watchers(db)
                native_watches=[dict(row) for row in db.execute('SELECT thread_id,cursor_turn_id FROM codex_native_threads ORDER BY created DESC LIMIT 4')]
                if data.get('active_id'):
                    db.execute("UPDATE codex_jobs SET updated=? WHERE id=? AND claim=? AND state='claimed'",(now,data['active_id'],data.get('claim','')))
                    waiting=bool(db.execute("SELECT 1 FROM codex_jobs WHERE state='queued' LIMIT 1").fetchone())
                    return {'job':None,'watches':watches,'native_watches':native_watches,'work_waiting':waiting}
                db.execute("UPDATE codex_jobs SET state='unknown',output=? WHERE state='claimed' AND updated<?",(json.dumps({'note':'连接中断，执行结果不明；未自动重发'}),now-90))
                row=db.execute("SELECT * FROM codex_jobs WHERE state='queued' ORDER BY created LIMIT 1").fetchone()
                if not row:return {'job':None,'watches':watches,'native_watches':native_watches}
                claim=secrets.token_hex(16)
                db.execute("UPDATE codex_jobs SET state='claimed',claim=?,updated=? WHERE id=?",(claim,now,row['id']))
                return {'job':{'id':row['id'],'claim':claim,'auto_reply':bool(row['auto_reply']),**json.loads(row['args'])},'watches':watches,'native_watches':native_watches}
        if action=='result':
            output=data.get('result',{})
            if not isinstance(output,dict):raise ValueError('worker结果必须为对象')
            with self.r.db() as db:
                row=db.execute('SELECT * FROM codex_jobs WHERE id=? AND claim=?',(data.get('id'),data.get('claim'))).fetchone()
                if not row or row['state']!='claimed':return {'accepted':False}
                if row['action']=='artifact' and not data.get('error'):
                    output=self.r.artifacts.ingest(db,row,output)
                if row['action']=='read' and not data.get('error'):self.reconcile_read_result(db,output)
                if row['action']=='usage':
                    if data.get('error'):
                        output={'error':'Codex usage query failed'}
                    else:
                        try:
                            output=sanitize_usage_summary(output)
                            output['cache_state']='fresh'
                            self.store_usage(db,output)
                        except Exception:
                            output={'error':'Codex usage response could not be validated'}
                            data=dict(data,error=True)
                else:
                    output=redact(json.dumps(output,ensure_ascii=False))
                    output=json.loads(output)
                    if row['action']=='list':
                        origin=json.loads(row['origin'] or '{}')
                        selected=self.resolve_thread(db,{},origin,remember=False)
                        if selected:output['default_thread_id']=selected
                    usage=output.get('usage') if isinstance(output,dict) else None
                    if isinstance(usage,dict):
                        try:
                            output['usage']=self.store_usage(db,usage)
                            output['usage']['cache_state']='fresh'
                        except Exception:output.pop('usage',None)
                    if row['action']=='send' and output.get('state')=='usage_blocked':
                        db.execute('UPDATE codex_chains SET state="usage_blocked",updated=? WHERE id=?',(now,row['chain_id']))
                output=normalized_output(output)
                encoded=json.dumps(output,ensure_ascii=False)
                if len(encoded.encode())>32000:raise ValueError('worker结果超过长度上限')
                db.execute('UPDATE codex_jobs SET state=?,updated=?,output=? WHERE id=?',('error' if data.get('error') else 'done',now,encoded,row['id']))
                if row['action']=='create' and not data.get('error') and output.get('thread_id'):
                    origin=json.loads(row['origin'] or '{}')
                    scope=str(origin.get('chat_id') or self.r.owner).split('/',1)[0]
                    db.execute('INSERT OR REPLACE INTO codex_thread_bindings VALUES(?,?,?)',(scope,output['thread_id'],now))
                    db.execute('DELETE FROM codex_query_cache WHERE cache_key=?',(self.query_key({'action':'list','query':''}),))
                query_event=None
                if row['action'] in ('list','read','usage','create','artifact'):
                    if not data.get('error') and row['action']!='artifact':
                        db.execute('INSERT OR REPLACE INTO codex_query_cache(cache_key,payload,created) VALUES(?,?,?)',(self.query_key(json.loads(row['args'])),encoded,now))
                        if self.result_dedup:
                            db.execute('INSERT OR REPLACE INTO codex_query_cache_jobs VALUES(?,?)',(self.query_key(json.loads(row['args'])),row['id']))
                    query_event=self.query_event(db,row,output)
                if row['action']=='send' and output.get('state')=='queued_on_owner' and output.get('thread_id'):
                    db.execute('INSERT OR IGNORE INTO codex_native_threads VALUES(?,NULL,?,?)',(output['thread_id'],now,now))
                if row['action'] in ('send','stop'):self.invalidate_read_cache(db,json.loads(row['args']).get('thread_id'))
                if data.get('error') and row['action']=='send' and row['chain_id']:
                    self.enqueue_event(db,row,'failure-'+row['id'],'failed','Codex执行入口返回错误，原任务及请求记录已保留。结合错误和最新回执继续处理，或向jg说明实际阻断。')
                if row['action']=='stop' and output.get('stopped') is True:
                    stop_args=json.loads(row['args'] or '{}');thread=stop_args.get('thread_id')
                    targets=[r['id'] for r in db.execute('SELECT id FROM codex_chains WHERE thread_id=? AND state NOT IN ("completed","failed","stopped","auto_limit")',(thread,))]
                    for chain_id in targets:
                        self._close_chain(db,chain_id,'stopped')
            return {'accepted':True,'event_id':query_event,'model_wake_required':bool(query_event)}
        if action=='observation':
            job=str(data.get('job_id',''));turn=str(data.get('turn_id',''));client_id=str(data.get('client_user_message_id',''))
            if data.get('execution_state') not in ('running','pending','unknown') or data.get('terminal_confirmed') is not False:
                raise ValueError('非终态观察必须明确未确认终态')
            with self.r.db() as db:
                db.execute('BEGIN IMMEDIATE')
                row=db.execute("SELECT * FROM codex_jobs WHERE id=? AND action='send' AND state='done'",(job,)).fetchone()
                if not row:return {'accepted':False}
                result=json.loads(row['output'] or '{}')
                if result.get('terminal_confirmed') is True or (result.get('state') not in ('queued_on_owner','running','unconfirmed') and result.get('submission_state')!='queued_on_owner'):return {'accepted':False}
                if not turn or (result.get('turn_id')!=turn and (not client_id or result.get('client_user_message_id')!=client_id)):
                    return {'accepted':False}
                chain=db.execute('SELECT state FROM codex_chains WHERE id=?',(row['chain_id'],)).fetchone()
                if chain and chain['state'] in ('completed','failed','stopped','auto_limit'):return {'accepted':True,'closed_chain_preserved':True}
                old=result.pop('turn_status',None)
                if old:result['reported_turn_status']=old;result.pop('final',None)
                result.update(turn_id=turn,execution_state=data['execution_state'],terminal_confirmed=False,
                    status_source=str(data.get('status_source',''))[:80],observed_at=data.get('observed_at'),task_goal_status='not_evaluated')
                result=normalized_output(result)
                db.execute('UPDATE codex_jobs SET updated=?,output=? WHERE id=?',(now,json.dumps(result,ensure_ascii=False),job))
                self.invalidate_read_cache(db,result.get('thread_id') or json.loads(row['args']).get('thread_id'))
                if old:
                    event=db.execute('SELECT * FROM codex_events WHERE turn_id=? AND job_id=?',(turn,job)).fetchone()
                    if not event or (event['status'] in ('pending','claimed','delivered') and not event['reply_job']):
                        if event:db.execute("UPDATE codex_events SET status='superseded',claimed_until=NULL WHERE id=?",(event['id'],))
                        db.execute('DELETE FROM codex_completions WHERE turn=? AND job=?',(turn,job))
                        db.execute("UPDATE codex_chains SET state='running',updated=? WHERE id=?",(now,row['chain_id']))
            return {'accepted':True,'model_wake_required':False,'execution_state':data['execution_state']}
        if action=='completed':
            turn=str(data.get('turn_id',''));job=str(data.get('job_id',''))
            if data.get('status') not in ('completed','failed','interrupted','cancelled','unknown'):raise ValueError('Codex回报必须为最终状态')
            if not re.fullmatch(r'[a-zA-Z0-9_-]{6,100}',turn):raise ValueError('无效turn_id')
            with self.r.db() as db:
                row=db.execute("SELECT * FROM codex_jobs WHERE id=? AND action IN ('send','probe')",(job,)).fetchone()
                if not row or row['state']!='done':return {'accepted':False}
                result=json.loads(row['output'] or '{}')
                if (result.get('state')=='queued_on_owner' or result.get('submission_state')=='queued_on_owner') and data.get('terminal_confirmed') is not True:
                    return {'accepted':False,'reason':'native_terminal_not_confirmed'}
                client_id=str(data.get('client_user_message_id',''))
                if result.get('turn_id')!=turn and (not client_id or result.get('client_user_message_id')!=client_id):return {'accepted':False}
                fresh=db.execute('INSERT OR IGNORE INTO codex_completions VALUES(?,?,?)',(turn,job,now)).rowcount
                result.update(turn_id=turn,turn_status=str(data.get('status','unknown'))[:40],final=redact(data.get('text',''))[:6000])
                for key in ('terminal_confirmed','status_source','observed_at','phase','final_truncated','task_goal_status',
                            'execution_state','completion_outcome','reply_present','error'):
                    if key in data:result[key]=data[key]
                result=normalized_output(result)
                db.execute('UPDATE codex_jobs SET updated=?,output=? WHERE id=?',(now,json.dumps(result,ensure_ascii=False),job))
                db.execute('UPDATE codex_chains SET execution_state=?,last_turn=?,updated=? WHERE id=?',(result.get('execution_state',data['status']),turn,now,row['chain_id']))
                self.invalidate_read_cache(db,result.get('thread_id') or json.loads(row['args']).get('thread_id'))
                db.execute('DELETE FROM codex_completions WHERE created<?',(now-30*86400,))
                event_id=None
                if fresh and row['action']=='send':event_id=self.enqueue_event(db,row,turn,result['turn_status'],result['final'])
            if not fresh:return {'accepted':True,'duplicate':True}
            if row['action']=='probe':return {'accepted':True,'probe':True}
            return {'accepted':True,'event_id':event_id,'model_wake_required':bool(event_id)}
        if action=='unknown':
            job=str(data.get('job_id',''));client_id=str(data.get('client_user_message_id',''));turn_id=str(data.get('turn_id',''))
            if not re.fullmatch(r'[a-zA-Z0-9_-]{6,100}',job):raise ValueError('无效job_id')
            if not client_id and not turn_id:raise ValueError('unknown回报需要真实接收凭据')
            with self.r.db() as db:
                row=db.execute("SELECT * FROM codex_jobs WHERE id=? AND action='send' AND state='done'",(job,)).fetchone()
                if not row:return {'accepted':False}
                result=json.loads(row['output'] or '{}')
                if client_id and result.get('client_user_message_id')!=client_id:return {'accepted':False}
                if turn_id and result.get('turn_id')!=turn_id:return {'accepted':False}
                if result.get('turn_status'):return {'accepted':True,'duplicate':True}
                marker='unknown-'+job
                fresh=db.execute('INSERT OR IGNORE INTO codex_completions VALUES(?,?,?)',(marker,job,now)).rowcount
                result.update(turn_status='unknown',final='Codex 接收队列中未找到可核实的最终回执。没有自动重发；请jg检查原 thread 后决定。')
                db.execute('UPDATE codex_jobs SET updated=?,output=? WHERE id=?',(now,json.dumps(result,ensure_ascii=False),job))
                event_id=self.enqueue_event(db,row,marker,'unknown',result['final']) if fresh else None
            return {'accepted':True,'event_id':event_id,'model_wake_required':bool(event_id)}
        raise ValueError('未知worker操作')
