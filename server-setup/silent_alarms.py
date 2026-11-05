"""Persistent, coalescing model wakeups; never sends a Telegram reminder."""
import hashlib
import json
import time
from reminders import Reminders
from codex_bridge import redact

MIN_INTERVAL_SECONDS=120
SCHEDULER_RESOLUTION_SECONDS=60

class SilentAlarms:
    def __init__(self,runtime):
        self.r=runtime
        with runtime.db() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS silent_alarms(
                id INTEGER PRIMARY KEY,chat TEXT NOT NULL,task TEXT NOT NULL,due REAL NOT NULL,
                interval_seconds INTEGER NOT NULL DEFAULT 0,status TEXT NOT NULL,create_key TEXT UNIQUE,
                created REAL NOT NULL,updated REAL NOT NULL,last_started REAL,last_finished REAL,
                last_outcome TEXT,last_result TEXT NOT NULL DEFAULT '',run_count INTEGER NOT NULL DEFAULT 0);
            CREATE INDEX IF NOT EXISTS silent_alarms_due ON silent_alarms(status,due);
            CREATE TABLE IF NOT EXISTS silent_alarm_runs(
                wake_id TEXT PRIMARY KEY,alarm_id INTEGER NOT NULL,started REAL NOT NULL,finished REAL,
                outcome TEXT,summary TEXT NOT NULL DEFAULT '');
            ''')
            if 'purpose' not in {r[1] for r in db.execute('PRAGMA table_info(silent_alarms)')}:
                db.execute("ALTER TABLE silent_alarms ADD COLUMN purpose TEXT NOT NULL DEFAULT ''")

    @staticmethod
    def public(row):
        return {'id':row['id'],'task':row['task'],'due_at':Reminders.date(row['due']),
                'interval_seconds':row['interval_seconds'],'purpose':row['purpose'],'status':row['status'],
                'last_outcome':row['last_outcome'],'last_result':row['last_result'],
                'last_started_at':Reminders.date(row['last_started']) if row['last_started'] else None,
                'last_finished_at':Reminders.date(row['last_finished']) if row['last_finished'] else None,
                'run_count':row['run_count'],'delivery':'silent','scheduler_resolution_seconds':SCHEDULER_RESOLUTION_SECONDS}

    def call(self,args,context,chat):
        chat=self.r.owner
        action=args.get('action');now=time.time()
        if action=='list':
            with self.r.db() as db:
                rows=db.execute('SELECT * FROM silent_alarms WHERE chat=? ORDER BY updated DESC LIMIT 30',(chat,)).fetchall()
            return {'now':Reminders.date(now),'alarms':[self.public(row) for row in rows],
                    'note':'到期只唤醒后台模型，绝不直接发提醒；未运行的过期周期合并一次。'}
        if action not in ('create','update','cancel'):raise ValueError('action需要create/list/update/cancel')
        with self.r.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row=None
            if action!='create':
                if type(args.get('id')) is not int:raise ValueError('需要真实静默闹钟编号')
                row=db.execute('SELECT * FROM silent_alarms WHERE id=? AND chat=?',(args['id'],chat)).fetchone()
                if not row:raise ValueError('没有这条静默闹钟')
                if action=='cancel':
                    db.execute("UPDATE silent_alarms SET status='cancelled',updated=? WHERE id=?",(now,row['id']))
                    return {'id':row['id'],'status':'cancelled','delivery':'silent'}
            task=args.get('task',row['task'] if row else '')
            if not isinstance(task,str) or not 1<=len(task.strip())<=2000:raise ValueError('需要1至2000字的后台检查任务')
            interval=args.get('interval_seconds',row['interval_seconds'] if row else 0)
            if type(interval) is not int or (interval!=0 and not MIN_INTERVAL_SECONDS<=interval<=366*86400):
                raise ValueError('interval_seconds为0表示一次；重复间隔最短120秒，最长一年')
            interval,purpose=self.r.pulses.schedule(task,interval,args.get('purpose'),row)
            if row is None and self.r.pulses.enabled and purpose in ('chat','quota_watch','quota_reset'):
                row=db.execute("SELECT * FROM silent_alarms WHERE chat=? AND purpose=? AND status IN ('pending','running') ORDER BY id LIMIT 1",(chat,purpose)).fetchone()
            has_time='due_at' in args or 'delay_seconds' in args
            if self.r.pulses.enabled and purpose=='quota_reset' and not has_time and row is None:
                raise ValueError('等额度重置需提供真实重置时间due_at或delay_seconds，只执行一次')
            if has_time:due=Reminders.parse_due(args,now)
            elif row and row['status']=='pending':due=row['due']
            elif row and row['status']=='running' and self.r.pulses.enabled:due=now+(interval or 300)
            elif row:raise ValueError('重新启用已结束的静默闹钟，需要新的时间')
            else:due=now+(interval or 300)
            if row:
                identifier=row['id']
                db.execute("UPDATE silent_alarms SET task=?,due=?,interval_seconds=?,status='pending',updated=? WHERE id=?",(task.strip(),due,interval,now,identifier))
            else:
                key=hashlib.sha256(json.dumps([chat,str(context.get('message_id') or context.get('session_key') or int(now//300)),task.strip(),interval,args.get('due_at'),args.get('delay_seconds')],ensure_ascii=False).encode()).hexdigest()
                old=db.execute('SELECT * FROM silent_alarms WHERE create_key=?',(key,)).fetchone()
                if old:return dict(self.public(old),already_created=True)
                identifier=db.execute("INSERT INTO silent_alarms(chat,task,due,interval_seconds,status,create_key,created,updated) VALUES(?,?,?,?,'pending',?,?,?)",(chat,task.strip(),due,interval,key,now,now)).lastrowid
            db.execute('UPDATE silent_alarms SET purpose=? WHERE id=?',(purpose,identifier))
            if hasattr(self.r,'loops'):
                task_id,epoch=self.r.loops.resolve(db,context)
                if task_id or epoch is not None:db.execute('INSERT OR REPLACE INTO loop_bindings VALUES(?,?,?,?)',('alarm',str(identifier),task_id,epoch))
            return self.public(db.execute('SELECT * FROM silent_alarms WHERE id=?',(identifier,)).fetchone())

    def claim(self,db,wake_id,now):
        # Called inside wake's transaction: at most one model task is in flight.
        rows=db.execute("SELECT * FROM silent_alarms WHERE chat=? AND status='pending' AND due<=? ORDER BY due,id",(self.r.owner,now)).fetchall()
        row=None
        for candidate in rows:
            if hasattr(self.r,'loops'):
                binding=db.execute("SELECT epoch FROM loop_bindings WHERE kind='alarm' AND key=?",(str(candidate['id']),)).fetchone()
                if binding and not self.r.loops.active(db,binding['epoch']):
                    db.execute("UPDATE silent_alarms SET status='cancelled' WHERE id=?",(candidate['id'],));continue
            reason=self.r.pulses.stop_reason(db,candidate,now)
            if reason:
                db.execute("UPDATE silent_alarms SET status='cancelled',updated=?,last_result=? WHERE id=?",(now,reason,candidate['id']))
                continue
            row=candidate;break
        if row is None:return None
        db.execute("UPDATE silent_alarms SET status='running',last_started=?,updated=?,run_count=run_count+1 WHERE id=?",(now,now,row['id']))
        db.execute('INSERT INTO silent_alarm_runs(wake_id,alarm_id,started) VALUES(?,?,?)',(wake_id,row['id'],now))
        return row

    def finish(self,db,wake_id,outcome,summary,now=None):
        now=time.time() if now is None else now
        run=db.execute('SELECT * FROM silent_alarm_runs WHERE wake_id=?',(wake_id,)).fetchone()
        if not run or run['finished'] is not None:return False
        row=db.execute('SELECT * FROM silent_alarms WHERE id=?',(run['alarm_id'],)).fetchone()
        clean=redact(str(summary))[:2000];outcome='completed' if outcome=='completed' else 'error'
        if outcome!='completed' and db.execute("SELECT 1 FROM sqlite_master WHERE name='codex_jobs'").fetchone():
            jobs=db.execute('SELECT id,action,state,args FROM codex_jobs WHERE source LIKE ? ORDER BY created DESC LIMIT 5',('%'+wake_id+'%',)).fetchall()
            if jobs:clean=(clean+'\n已登记Codex请求，接续时沿用job_id：'+json.dumps([{'job_id':j['id'],'action':j['action'],'state':j['state'],'thread_id':json.loads(j['args'] or '{}').get('thread_id')} for j in jobs],ensure_ascii=False))[:2000]
        db.execute('UPDATE silent_alarm_runs SET finished=?,outcome=?,summary=? WHERE wake_id=?',(now,outcome,clean,wake_id))
        # Keep the latest result on the alarm; detailed finished runs need only one week.
        db.execute('DELETE FROM silent_alarm_runs WHERE finished IS NOT NULL AND finished<?',(now-7*86400,))
        due=row['due'];state=row['status']
        if state in ('running','pending') and self.r.pulses.ended_without_share(db,row,wake_id,outcome):
            state='cancelled'
        if state=='running' and self.r.pulses.enabled and row['purpose']=='quota_reset' and outcome!='completed':
            state='error'  # A one-off reset is not a new five-minute retry chain.
        if state=='running':
            interval=row['interval_seconds']
            if interval:
                due+=max(1,int((now-due)//interval)+1)*interval;state='pending'
            elif outcome=='completed':state='completed'
            else:state='pending';due=now+300
        db.execute('UPDATE silent_alarms SET status=?,due=?,updated=?,last_finished=?,last_outcome=?,last_result=? WHERE id=?',(state,due,now,now,outcome,clean,row['id']))
        return True

    @staticmethod
    def is_silent(db,wake_id):
        return bool(wake_id and db.execute('SELECT 1 FROM silent_alarm_runs WHERE wake_id=?',(wake_id,)).fetchone())

    def recover_stale(self,db,now):
        for run in db.execute('SELECT r.wake_id FROM silent_alarm_runs r LEFT JOIN wakes w ON w.id=r.wake_id WHERE r.finished IS NULL AND COALESCE(w.heartbeat_at,r.started)<?',(now-300,)).fetchall():
            self.finish(db,run['wake_id'],'error','后台进程连接中断；已执行动作和Codex请求保留在回执中，可从已有进展接续。',now)
            db.execute('UPDATE wakes SET finished=1 WHERE id=?',(run['wake_id'],))

    @staticmethod
    def prompt(row):
        return ('[静默后台行动]\n'
                '本轮按jg最新意愿和当前HEARTBEAT执行；上次结果只作历史，不覆盖新安排。'
                '检查类任务记录检查结果；推进类任务在条件满足后直接执行，例如额度恢复就用Codex继续原任务。'
                '沿用jg已有授权，工具能力与日常聊天相同，可提交、续接和停止Codex，更新待办或安排下次唤醒。'
                '静默表示默认把结果留在内部记录；有值得联系jg的内容时自行判断。'
                '接续已有job和待办进展，以真实执行结果记录本轮进展。\n'
                '任务：'+row['task']+'\n上次结果：'+((row['last_result'][:400] if row['purpose'] else row['last_result']) or '暂无；本次建立基线。'))
