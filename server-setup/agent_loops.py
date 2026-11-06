"""Durable task contexts and revocable autonomous opportunities, on runtime.sqlite.

Scheduling uses the existing heartbeat lease. Executors retain their existing
queues; bindings keep complete receipts and route them back to the owning task.
"""
import json
import re
import secrets
import time
import datetime as dt
from execution_status import receipt_summary
from autonomy_window import DailyWindow
from pathlib import PurePosixPath

PATROL_SECONDS=25*60
ARCHIVED=('completed','cancelled','stopped','failed','handed_back')

def command_text(text):
    return re.sub(r'“[^”]*”|「[^」]*」|"[^"\n]*"|`[^`]*`','',text)

def negated_control(text):
    return bool(re.search(r'(?:不要|别|不能|不是让你|没有让你).{0,8}(?:关闭|关掉|撤销|停掉|撤掉|关了|停了|撤了)',command_text(text)))

def all_background_off(text):
    return not negated_control(text) and bool(re.search(r'(?:把)?后台(?:的)?(?:闹钟|时钟|活动)(?:都|全|全部)?(?:先)?(?:关了|关掉|关闭|停了|停掉|撤了|撤掉)',command_text(text)))

def task_handoff(text):
    text=command_text(text)
    if re.search(r'(剩下的(?:活)?(?:给我|我来)|(?:这个)?任务(?:已经)?(?:结束|收工|完成)(?:啦|了|吧|[。！!，,\s]|$))',text):return 'handed_back'
    return None

def user_switch(text):
    if negated_control(text):return None
    text=command_text(text).strip()
    if all_background_off(text):return False
    prefix=r'^(?:(?:请|帮我|我想|先|现在|给我)\s*)*'
    if re.search(r'(?:关闭|停掉|撤掉)(?:自主|自由)活动|(?:自主|自由)活动(?:都|先)?(?:关了|关掉|停了|停掉|全撤)|第二类(?:先撤|全撤|关掉)|把(?:自主|自由)活动关掉',text):return False
    if re.match(prefix+r'(?:开启自主活动|恢复自主活动|打开自主活动)(?:[。！!，,\s]|$)',text):return True
    return None

def monitor_cancel(text):
    return not negated_control(text) and bool(re.search(r'(?:(?:任务)?巡检|任务时钟|任务闹钟)(?:都|先)?(?:关了吧|关掉|关了|停掉|停了|撤掉|撤了)|(?:关闭|停止|取消|撤销)(?:(?:任务)?巡检|任务时钟|任务闹钟)',command_text(text)))


