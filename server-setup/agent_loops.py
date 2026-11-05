"""Durable task contexts and revocable autonomous opportunities, on runtime.sqlite.

Scheduling uses the existing heartbeat lease. Executors retain their existing
queues; bindings keep complete receipts and route them back to the owning task.
"""
import json
import re
import secrets
import time

PATROL_SECONDS=25*60

def user_switch(text):
    text=text.strip()
    prefix=r'^(?:(?:请|帮我|我想|先|现在|给我)\s*)*'
    if re.match(prefix+r'(?:关闭自主活动|第二类先撤|第二类全撤|第二类关掉|自主活动全撤|把自主活动关掉)(?:[。！!，,／/\s]|$)',text):return False
    if re.match(prefix+r'(?:开启自主活动|恢复自主活动|打开自主活动)(?:[。！!，,\s]|$)',text):return True
    return None

def monitor_cancel(text):
    return bool(re.match(r'^(?:(?:请|帮我|先)\s*)*(?:把(?:任务)?巡检(?:先)?(?:关了吧|关掉|关了|停掉|撤掉)|(?:关闭|停止|取消|撤销)(?:任务)?巡检)(?:[。！!，,\s]|$)',text.strip()))


class AgentLoops:
    def __init__(self,runtime):
        self.r=runtime
        with runtime.db() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS loop_tasks(id TEXT PRIMARY KEY,version INTEGER,data TEXT,monitor INTEGER,report INTEGER,next_due REAL,origin_epoch INTEGER,created REAL,updated REAL);
            CREATE TABLE IF NOT EXISTS loop_task_history(id INTEGER PRIMARY KEY,task_id TEXT,version INTEGER,data TEXT,source TEXT,created REAL);
            CREATE TABLE IF NOT EXISTS loop_task_events(id INTEGER PRIMARY KEY,task_id TEXT,kind TEXT,source TEXT,content TEXT,created REAL);
            CREATE TABLE IF NOT EXISTS loop_bindings(kind TEXT,key TEXT,task_id TEXT,epoch INTEGER,PRIMARY KEY(kind,key));
            CREATE TABLE IF NOT EXISTS loop_wakes(id TEXT PRIMARY KEY,task_id TEXT,epoch INTEGER,kind TEXT);
            CREATE TABLE IF NOT EXISTS loop_autonomy(id INTEGER PRIMARY KEY CHECK(id=1),enabled INTEGER,epoch INTEGER,interval_seconds INTEGER,next_due REAL,note TEXT);
            CREATE TABLE IF NOT EXISTS loop_activity(id INTEGER PRIMARY KEY,epoch INTEGER,kind TEXT,content TEXT,created REAL);
            CREATE TABLE IF NOT EXISTS loop_quota(provider TEXT,reset_at REAL,status TEXT,task_ids TEXT,PRIMARY KEY(provider,reset_at));
            CREATE TABLE IF NOT EXISTS loop_requests(id INTEGER PRIMARY KEY,task_id TEXT,version INTEGER,request TEXT,created REAL);
            CREATE TABLE IF NOT EXISTS loop_receipts(backend TEXT,job_id TEXT,task_id TEXT,record TEXT,updated REAL,PRIMARY KEY(backend,job_id));
            CREATE TABLE IF NOT EXISTS loop_quota_watch(provider TEXT PRIMARY KEY,enabled INTEGER,origin_epoch INTEGER);
            ''')
            interval=runtime.settings.get('autonomy_interval_seconds',900)
            db.execute('INSERT OR IGNORE INTO loop_autonomy VALUES(1,1,1,?,?,?)',(interval,time.time()+interval,''))
            for backend in ('codex','dsh','claude'):
                for event in ('UPDATE','DELETE'):
                    ref='new' if event=='UPDATE' else 'old'
                    record="json_object('id',"+ref+".id,'action',"+ref+".action,'args',"+ref+".args,'state',"+ref+".state,'output',"+ref+".output,'chain_id',"+ref+".chain_id,'origin',"+ref+".origin,'backend','"+backend+"')"
                    db.execute(f'''CREATE TRIGGER IF NOT EXISTS loop_{backend}_{event.lower()} AFTER {event} ON {backend}_jobs BEGIN
                    INSERT OR REPLACE INTO loop_receipts SELECT '{backend}',{ref}.id,task_id,{record},strftime('%s','now') FROM loop_bindings WHERE kind='{backend}' AND key={ref}.id; END''')

    @staticmethod
    def view(row):
        return dict(json.loads(row['data']),task_id=row['id'],version=row['version'],monitor=bool(row['monitor']),report=bool(row['report']),origin_epoch=row['origin_epoch'],created_at=row['created'],updated_at=row['updated'])

    def source(self,db,context):
        msg=str(context.get('message_id') or context.get('turn') or '')
        if not msg:return {'session_key':context.get('session_key'),'kind':'automatic'}
        row=db.execute("SELECT id,msg,content,created FROM events WHERE chat=? AND msg=? AND kind='user' ORDER BY id DESC LIMIT 1",(self.r.owner,msg)).fetchone()
        return dict(row) if row else {'message_id':msg,'session_key':context.get('session_key'),'kind':'automatic'}

    def resolve(self,db,context):
        session=str(context.get('session_key',''));msg=str(context.get('message_id') or context.get('turn') or '')
        task=session.removeprefix('agent:task-') if session.startswith('agent:task-') else context.get('task_id')
        epoch=None
        if session.startswith('agent:autonomy-'):
            try:epoch=int(session.removeprefix('agent:autonomy-'))
            except ValueError:pass
        wake=db.execute('SELECT * FROM loop_wakes WHERE id=?',(msg.removeprefix('heartbeat:'),)).fetchone() if msg.startswith('heartbeat:') else None
        if wake:task,epoch=wake['task_id'],wake['epoch']
        if not task:
            binding=db.execute('SELECT task_id,epoch FROM loop_bindings WHERE kind=? AND key=?',('message',msg)).fetchone()
            if binding:task,epoch=binding['task_id'],binding['epoch']
        if msg.startswith('codex-event:'):
            event_id=msg.removeprefix('codex-event:')
            for backend in ('codex','dsh','claude'):
                event=db.execute(f'SELECT job_id FROM {backend}_events WHERE id=?',(event_id,)).fetchone()
                if event:
                    binding=db.execute('SELECT task_id,epoch FROM loop_bindings WHERE kind=? AND key=?',(backend,event['job_id'])).fetchone()
                    if binding:task,epoch=binding['task_id'],binding['epoch']
                    break
        if task:
            row=db.execute('SELECT origin_epoch FROM loop_tasks WHERE id=?',(task,)).fetchone()
            if row:epoch=row['origin_epoch']
        return task,epoch

    def active(self,db,epoch):
        if epoch is None:return True
        state=db.execute('SELECT enabled,epoch FROM loop_autonomy WHERE id=1').fetchone()
        return bool(state['enabled'] and epoch==state['epoch'])

    def admitted(self,db,context):
        task,epoch=self.resolve(db,context);human=self.source(db,context)
        row=db.execute('SELECT data,monitor FROM loop_tasks WHERE id=?',(task,)).fetchone() if task else None
        if row:
            data=json.loads(row['data'])
            if data['status']=='cancelled':
                last=data.get('latest_instruction',{});cancel=db.execute("SELECT id FROM events WHERE chat=? AND msg=? AND kind='user'",(self.r.owner,last.get('message_id'))).fetchone()
                return bool(human.get('id') and cancel and human['id']>=cancel['id'])
            msg=str(context.get('message_id') or context.get('turn') or '')
            wake=db.execute("SELECT kind FROM loop_wakes WHERE id=?",(msg.removeprefix('heartbeat:'),)).fetchone() if msg.startswith('heartbeat:') else None
            if wake and wake['kind']=='task' and not row['monitor'] and data.get('monitor_cancel_source'):return False
        return bool(human.get('id') or self.active(db,epoch))

    def save(self,db,row,data,source,**fields):
        version=row['version']+1
        db.execute('INSERT INTO loop_task_history(task_id,version,data,source,created) VALUES(?,?,?,?,?)',(row['id'],row['version'],row['data'],json.dumps(source,ensure_ascii=False),time.time()))
        db.execute('UPDATE loop_tasks SET version=?,data=?,updated=? WHERE id=?',(version,json.dumps(data,ensure_ascii=False),time.time(),row['id']))
        for name,value in fields.items():
            if name not in ('monitor','report','next_due'):raise ValueError('invalid task field')
            db.execute('UPDATE loop_tasks SET '+name+'=? WHERE id=?',(value,row['id']))
        return self.view(db.execute('SELECT * FROM loop_tasks WHERE id=?',(row['id'],)).fetchone())

    def register(self,db,args,context):
        source=self.source(db,context);_,epoch=self.resolve(db,context)
        if source.get('id'):epoch=None
        goal=args.get('goal') or source.get('content')
        if not isinstance(goal,str) or not goal.strip():raise ValueError('登记任务需要目标及来源')
        if source.get('id'):
            existing=db.execute("SELECT * FROM loop_tasks WHERE json_extract(data,'$.source.id')=? AND json_extract(data,'$.goal')=?",(source['id'],goal)).fetchone()
            if existing:return self.view(existing)
        identifier=secrets.token_hex(12)
        data={'original_goal':source.get('content',goal),'goal':goal,'source':source,'completion':args.get('completion','按jg的目标判断实际可用与完成程度'),
              'agreements':args.get('agreements',''),'progress':'已登记','waiting':'','next':'推进原目标','status':'active','leftovers':''}
        now=time.time()
        db.execute('INSERT INTO loop_tasks VALUES(?,1,?,1,1,?,?,?,?)',(identifier,json.dumps(data,ensure_ascii=False),now+PATROL_SECONDS,epoch,now,now))
        if source.get('msg'):db.execute('INSERT OR REPLACE INTO loop_bindings VALUES(?,?,?,?)',('message',source['msg'],identifier,epoch))
        return self.view(db.execute('SELECT * FROM loop_tasks WHERE id=?',(identifier,)).fetchone())

    def task(self,args,context,chat):
        if chat!=self.r.owner:raise ValueError('任务记录只在jg私聊使用')
        action=args.get('action','list')
        if action=='dispatch':
            backend=args.get('backend','codex')
            if backend not in ('codex','dsh','claude_code'):raise ValueError('执行端无效')
            payload=dict(args.get('request') or {})
            payload['_context']=dict(context)
            if args.get('task_id'):payload['_context']['task_id']=args['task_id']
            if args.get('goal'):payload['_task_spec']={k:args[k] for k in ('goal','completion','agreements') if k in args}
            return self.r.call(backend,payload)
        with self.r.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if action=='register':return self.register(db,args,context)
            if action=='list':return {'report_default':self.r._get(db,'loop_reporting','1')=='1','tasks':[self.view(r) for r in db.execute('SELECT * FROM loop_tasks ORDER BY updated DESC LIMIT 30')]}
            if action in ('monitor','report') and type(args.get('enabled')) is not bool:raise ValueError('enabled需要明确true或false')
            if action=='report' and (args.get('scope')=='all' or (not args.get('task_id') and not self.resolve(db,context)[0])):
                self.r._set(db,'loop_reporting',int(bool(args.get('enabled'))))
                return {'scope':'all','enabled':bool(args.get('enabled'))}
            identifier=args.get('task_id') or self.resolve(db,context)[0]
            row=db.execute('SELECT * FROM loop_tasks WHERE id=?',(identifier,)).fetchone()
            if not row:raise ValueError('需要明确的task_id；并行任务的补充只更新对应任务')
            if action=='read':return dict(self.view(row),history=[dict(x) for x in db.execute('SELECT * FROM loop_task_history WHERE task_id=? ORDER BY id DESC LIMIT 10',(identifier,))],receipts=self.receipts(db,identifier),internal_notes=[dict(x) for x in db.execute('SELECT * FROM loop_task_events WHERE task_id=? ORDER BY id DESC LIMIT 30',(identifier,))])
            data=json.loads(row['data']);source=self.source(db,context)
            if source.get('id'):data['latest_instruction']={'text':source['content'],'message_id':source['msg'],'source_id':source['id'],'created':source['created']}
            for key in ('goal','completion','agreements','progress','waiting','next','leftovers'):
                if key in args:data[key]=args[key]
            fields={}
            if action=='finish':data['status']=args.get('status','completed');fields['monitor']=0
            elif action=='monitor':
                if args['enabled'] and data.get('monitor_cancel_source') and not source.get('id'):raise ValueError('巡检已由jg撤销，旧回执不能重新开启')
                if args['enabled']:data.pop('monitor_cancel_source',None)
                elif source.get('id') and monitor_cancel(source['content']):data['monitor_cancel_source']=source
                fields['monitor']=int(args['enabled']);fields['next_due']=time.time()+PATROL_SECONDS
            elif action=='report':fields['report']=int(bool(args.get('enabled')))
            elif action!='update':raise ValueError('任务操作无效')
            return self.save(db,row,data,source,**fields)

    def receipts(self,db,task):
        for b in db.execute("SELECT kind,key FROM loop_bindings WHERE task_id=? AND kind IN ('codex','dsh','claude')",(task,)):
            row=db.execute(f'SELECT id,action,args,state,output,chain_id,origin,created,updated FROM {b["kind"]}_jobs WHERE id=?',(b['key'],)).fetchone()
            if row:db.execute('INSERT OR REPLACE INTO loop_receipts VALUES(?,?,?,?,?)',(b['kind'],b['key'],task,json.dumps(dict(row,backend=b['kind']),ensure_ascii=False),row['updated']))
        return sorted([json.loads(r['record']) for r in db.execute('SELECT record FROM loop_receipts WHERE task_id=? ORDER BY updated',(task,))],key=lambda r:r.get('created',0))

    def pending_request(self,db,context,backend,thread):
        if self.source(db,context).get('id'):return None
        task=context.get('task_id')
        if not task:return None
        for row in db.execute('SELECT j.* FROM '+backend+'_jobs j JOIN loop_bindings b ON b.key=j.id WHERE b.kind=? AND b.task_id=? AND j.action=\'send\' ORDER BY j.created DESC',(backend,task)):
            if json.loads(row['args']).get('thread_id')!=thread:continue
            output=json.loads(row['output'] or '{}')
            if row['state'] in ('queued','claimed') or (output.get('state')=='queued_on_owner' and output.get('terminal_confirmed') is not True):return row
        return None

    def bridge_context(self,args,context,backend):
        """Called by the single bridge entry before queueing any executor request."""
        context=dict(context)
        with self.r.db() as db:
            db.execute('BEGIN IMMEDIATE');task,epoch=self.resolve(db,context)
            if not self.active(db,epoch):
                source=self.source(db,context)
                if not source.get('id'):raise ValueError('自主活动已由jg撤销；保留已提交执行，不再派新动作')
                old_task=task
                if old_task and not args.get('thread_id'):
                    for receipt in reversed(self.receipts(db,old_task)):
                        if receipt.get('backend')==backend:
                            thread=json.loads(receipt.get('output') or '{}').get('thread_id') or json.loads(receipt['args']).get('thread_id')
                            if thread:args['thread_id']=thread;break
                task=self.register(db,{'goal':args.get('text') or source['content']},context)['task_id'];epoch=None
            spec=args.get('_task_spec')
            if spec and not context.get('task_id'):
                task=self.register(db,spec,context)['task_id']
            # Explicit task ID wins; otherwise a known executor thread identifies
            # its owning task. Never guess by mixing other private chat history.
            thread=args.get('thread_id');job=args.get('job_id')
            if not thread and not job and args.get('action') in ('read','send','continue','stop','finish','wait_for_jg'):
                binding=db.execute('SELECT thread_id FROM '+backend+'_thread_bindings WHERE scope=?',(self.r.owner,)).fetchone()
                thread=binding['thread_id'] if binding else self.r.settings.get(backend+'_default_thread_id')
            if not task and (thread or job):
                for r in db.execute('SELECT b.task_id,j.id,j.args FROM loop_bindings b JOIN '+backend+'_jobs j ON j.id=b.key WHERE b.kind=? ORDER BY j.created DESC',(backend,)):
                    if (job and job==r['id']) or (thread and json.loads(r['args']).get('thread_id')==thread):task=r['task_id'];break
            if not task and args.get('action') in ('send','continue','create','read','list'):
                task=self.register(db,args.get('_task_spec') or {'goal':args.get('text') or args.get('title') or self.source(db,context).get('content') or backend+' '+str(args.get('action'))+' '+str(thread or '')},context)['task_id']
            if task:
                row=db.execute('SELECT * FROM loop_tasks WHERE id=?',(task,)).fetchone()
                if not row:raise ValueError('任务记录不存在')
                if not args.get('thread_id') and not args.get('job_id') and args.get('action') not in ('create','list','usage','status'):
                    target=thread
                    for receipt in reversed(self.receipts(db,task)):
                        if receipt.get('backend')==backend:
                            target=json.loads(receipt.get('output') or '{}').get('thread_id') or json.loads(receipt['args']).get('thread_id') or target
                            if target:break
                    if target:args['thread_id']=target
                if json.loads(row['data'])['status'] in ('cancelled','completed') and args.get('action') in ('send','continue','create'):
                    raise ValueError('该任务已收口；新目标先更新对应任务或登记新任务')
                context.update(task_id=task,task_version=row['version'])
            return context

    def bind_job(self,db,context,backend,job):
        task=context.get('task_id')
        if not task:
            if context.get('session_key')=='agent:quota':db.execute('INSERT OR REPLACE INTO loop_bindings VALUES(?,?,?,?)',(backend,job,'',self.resolve(db,context)[1]))
            return
        row=db.execute('SELECT origin_epoch FROM loop_tasks WHERE id=?',(task,)).fetchone()
        if row:db.execute('INSERT OR REPLACE INTO loop_bindings VALUES(?,?,?,?)',(backend,job,task,row['origin_epoch']))

    def autonomy(self,args,context,chat):
        if chat!=self.r.owner:raise ValueError('自主活动只在jg私聊管理')
        with self.r.db() as db:
            db.execute('BEGIN IMMEDIATE');state=dict(db.execute('SELECT * FROM loop_autonomy WHERE id=1').fetchone())
            action=args.get('action','status');_,epoch=self.resolve(db,context)
            if action in ('enable','disable'):
                source=self.source(db,context)
                if not source.get('id'):raise ValueError('总开关由jg本人决定')
                text=source['content']
                if user_switch(text)!=(action=='enable'):raise ValueError('恢复或关闭需要jg本人的明确指令')
                self.switch(db,action=='enable',text)
            elif action=='adjust':
                if not self.active(db,epoch):raise ValueError('自主活动已关闭，恢复由jg决定')
                interval=args.get('interval_seconds',state['interval_seconds'])
                if type(interval) is not int or not 120<=interval<=31622400:raise ValueError('自主活动间隔最少120秒')
                db.execute('UPDATE loop_autonomy SET interval_seconds=?,next_due=? WHERE id=1',(interval,time.time()+interval))
            elif action=='note':
                if not self.active(db,epoch):raise ValueError('自主活动已关闭')
                note=str(args.get('note',''))[:3000]
                db.execute('UPDATE loop_autonomy SET note=? WHERE id=1',(note,))
                db.execute('INSERT INTO loop_activity(epoch,kind,content,created) VALUES(?,?,?,?)',(state['epoch'],'continuity',note,time.time()))
            elif action!='status':raise ValueError('自主活动操作无效')
            return dict(db.execute('SELECT * FROM loop_autonomy WHERE id=1').fetchone(),recent=[dict(x) for x in db.execute('SELECT * FROM loop_activity WHERE epoch IS NOT NULL ORDER BY id DESC LIMIT 12')])

    def switch(self,db,enabled,text):
        state=db.execute('SELECT * FROM loop_autonomy WHERE id=1').fetchone()
        epoch=state['epoch']+1
        db.execute('UPDATE loop_autonomy SET enabled=?,epoch=?,next_due=? WHERE id=1',(int(enabled),epoch,time.time()+state['interval_seconds']))
        db.execute('INSERT INTO loop_activity(epoch,kind,content,created) VALUES(?,?,?,?)',(epoch,'enabled' if enabled else 'disabled',text,time.time()))
        if not enabled:
            db.execute('UPDATE loop_tasks SET monitor=0 WHERE origin_epoch IS NOT NULL')
            db.execute("UPDATE silent_alarms SET status='cancelled' WHERE id IN (SELECT key FROM loop_bindings WHERE kind='alarm' AND epoch IS NOT NULL) AND status IN ('pending','running')")
            db.execute("UPDATE reminders SET status='cancelled' WHERE id IN (SELECT key FROM loop_bindings WHERE kind='reminder' AND epoch IS NOT NULL) AND status='pending'")
            db.execute('UPDATE loop_quota_watch SET enabled=0 WHERE origin_epoch IS NOT NULL')
            # Already dispatched executor jobs intentionally continue. Their late
            # callbacks remain stored and are filtered at admission below.

    def inbound(self,db,text,message,reply=''):
        switch=user_switch(text)
        if switch is not None:
            self.switch(db,switch,text)
            return '[自主活动控制回执]\n'+('已开启自主活动，按当前间隔安排下一次机会。' if switch else '已关闭自主活动并撤销派生安排；已交执行端继续运行，迟到回执只保存，恢复由jg决定。')
        elif re.fullmatch(r'(活跃点|少醒点)[。！! ]*',text):
            row=db.execute('SELECT * FROM loop_autonomy WHERE id=1').fetchone();interval=max(120,min(86400,row['interval_seconds']*(.5 if '活跃' in text else 2)))
            db.execute('UPDATE loop_autonomy SET interval_seconds=?,next_due=? WHERE id=1',(int(interval),time.time()+interval))
            return '[自主活动控制回执]\n间隔已调整为'+str(int(interval))+'秒；总开关'+('开启。' if row['enabled'] else '仍关闭，恢复由jg决定。')
        binding=db.execute("SELECT task_id FROM loop_bindings WHERE kind='sent' AND key=?",(reply,)).fetchone() if reply else None
        explicit=re.search(r'\btask[:：-]([a-f0-9]{24})\b',text)
        task=explicit[1] if explicit else binding['task_id'] if binding else None
        if monitor_cancel(text):
            source=dict(db.execute("SELECT id,msg,content,created FROM events WHERE chat=? AND msg=? AND kind='user'",(self.r.owner,message)).fetchone())
            targets=db.execute('SELECT * FROM loop_tasks WHERE id=?',(task,)).fetchall() if task else db.execute('SELECT * FROM loop_tasks WHERE monitor=1').fetchall()
            for row in targets:
                data=json.loads(row['data']);data['monitor_cancel_source']=source;data['latest_instruction']={'text':text,'message_id':message,'source_id':source['id'],'kind':'monitor_cancel'}
                self.save(db,row,data,source,monitor=0)
            return '[任务巡检控制回执]\n已撤销'+str(len(targets))+'项任务巡检；已提交执行、独立额度守候和自主活动各自保留。'
        if task:
            row=db.execute('SELECT * FROM loop_tasks WHERE id=?',(task,)).fetchone()
            if row:
                source=self.source(db,{'message_id':message})
                data=json.loads(row['data']);data['latest_instruction']={'text':text,'message_id':message,'source_id':source.get('id'),'created':source.get('created')}
                cancelled=bool(re.search(r'(取消任务|停止任务|这个任务.{0,3}(取消|停))',text))
                if cancelled:data['status']='cancelled'
                self.save(db,row,data,source,**({'monitor':0} if data['status']=='cancelled' else {}))
                if cancelled:self.stop_executors(db,task,message)
                db.execute('INSERT OR REPLACE INTO loop_bindings VALUES(?,?,?,?)',('message',message,task,row['origin_epoch']))

    def stop_executors(self,db,task,message):
        for backend in ('codex','dsh','claude'):
            threads={json.loads(r['args']).get('thread_id') for r in db.execute('SELECT j.args FROM '+backend+'_jobs j JOIN loop_bindings b ON b.key=j.id WHERE b.kind=? AND b.task_id=?',(backend,task))}
            for thread in threads- {None}:
                args=json.dumps({'action':'stop','thread_id':thread},ensure_ascii=False)
                job=secrets.token_hex(12);source='jg-stop:'+message
                db.execute('INSERT OR IGNORE INTO '+backend+'_jobs(id,source,fingerprint,action,args,state,claim,created,updated,output,origin) VALUES(?,?,?,\'stop\',?,\'queued\',\'\',?,?,\'\',?)',(job,source,thread,args,time.time(),time.time(),json.dumps({'task_id':task,'message_id':message,'chat_id':self.r.owner})))
                self.bind_job(db,{'task_id':task},backend,job)

    def quota(self,args,context):
        provider=args.get('provider','codex');reset=args.get('reset_at')
        if provider not in ('codex','dsh','claude'):raise ValueError('额度执行端无效')
        with self.r.db() as db:
            # Reset is evidence, not a guessed interval. Codex is the only
            # backend currently exposing a verified usage API.
            cache=db.execute('SELECT payload FROM '+provider+'_usage_cache WHERE id=1').fetchone()
            value=json.loads(cache['payload']) if cache else {}
            valid={w.get('resets_at') for l in value.get('limits',[]) for w in (l.get('primary'),l.get('secondary')) if w}
            if type(reset) not in (int,float) or reset not in valid:raise ValueError('reset_at必须来自该执行端真实额度回执')
            task=args.get('task_id') or self.resolve(db,context)[0]
            old=db.execute('SELECT task_ids FROM loop_quota WHERE provider=? AND reset_at=?',(provider,reset)).fetchone()
            tasks=set(json.loads(old['task_ids']) if old else []);
            if task:tasks.add(task)
            db.execute('INSERT OR REPLACE INTO loop_quota VALUES(?,?,?,?)',(provider,reset,'pending',json.dumps(sorted(tasks))))
            _,epoch=self.resolve(db,context)
            watch=db.execute('SELECT origin_epoch FROM loop_quota_watch WHERE provider=?',(provider,)).fetchone()
            if watch and watch['origin_epoch'] is None:epoch=None
            db.execute('INSERT OR REPLACE INTO loop_quota_watch VALUES(?,1,?)',(provider,epoch))
            return {'provider':provider,'reset_at':reset,'task_ids':sorted(tasks),'reused':bool(old)}

    def observe_usage(self,db,provider,value):
        watch=db.execute('SELECT * FROM loop_quota_watch WHERE provider=?',(provider,)).fetchone()
        if not watch or not watch['enabled']:return
        tasks=[r[0] for r in db.execute("SELECT DISTINCT b.task_id FROM loop_bindings b JOIN loop_tasks t ON t.id=b.task_id WHERE b.kind=? AND json_extract(t.data,'$.status')='active'",(provider,))]
        for limit in value.get('limits',[]):
            for window in (limit.get('primary'),limit.get('secondary')):
                if window and type(window.get('resets_at')) is int and window['resets_at']>time.time():
                    db.execute("INSERT OR IGNORE INTO loop_quota VALUES(?,?,'pending',?)",(provider,window['resets_at'],json.dumps(tasks)))

    def claim(self,db,wake,now):
        quota=db.execute("SELECT * FROM loop_quota WHERE status='pending' AND reset_at<=? ORDER BY reset_at LIMIT 1",(now,)).fetchone()
        kind='task';task=None;epoch=None;extra=''
        if quota:
            watch=db.execute('SELECT * FROM loop_quota_watch WHERE provider=?',(quota['provider'],)).fetchone()
            epoch=watch['origin_epoch'] if watch else None
            if watch and (not watch['enabled'] or not self.active(db,epoch)):
                db.execute("UPDATE loop_quota SET status='cancelled' WHERE provider=? AND reset_at=?",(quota['provider'],quota['reset_at']));return None
            tasks=[]
            for t in json.loads(quota['task_ids']):
                owner=db.execute("SELECT origin_epoch FROM loop_tasks WHERE id=? AND json_extract(data,'$.status')='active'",(t,)).fetchone()
                if owner and self.active(db,owner['origin_epoch']):tasks.append(t)
            db.execute("UPDATE loop_quota SET status='checked' WHERE provider=? AND reset_at=?",(quota['provider'],quota['reset_at']))
            task=tasks[0] if tasks else None;kind='quota'
            extra='额度真实重置时间已到，先查'+quota['provider']+' usage；有待续任务时复用原请求与真实状态推进。额度守候独立保留。'
            for t in tasks[1:]:db.execute('UPDATE loop_tasks SET next_due=MIN(next_due,?) WHERE id=?',(now,t))
        else:
            row=db.execute('SELECT * FROM loop_tasks WHERE monitor=1 AND next_due<=? ORDER BY next_due LIMIT 1',(now,)).fetchone()
            if row:task=row['id'];epoch=row['origin_epoch'];db.execute('UPDATE loop_tasks SET next_due=? WHERE id=?',(now+PATROL_SECONDS,task))
            else:
                auto=db.execute('SELECT * FROM loop_autonomy WHERE id=1').fetchone()
                if not auto['enabled'] or auto['next_due']>now:return None
                kind='autonomy';epoch=auto['epoch']
                db.execute('UPDATE loop_autonomy SET next_due=? WHERE id=1',(now+auto['interval_seconds'],))
                extra='自主活动机会：沿兴趣阅读、探索、整理、聊天或安静结束。按需用autonomy调整下次唤醒或保存少量续记；安静一轮后仍有下次机会。持续任务用task_loop登记跟踪。续记：'+auto['note']
                recent=[dict(r) for r in db.execute("SELECT msg,kind,content,created FROM events WHERE chat=? AND kind IN ('user','assistant') ORDER BY id DESC LIMIT 12",(self.r.owner,))]
                extra+='\n最近真实往来原话（按时间）：'+json.dumps(list(reversed(recent)),ensure_ascii=False)
                extra+='\n相关记忆线索：'+self.r.memory_recall.background_context(db,self.r.owner)
        if not self.active(db,epoch):return None
        db.execute('INSERT INTO loop_wakes VALUES(?,?,?,?)',(wake,task,epoch,kind))
        db.execute('INSERT INTO wakes(id,created,day,kind) VALUES(?,?,?,?)',(wake,now,time.strftime('%Y-%m-%d'),kind))
        session='agent:task-'+task if task else 'agent:autonomy-'+str(epoch) if epoch is not None else 'agent:quota'
        if kind=='quota' and not task:session='agent:quota'
        return {'allowed':True,'wake_id':wake,'silent':True,'kind':kind,'session_key':session,'context':extra or '任务巡检：读取当前目标与约定，按真实进展判断下一步；达标时记录小遗留并finish，不反复验收。'}

    def request(self,data):
        context={k:data.get(k) for k in ('session_key','message_id','task_id')}
        with self.r.db() as db:
            task,epoch=self.resolve(db,context);active=self.admitted(db,context)
            human=bool(self.source(db,context).get('id'))
            row=db.execute('SELECT * FROM loop_tasks WHERE id=?',(task,)).fetchone() if task else None
            result={'allowed':True,'active':active,'managed':bool(row or epoch is not None or context.get('session_key')=='agent:quota'),'task_id':task or '', 'epoch':epoch,'version':row['version'] if row else 0}
            message=str(context.get('message_id') or '')
            if message.startswith('heartbeat:'):
                wake=db.execute('SELECT COALESCE(l.kind,w.kind) AS kind FROM wakes w LEFT JOIN loop_wakes l ON l.id=w.id WHERE w.id=?',(message.removeprefix('heartbeat:'),)).fetchone()
                if wake:result['wake_kind']=wake['kind']
            result['report']=bool(row['report']) if row else True
            reporting=self.r._get(db,'loop_reporting','1')=='1'
            if row or context.get('session_key')=='agent:quota':result['report']=result['report'] and reporting
            if context.get('session_key')=='agent:quota':result['context']='独立额度守候；汇报开关'+('开启' if reporting else '关闭，结果留内部')+'。到点按真实额度更新排程，接续仍有效的任务。'
            if epoch is not None and not row:result['session_key']='agent:autonomy-'+str(epoch)
            if row:
                record=self.view(row)
                record['report']=result['report']
                if human:result['report']=True
                result.update(record=record,receipts=self.receipts(db,task),session_key='agent:task-'+task)
                result['context']='[任务上下文 task_id='+task+' version='+str(row['version'])+']\n'+json.dumps(record,ensure_ascii=False)+'\n真实派单与回执（完整记录按task_loop read取回）：\n'+json.dumps(result['receipts'][-12:],ensure_ascii=False)+'\n自动最终文本留内部。联系jg时显式发送；汇报开关开启可主动报告阻断、决策需求与完成结果。已提交请求复用；实际达标后记录小遗留并结束巡检，额度守候独立保留。'
                notes=[dict(r) for r in db.execute('SELECT kind,source,content,created FROM loop_task_events WHERE task_id=? ORDER BY id DESC LIMIT 3',(task,))]
                if notes:result['context']+='\n上次任务内部续记（当前目标、约定与真实回执优先）：'+json.dumps(list(reversed(notes)),ensure_ascii=False)
                if human:result['context']+='\n本轮是真人新消息，可直接回应眼前要求；后台汇报开关保持原设置。'
                if data.get('assembled_request') is not None:
                    version=data.get('assembled_task_version',row['version'])
                    db.execute('INSERT INTO loop_requests(task_id,version,request,created) VALUES(?,?,?,?)',(task,version,json.dumps(data['assembled_request'],ensure_ascii=False),time.time()))
                    db.execute('DELETE FROM loop_requests WHERE id NOT IN (SELECT id FROM loop_requests ORDER BY id DESC LIMIT 8)')
            return result

    def send_gate(self,db,data):
        task,epoch=self.resolve(db,data)
        if not self.admitted(db,data):return 'autonomy_revoked' if not self.active(db,epoch) else 'task_cancelled'
        if self.source(db,data).get('id'):return ''
        if not self.active(db,epoch):return 'autonomy_revoked'
        if task or data.get('session_key')=='agent:quota':
            row=db.execute('SELECT report,data FROM loop_tasks WHERE id=?',(task,)).fetchone()
            if self.source(db,data).get('id'):return ''
            if self.r._get(db,'loop_reporting','1')!='1':return 'task_report_disabled'
            if row and (not row['report'] or json.loads(row['data'])['status']=='cancelled'):return 'task_report_disabled'
        return ''

    def event_context(self,db,backend,job):
        binding=db.execute('SELECT task_id,epoch FROM loop_bindings WHERE kind=? AND key=?',(backend,job)).fetchone()
        if not binding:return {}
        if not binding['task_id']:return {'session_key':'agent:quota','task_id':'','revoked':not self.active(db,binding['epoch'])}
        row=db.execute('SELECT data FROM loop_tasks WHERE id=?',(binding['task_id'],)).fetchone()
        return {'session_key':'agent:task-'+binding['task_id'],'task_id':binding['task_id'],'revoked':not self.active(db,binding['epoch']),
                'archived':bool(row and json.loads(row['data'])['status'] in ('completed','cancelled','stopped','failed'))}
