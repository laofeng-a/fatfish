"""Small persistent reminder scheduler; sends explicit requests without a model."""
import datetime as dt,hashlib,json,threading,time
UTC8=dt.timezone(dt.timedelta(hours=8))

class Reminders:
    def __init__(self,runtime):
        self.r=runtime
        self.stop=threading.Event()
        with runtime.db() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS reminders(id INTEGER PRIMARY KEY,chat TEXT NOT NULL,text TEXT NOT NULL,due REAL NOT NULL,repeat TEXT NOT NULL,status TEXT NOT NULL,create_key TEXT UNIQUE,created REAL,updated REAL,last_message_id TEXT,last_error TEXT,next_attempt REAL DEFAULT 0,attempts INTEGER DEFAULT 0);
            CREATE INDEX IF NOT EXISTS reminders_due ON reminders(status,due,next_attempt);
            ''')
    def call(self,args,context,chat):
        action=args.get('action');now=time.time()
        if action=='list':
            with self.r.db() as db:
                rows=db.execute("SELECT * FROM reminders WHERE status NOT IN ('sent','cancelled') ORDER BY due LIMIT 50").fetchall()
            return {'now':self.date(now),'timezone':'Asia/Shanghai','reminders':[self.public(row) for row in rows]}
        if action not in ('create','update','cancel'):raise ValueError('action需要create、list、update或cancel')
        with self.r.db() as db:
            if action!='create':
                identifier=args.get('id')
                if type(identifier) is not int or identifier<1:raise ValueError('需要真实提醒编号')
                row=db.execute('SELECT * FROM reminders WHERE id=?',(identifier,)).fetchone()
                if not row:raise ValueError('没有找到这条提醒')
                if row['status']=='sending':raise ValueError('这条提醒正在发送，稍后查询送达结果')
                if action=='cancel':
                    db.execute("UPDATE reminders SET status='cancelled',updated=? WHERE id=?",(now,identifier))
                    return {'status':'cancelled','id':identifier}
            text=args.get('text',row['text'] if action=='update' else None)
            if not isinstance(text,str) or not 1<=len(text.strip())<=1000:raise ValueError('需要明确的提醒内容')
            target=args.get('chat_id',row['chat'] if action=='update' else chat)
            if not isinstance(target,str) or not target.lstrip('-').isdigit():raise ValueError('需要真实Telegram会话编号')
            repeat=args.get('repeat',row['repeat'] if action=='update' else 'none')
            if repeat not in ('none','daily','weekly'):raise ValueError('repeat需要none、daily或weekly')
            has_time='due_at' in args or 'delay_seconds' in args
            if has_time:due=self.parse_due(args,now)
            elif action=='update':due=row['due']
            else:raise ValueError('需要due_at或delay_seconds，时间含糊时先问jg')
            if action=='create':
                source=str(context.get('message_id') or context.get('session_key') or int(now//300))
                key=hashlib.sha256(json.dumps([chat,source,target,text.strip(),repeat,args.get('due_at'),args.get('delay_seconds')],ensure_ascii=False).encode()).hexdigest()
                old=db.execute('SELECT * FROM reminders WHERE create_key=?',(key,)).fetchone()
                if old:return dict(self.public(old),already_created=True)
                identifier=db.execute("INSERT INTO reminders(chat,text,due,repeat,status,create_key,created,updated) VALUES(?,?,?,?,'pending',?,?,?)",(target,text.strip(),due,repeat,key,now,now)).lastrowid
            else:
                if row['status'] in ('cancelled','sent','uncertain') and not has_time:raise ValueError('重新启用已结束或发送结果不明的提醒，需要新的明确时间')
                db.execute("UPDATE reminders SET chat=?,text=?,due=?,repeat=?,status='pending',updated=?,last_error=NULL,next_attempt=0,attempts=0 WHERE id=?",(target,text.strip(),due,repeat,now,identifier))
            row=db.execute('SELECT * FROM reminders WHERE id=?',(identifier,)).fetchone()
        return self.public(row)
    @staticmethod
    def date(value):return dt.datetime.fromtimestamp(value,UTC8).isoformat(timespec='seconds')
    @classmethod
    def public(cls,row):
        return {'id':row['id'],'chat_id':row['chat'],'text':row['text'],'due_at':cls.date(row['due']),'repeat':row['repeat'],'status':row['status'],'last_message_id':row['last_message_id'],'last_error':row['last_error']}
    @staticmethod
    def parse_due(args,now):
        if 'due_at' in args and 'delay_seconds' in args:raise ValueError('绝对时间和相对时间只选一种')
        if 'delay_seconds' in args:
            delay=args['delay_seconds']
            if type(delay) is not int or not 1<=delay<=366*86400:raise ValueError('delay_seconds需要正整数秒数，最长一年')
            return now+delay
        value=args.get('due_at')
        if not isinstance(value,str):raise ValueError('due_at需要带时区的ISO时间')
        try:parsed=dt.datetime.fromisoformat(value.replace('Z','+00:00'))
        except ValueError:raise ValueError('时间格式不合法') from None
        if parsed.tzinfo is None:raise ValueError('提醒时间需要时区，例如+08:00')
        due=parsed.timestamp()
        if due<=now:raise ValueError('提醒时间已经过去，需要确认新的时间')
        return due
    def recover(self):
        with self.r.db() as db:
            db.execute("UPDATE reminders SET status='uncertain',last_error='服务重启前已开始发送，未取得回执；请核对后决定是否重设',updated=? WHERE status='sending'",(time.time(),))
    def tick(self,now=None):
        now=time.time() if now is None else now
        with self.r.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute("SELECT * FROM reminders WHERE status='pending' AND due<=? AND next_attempt<=? ORDER BY due LIMIT 1",(now,now)).fetchone()
            if not row:return False
            db.execute("UPDATE reminders SET status='sending',updated=?,attempts=attempts+1 WHERE id=?",(now,row['id']))
        late=now-row['due']>300
        text=('⏰ 补提醒（原定'+self.date(row['due'])+'）：' if late else '⏰ ')+row['text']
        try:
            result=self.r.telegram('sendMessage',{'chat_id':row['chat'],'text':text})
            message=str(result['message_id'])
        except Exception as error:
            rejected=getattr(error,'rejected',False)
            retry=getattr(error,'retry_after',None)
            code=getattr(error,'code',0)
            retryable=rejected and (code==429 or code>=500)
            state='pending' if retryable else ('failed' if rejected else 'uncertain')
            description=('Telegram已明确拒绝发送' if rejected else '未获得发送回执，未自动重发')
            wait=retry if type(retry) is int and retry>0 else min(300,2**min(row['attempts']+2,8))
            with self.r.db() as db:db.execute('UPDATE reminders SET status=?,last_error=?,next_attempt=?,updated=? WHERE id=?',(state,description,now+wait,now,row['id']))
            return True
        with self.r.db() as db:
            due=row['due'];state='sent'
            if row['repeat']!='none':
                interval=86400 if row['repeat']=='daily' else 7*86400
                due+=max(1,int((now-due)//interval)+1)*interval;state='pending'
            db.execute('UPDATE reminders SET status=?,due=?,last_message_id=?,last_error=NULL,next_attempt=0,attempts=0,updated=? WHERE id=?',(state,due,message,now,row['id']))
        self.r.event(row['chat'],'assistant',text,msg=message)
        return True
    def start(self):
        self.recover()
        def run():
            while not self.stop.wait(1):
                try:self.tick()
                except Exception:pass  # Persisted pending jobs survive a transient local error.
        threading.Thread(target=run,name='fatfish-reminders',daemon=True).start()