class AgentLoops:
    def __init__(self,runtime):
        self.r=runtime
        self.daily_window=DailyWindow(runtime.settings.get('autonomy_daily_window'))
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
        if hasattr(self.r,'companion')and self.r.companion.turn_blocked(db,context):return False
        task,epoch=self.resolve(db,context);human=self.source(db,context)
        row=db.execute('SELECT data,monitor FROM loop_tasks WHERE id=?',(task,)).fetchone() if task else None
        if row:
            data=json.loads(row['data'])
            if data['status'] in ARCHIVED:
                last=data.get('monitor_cancel_source') or data.get('latest_instruction',{})
                cutoff=last.get('id') or last.get('source_id') or 0
                return bool(human.get('id') and human['id']>=cutoff)
            msg=str(context.get('message_id') or context.get('turn') or '')
            wake=db.execute("SELECT kind FROM loop_wakes WHERE id=?",(msg.removeprefix('heartbeat:'),)).fetchone() if msg.startswith('heartbeat:') else None
            if wake and wake['kind'] in ('task','alarm') and not row['monitor']:
                run=db.execute('SELECT a.purpose FROM silent_alarm_runs r JOIN silent_alarms a ON a.id=r.alarm_id WHERE r.wake_id=?',(msg.removeprefix('heartbeat:'),)).fetchone()
                if not run or run['purpose'] not in ('quota_reset','quota_watch'):return False
        return bool(human.get('id') or self.active(db,epoch))

    def scheduling_allowed(self,db,context):
        if not self.admitted(db,context):return False
        if self.source(db,context).get('id'):return True
        task,_=self.resolve(db,context)
        row=db.execute('SELECT monitor FROM loop_tasks WHERE id=?',(task,)).fetchone() if task else None
        return not row or bool(row['monitor'])

    def receipt_recovery(self,db,context):
        message=str(context.get('message_id') or '')
        if not message.startswith('codex-event:'):return False
        for backend in ('codex','dsh','claude'):
            row=db.execute('SELECT recovery FROM '+backend+'_events WHERE id=?',(message.removeprefix('codex-event:'),)).fetchone()
            if row:return bool(row['recovery'])
        return False

    def cancel_schedules(self,db,task,*,all_derived=False):
        purposes=" AND purpose NOT IN ('quota_reset','quota_watch')"
        count=db.execute("UPDATE silent_alarms SET status='cancelled',updated=? WHERE status IN ('pending','running') AND id IN (SELECT key FROM loop_bindings WHERE kind='alarm' AND task_id=?)"+purposes,(time.time(),task)).rowcount
        if all_derived:
            count+=db.execute("UPDATE reminders SET status='cancelled' WHERE status='pending' AND id IN (SELECT key FROM loop_bindings WHERE kind='reminder' AND task_id=?)",(task,)).rowcount
        return count

    def stop_monitor(self,db,row,source,status=None):
        data=json.loads(row['data']);data['monitor_cancel_source']=source
        data['control_version']=data.get('control_version',0)+1
        if status:
            data['status']=status;data['next']='已交还jg，后续回执只保存';data['waiting']=''
            data['latest_instruction']={'text':source.get('content',''),'message_id':source.get('msg'),'source_id':source.get('id'),'kind':'handoff'}
        cancelled=self.cancel_schedules(db,row['id'],all_derived=bool(status))
        result=self.save(db,row,data,source,monitor=0)
        return dict(result,cancelled_schedules=cancelled,executors='preserved')

    def require_deliveries(self,db,task):
        row=db.execute('SELECT data FROM loop_tasks WHERE id=?',(task,)).fetchone()
        if not row:return
        data=json.loads(row['data'])
        for path in data.get('deliverables',[]):
            full=PurePosixPath(path)
            if not full.is_absolute():full=PurePosixPath('/var/lib/picoclaw/.picoclaw/workspace')/full
            if data.get('deliveries',{}).get(str(full),{}).get('state')!='verified':
                raise ValueError('执行端已完成与成果已交付分别记录；该文件还缺校验交付回执：'+str(full))

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
              'agreements':args.get('agreements',''),'progress':'已登记','waiting':'','next':'推进原目标','status':'active','leftovers':'','instruction_version':1,'control_version':0,'deliverables':args.get('deliverables',[])}
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
            if args.get('goal'):payload['_task_spec']={k:args[k] for k in ('goal','completion','agreements','deliverables') if k in args}
            return self.r.call(backend,payload)
        with self.r.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if action=='register':return self.register(db,args,context)
            if action=='list':return {'report_default':self.r._get(db,'loop_reporting','1')=='1','tasks':[self.view(r) for r in db.execute('SELECT * FROM loop_tasks ORDER BY updated DESC LIMIT 30')]}
            if action in ('monitor','report') and type(args.get('enabled')) is not bool:raise ValueError('enabled需要明确true或false')
            if action=='monitor' and args.get('scope')=='all':
                if args['enabled']:raise ValueError('恢复任务巡检需要明确task_id')
                source=self.source(db,context)
                if not source.get('id'):raise ValueError('全部任务撤销需要jg原话来源')
                rows=db.execute("SELECT * FROM loop_tasks WHERE json_extract(data,'$.status')='active'").fetchall()
                return {'scope':'all','monitor':False,'tasks':[self.stop_monitor(db,row,source) for row in rows]}
            if action=='report' and (args.get('scope')=='all' or (not args.get('task_id') and not self.resolve(db,context)[0])):
                self.r._set(db,'loop_reporting',int(bool(args.get('enabled'))))
                return {'scope':'all','enabled':bool(args.get('enabled'))}
            identifier=args.get('task_id') or self.resolve(db,context)[0]
            row=db.execute('SELECT * FROM loop_tasks WHERE id=?',(identifier,)).fetchone()
            if not row:raise ValueError('需要明确的task_id；并行任务的补充只更新对应任务')
            if action=='read':return dict(self.view(row),history=[dict(x) for x in db.execute('SELECT * FROM loop_task_history WHERE task_id=? ORDER BY id DESC LIMIT 10',(identifier,))],receipts=self.receipts(db,identifier),internal_notes=[dict(x) for x in db.execute('SELECT * FROM loop_task_events WHERE task_id=? ORDER BY id DESC LIMIT 30',(identifier,))])
            data=json.loads(row['data']);source=self.source(db,context)
            if source.get('id') and any(k in args for k in ('goal','completion','agreements','deliverables')):
                data['latest_instruction']={'text':source['content'],'message_id':source['msg'],'source_id':source['id'],'created':source['created']}
                data['instruction_version']=data.get('instruction_version',1)+1
            for key in ('goal','completion','agreements','progress','waiting','next','leftovers','deliverables'):
                if key in args:data[key]=args[key]
            fields={}
            if action=='finish':
                data['status']=args.get('status','completed');fields['monitor']=0
                if data['status']=='completed':self.require_deliveries(db,row['id'])
                self.cancel_schedules(db,row['id'],all_derived=True)
            elif action=='monitor':
                cancel=data.get('monitor_cancel_source') or {}
                if args['enabled'] and cancel and (not source.get('id') or source['id']<=cancel.get('id',0)):raise ValueError('巡检已由jg撤销，旧指令或回执不能重新开启')
                if args['enabled']:
                    data.pop('monitor_cancel_source',None)
                    if data['status'] in ARCHIVED:
                        if not source.get('id'):raise ValueError('恢复已交还任务需要jg新指令')
                        data['status']='active';data['latest_instruction']={'text':source['content'],'message_id':source['msg'],'source_id':source['id'],'kind':'resume'}
                        data['instruction_version']=data.get('instruction_version',1)+1
                else:
                    data['monitor_cancel_source']=source;data['control_version']=data.get('control_version',0)+1
                    self.cancel_schedules(db,row['id'])
                fields['monitor']=int(args['enabled']);fields['next_due']=time.time()+PATROL_SECONDS
            elif action=='report':fields['report']=int(bool(args.get('enabled')))
            elif action!='update':raise ValueError('任务操作无效')
            return self.save(db,row,data,source,**fields)

    def receipts(self,db,task):
        for b in db.execute("SELECT kind,key FROM loop_bindings WHERE task_id=? AND kind IN ('codex','dsh','claude')",(task,)):
            row=db.execute(f'SELECT id,action,args,state,output,chain_id,origin,created,updated FROM {b["kind"]}_jobs WHERE id=?',(b['key'],)).fetchone()
            if row:db.execute('INSERT OR REPLACE INTO loop_receipts VALUES(?,?,?,?,?)',(b['kind'],b['key'],task,json.dumps(dict(row,backend=b['kind']),ensure_ascii=False),row['updated']))
        return sorted([json.loads(r['record']) for r in db.execute('SELECT record FROM loop_receipts WHERE task_id=? ORDER BY updated',(task,))],key=lambda r:r.get('created',0))

    def pending_request(self,db,context,backend,thread,request=None,include_completed=False):
        if self.source(db,context).get('id'):return None
        task=context.get('task_id')
        if not task:return None
        for row in db.execute('SELECT j.* FROM '+backend+'_jobs j JOIN loop_bindings b ON b.key=j.id WHERE b.kind=? AND b.task_id=? AND j.action=\'send\' ORDER BY j.created DESC',(backend,task)):
            args=json.loads(row['args'])
            if args.get('thread_id')!=thread:continue
            if request is None:continue
            def text(value):return re.sub(r'^\[(?:Fatfish 自动回复（不是 jg）|jg 补充)\]\s*','',str(value or '')).strip()
            if text(args.get('text'))!=text(request.get('text')):continue
            origin=json.loads(row['origin'] or '{}')
            if origin.get('instruction_version',1)!=context.get('instruction_version',1):continue
            output=json.loads(row['output'] or '{}')
            if include_completed and output.get('terminal_confirmed') is True and (output.get('turn_status')=='completed' or output.get('completion_outcome')=='succeeded'):
                return row
            if output.get('submission_status') in ('superseded','cancelled','absent'):continue
            if row['state'] in ('queued','claimed') or (output.get('state') in ('queued_on_owner','running') and output.get('terminal_confirmed') is not True) or output.get('requires_reconciliation'):return row
        return None

    def bridge_context(self,args,context,backend):
        """Called by the single bridge entry before queueing any executor request."""
        context=dict(context)
        with self.r.db() as db:
            db.execute('BEGIN IMMEDIATE');task,epoch=self.resolve(db,context)
            human=self.source(db,context)
            if self.receipt_recovery(db,context) and args.get('action') in ('send','continue','create','stop'):
                raise ValueError('本轮只恢复已完成结果的接收与交付；执行端状态保留，不能重新派单或停止执行')
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
            elif not task and human.get('id') and args.get('action') in ('send','create'):
                task=self.register(db,{'goal':args.get('text') or args.get('title') or human['content']},context)['task_id']
            # Explicit task ID wins; otherwise a known executor thread identifies
            # its owning task. Never guess by mixing other private chat history.
            thread=args.get('thread_id');job=args.get('job_id')
            if not thread and not job and args.get('action') in ('read','send','continue','stop','finish','wait_for_jg'):
                binding=db.execute('SELECT thread_id FROM '+backend+'_thread_bindings WHERE scope=?',(self.r.owner,)).fetchone()
                thread=binding['thread_id'] if binding else self.r.settings.get(backend+'_default_thread_id')
            if not task and (thread or job):
                for r in db.execute('SELECT b.task_id,j.id,j.args FROM loop_bindings b JOIN '+backend+'_jobs j ON j.id=b.key WHERE b.kind=? ORDER BY j.created DESC',(backend,)):
                    if (job and job==r['id']) or (thread and json.loads(r['args']).get('thread_id')==thread):task=r['task_id'];break
            if not task and args.get('action') in ('send','continue','create'):
                task=self.register(db,args.get('_task_spec') or {'goal':args.get('text') or args.get('title') or self.source(db,context).get('content') or backend+' '+str(args.get('action'))+' '+str(thread or '')},context)['task_id']
            if task:
                row=db.execute('SELECT * FROM loop_tasks WHERE id=?',(task,)).fetchone()
                if not row:raise ValueError('任务记录不存在')
                record=json.loads(row['data']);closed=record['status'] in ARCHIVED
                if closed and human.get('id') and args.get('action') in ('read','list','usage','artifact'):
                    # A fresh human query does not resume a withdrawn delegation.
                    context.pop('task_id',None);context.pop('task_version',None);context.pop('instruction_version',None)
                    return context
                if human.get('id') and args.get('action') in ('send','continue'):
                    threshold=max(record.get('source',{}).get('id') or 0,record.get('latest_instruction',{}).get('source_id') or 0,
                        record.get('monitor_cancel_source',{}).get('id') or 0)
                    if closed:
                        if args['action']!='continue' or human['id']<=threshold:raise ValueError('已交还任务只能由jg的新指令明确续接')
                        record['status']='active'
                    if human['id']>threshold:
                        record['latest_instruction']={'text':human['content'],'message_id':human['msg'],'source_id':human['id'],'kind':'supplement'}
                        record['instruction_version']=record.get('instruction_version',1)+1
                        self.save(db,row,record,human)
                        row=db.execute('SELECT * FROM loop_tasks WHERE id=?',(task,)).fetchone()
                if not args.get('thread_id') and not args.get('job_id') and args.get('action') not in ('create','list','usage','status'):
                    target=thread
                    for receipt in reversed(self.receipts(db,task)):
                        if receipt.get('backend')==backend:
                            target=json.loads(receipt.get('output') or '{}').get('thread_id') or json.loads(receipt['args']).get('thread_id') or target
                            if target:break
                    if target:args['thread_id']=target
                if json.loads(row['data'])['status'] in ARCHIVED and args.get('action') in ('send','continue','create'):
                    raise ValueError('该任务已收口；新目标先更新对应任务或登记新任务')
                context.update(task_id=task,task_version=row['version'],instruction_version=json.loads(row['data']).get('instruction_version',1))
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
                if negated_control(text):raise ValueError('本轮真人原话要求保留当前安排')
                if args.get('source_message_id') not in (None,source.get('msg')):raise ValueError('控制来源必须是本轮jg原话')
                # The native tool carries the model's interpreted operation;
                # authority comes from the real user message, not a magic phrase.
                if action=='enable' and user_switch(text) is False:raise ValueError('jg当前要求关闭，不能恢复')
                self.switch(db,action=='enable',text,source)
            elif action=='adjust':
                if not self.active(db,epoch):raise ValueError('自主活动已关闭，恢复由jg决定')
                interval=args.get('interval_seconds',state['interval_seconds']);due=state['next_due']
                if type(interval) is not int or not 120<=interval<=31622400:raise ValueError('自主活动间隔最少120秒')
                if 'interval_seconds' in args:due=time.time()+interval
                if 'delay_seconds' in args:
                    delay=args['delay_seconds']
                    if type(delay) is not int or delay<1:raise ValueError('delay_seconds需要正整数')
                    due=time.time()+delay
                if args.get('due_at'):
                    when=dt.datetime.fromisoformat(args['due_at'])
                    if when.tzinfo is None:raise ValueError('due_at需要时区')
                    due=when.timestamp()
                    if due<=time.time():raise ValueError('下次唤醒必须晚于现在')
                due=self.daily_window.constrain(due)
                db.execute('UPDATE loop_autonomy SET interval_seconds=?,next_due=? WHERE id=1',(interval,due))
            elif action=='skip_next':
                if not self.active(db,epoch):raise ValueError('自主活动已关闭，恢复由jg决定')
                db.execute('UPDATE loop_autonomy SET next_due=? WHERE id=1',(self.daily_window.constrain(max(time.time(),state['next_due'])+state['interval_seconds']),))
            elif action=='note':
                if not self.active(db,epoch):raise ValueError('自主活动已关闭')
                note=str(args.get('note',''))[:3000]
                db.execute('UPDATE loop_autonomy SET note=? WHERE id=1',(note,))
                db.execute('INSERT INTO loop_activity(epoch,kind,content,created) VALUES(?,?,?,?)',(state['epoch'],'continuity',note,time.time()))
            elif action!='status':raise ValueError('自主活动操作无效')
            return dict(db.execute('SELECT * FROM loop_autonomy WHERE id=1').fetchone(),daily_window=self.daily_window.view(),in_daily_window=self.daily_window.contains(time.time()),companion=self.r.companion.status(db)if hasattr(self.r,'companion')else None,recent=[dict(x) for x in db.execute('SELECT * FROM loop_activity WHERE epoch IS NOT NULL ORDER BY id DESC LIMIT 12')])

    def switch(self,db,enabled,text,source=None):
        state=db.execute('SELECT * FROM loop_autonomy WHERE id=1').fetchone()
        source=source or {};last=json.loads(self.r._get(db,'loop_autonomy_control','{}'))
        if source.get('id') and source['id']<(last.get('source_id') or 0):raise ValueError('自主活动已有jg更新的控制指令，旧轮不能覆盖')
        if source.get('id')==last.get('source_id') and bool(state['enabled'])==enabled:return
        epoch=state['epoch']+1
        db.execute('UPDATE loop_autonomy SET enabled=?,epoch=?,next_due=? WHERE id=1',(int(enabled),epoch,self.daily_window.next_slot(time.time(),state['interval_seconds'])))
        self.r._set(db,'loop_autonomy_control',json.dumps({'enabled':enabled,'epoch':epoch,'source_id':source.get('id'),
            'message_id':source.get('msg'),'text':text,'at':time.time()},ensure_ascii=False))
        db.execute('INSERT INTO loop_activity(epoch,kind,content,created) VALUES(?,?,?,?)',(epoch,'enabled' if enabled else 'disabled',text,time.time()))
        if not enabled:
            if hasattr(self.r,'companion'):self.r.companion.disarm(db,'owner_disabled')
            db.execute('UPDATE loop_tasks SET monitor=0 WHERE origin_epoch IS NOT NULL')
            db.execute("UPDATE silent_alarms SET status='cancelled' WHERE id IN (SELECT key FROM loop_bindings WHERE kind='alarm' AND epoch IS NOT NULL) AND status IN ('pending','running')")
            db.execute("UPDATE reminders SET status='cancelled' WHERE id IN (SELECT key FROM loop_bindings WHERE kind='reminder' AND epoch IS NOT NULL) AND status='pending'")
            db.execute('UPDATE loop_quota_watch SET enabled=0 WHERE origin_epoch IS NOT NULL')
            # Already dispatched executor jobs intentionally continue. Their late
            # callbacks remain stored and are filtered at admission below.

    def inbound(self,db,text,message,reply=''):
        source=self.source(db,{'message_id':message})
        binding=db.execute("SELECT task_id FROM loop_bindings WHERE kind='sent' AND key=?",(reply,)).fetchone() if reply else None
        explicit=re.search(r'\btask[:：-]([a-f0-9]{24})\b',text)
        task=explicit[1] if explicit else binding['task_id'] if binding else None
        handoff=task_handoff(text)
        if handoff and not task:
            candidates=db.execute("SELECT * FROM loop_tasks WHERE json_extract(data,'$.status')='active' AND json_extract(data,'$.source.id') IS NOT NULL ORDER BY created DESC").fetchall()
            if len(candidates)==1:task=candidates[0]['id']
        notices=[]
        if handoff and not task:notices.append('有多项或没有可明确关联的任务；交还目标需要明确task_id或回复对应任务消息，不能把这句广播到所有任务。')
        if handoff and task:
            row=db.execute('SELECT * FROM loop_tasks WHERE id=?',(task,)).fetchone()
            if row:
                result=self.stop_monitor(db,row,source,handoff)
                notices.append('任务已交还，撤销派生安排'+str(result['cancelled_schedules'])+'条；已提交执行保留，迟到回执只保存。')
        if all_background_off(text) or monitor_cancel(text):
            rows=db.execute('SELECT * FROM loop_tasks WHERE id=?',(task,)).fetchall() if task and not all_background_off(text) else db.execute("SELECT * FROM loop_tasks WHERE monitor=1 OR id IN (SELECT task_id FROM loop_bindings WHERE kind='alarm' AND key IN (SELECT CAST(id AS TEXT) FROM silent_alarms WHERE status IN ('pending','running')))").fetchall()
            for row in rows:self.stop_monitor(db,row,source)
            notices.append('已关闭'+str(len(rows))+'项任务的主巡检与派生复查；独立额度监听、已提交执行和普通提醒保留。')
        switch=user_switch(text)
        if switch is not None:
            self.switch(db,switch,text,source)
            notices.append('已开启自主活动，按当前间隔安排下一次机会。' if switch else '自主活动enabled=false，旧代次已失效，派生安排已撤销；已交执行端继续运行，恢复由jg决定。')
        if notices:return '[后台控制回执]\n'+'\n'.join(notices)
        if re.search(r'(?:自由|自主)(?:闹钟|时钟).{0,8}(?:停了|停掉|撤了|撤掉)',text):
            row=db.execute('SELECT * FROM loop_autonomy WHERE id=1').fetchone()
            db.execute('UPDATE loop_autonomy SET next_due=? WHERE id=1',(max(time.time(),row['next_due'])+row['interval_seconds'],))
            return '[自主活动控制回执]\n已跳过下一次机会；总开关及默认间隔不变。'
        if re.fullmatch(r'(活跃点|少醒点)[。！! ]*',text):
            row=db.execute('SELECT * FROM loop_autonomy WHERE id=1').fetchone();interval=max(120,min(86400,row['interval_seconds']*(.5 if '活跃' in text else 2)))
            db.execute('UPDATE loop_autonomy SET interval_seconds=?,next_due=? WHERE id=1',(int(interval),time.time()+interval))
            return '[自主活动控制回执]\n间隔已调整为'+str(int(interval))+'秒；总开关'+('开启。' if row['enabled'] else '仍关闭，恢复由jg决定。')
        if task:
            row=db.execute('SELECT * FROM loop_tasks WHERE id=?',(task,)).fetchone()
            if row:
                source=self.source(db,{'message_id':message})
                data=json.loads(row['data']);data['latest_instruction']={'text':text,'message_id':message,'source_id':source.get('id'),'created':source.get('created')};data['instruction_version']=data.get('instruction_version',1)+1
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
            old=db.execute('SELECT task_ids,status FROM loop_quota WHERE provider=? AND reset_at=?',(provider,reset)).fetchone()
            tasks=set(json.loads(old['task_ids']) if old else []);
            if task:tasks.add(task)
            db.execute('INSERT OR REPLACE INTO loop_quota VALUES(?,?,?,?)',(provider,reset,old['status'] if old else 'pending',json.dumps(sorted(tasks))))
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
                if not auto['enabled']:return None
                if not self.daily_window.contains(now):
                    db.execute('UPDATE loop_autonomy SET next_due=? WHERE id=1',(self.daily_window.next_slot(now,auto['interval_seconds'],inclusive=True),))
                    return None
                if auto['next_due']>now:return None
                kind='autonomy';epoch=auto['epoch']
                db.execute('UPDATE loop_autonomy SET next_due=? WHERE id=1',(self.daily_window.next_slot(now,auto['interval_seconds']),))
                extra='自主活动机会：沿兴趣阅读、探索、整理、聊天或安静结束。按需用autonomy调整下次唤醒或保存少量续记；安静一轮后仍有下次机会。持续任务用task_loop登记跟踪。续记：'+auto['note']
                if self.daily_window.view():extra+='\n当前自由唤醒窗口：'+json.dumps(self.daily_window.view(),ensure_ascii=False)+'；仅在窗口内提供新的自主机会，窗口结束不代表撤销总开关，已开始的工具自然完成。'
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
            if data.get('intent')=='schedule':active=active and self.scheduling_allowed(db,context)
            human=bool(self.source(db,context).get('id'))
            row=db.execute('SELECT * FROM loop_tasks WHERE id=?',(task,)).fetchone() if task else None
            result={'allowed':True,'active':active,'managed':bool(row or epoch is not None or context.get('session_key')=='agent:quota'),'task_id':task or '', 'epoch':epoch,'version':row['version'] if row else 0}
            result['receipt_recovery']=self.receipt_recovery(db,context)
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
                result.update(record=record,receipts=[receipt_summary(r) for r in self.receipts(db,task)],session_key='agent:task-'+task)
                result['context']='[任务上下文 task_id='+task+' version='+str(row['version'])+']\n'+json.dumps(record,ensure_ascii=False)+'\n真实派单与回执（完整记录按task_loop read取回）：\n'+json.dumps(result['receipts'][-12:],ensure_ascii=False)+'\n自动最终文本留内部。联系jg时显式发送；汇报开关开启可主动报告阻断、决策需求与完成结果。同一指令及要求版本复用原请求，新的补充明确提交；实际达标后记录小遗留并结束巡检，额度守候独立保留。'
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
                'archived':bool(row and json.loads(row['data'])['status'] in ARCHIVED)}
