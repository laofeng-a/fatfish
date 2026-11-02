"""Daily private diary maintenance, independent of social sharing and alarms.

The scheduler owns dates and receipts. A model's final text is never a save
receipt. Existing unverified diaries are marked for review without rewriting.
"""
import datetime as dt
import json
import time
from memory_notes import UTC8
from codex_bridge import redact


class DiaryMaintenance:
    HOUR=23
    MINUTE=30
    RETRIES=(15*60,30*60,60*60)
    LEASE_SECONDS=10*60
    READ_TOOLS=frozenset(('history_find','history_expand','memory_note_read'))

    def __init__(self,runtime):
        self.r=runtime
        with runtime.db() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS diary_days(
                day TEXT PRIMARY KEY,path TEXT NOT NULL,status TEXT NOT NULL,
                task_kind TEXT NOT NULL DEFAULT 'daily',next_due REAL,attempts INTEGER NOT NULL DEFAULT 0,
                created REAL NOT NULL,updated REAL NOT NULL,last_started REAL,last_finished REAL,
                revision TEXT,bytes INTEGER NOT NULL DEFAULT 0,sources TEXT NOT NULL DEFAULT '[]',
                last_error TEXT NOT NULL DEFAULT '');
            CREATE INDEX IF NOT EXISTS diary_days_due ON diary_days(status,next_due);
            CREATE TABLE IF NOT EXISTS diary_runs(
                wake_id TEXT PRIMARY KEY,day TEXT NOT NULL,started REAL NOT NULL,finished REAL,
                outcome TEXT,summary TEXT NOT NULL DEFAULT '');
            ''')
            # These are known historical backfills. Register the need to verify
            # them, but never replace their existing prose at startup.
            now=time.time()
            for day in ('2026-10-22','2026-10-23'):
                path=self.name(day)
                if self.r.notes.path(path).exists():
                    db.execute("INSERT OR IGNORE INTO diary_days(day,path,status,task_kind,created,updated,last_error) VALUES(?,?,'review','review',?,?,?)",
                               (day,path,now,now,'已有历史追记，待核实同日真实来源；原文保留。'))

    @staticmethod
    def name(day):
        date=dt.date.fromisoformat(day)
        return date.strftime('%Y%m/%Y%m%d.md')

    @staticmethod
    def bounds(day):
        start=dt.datetime.combine(dt.date.fromisoformat(day),dt.time(),UTC8)
        return start.timestamp(),(start+dt.timedelta(days=1)).timestamp()

    def _sources(self,db,day,ids):
        if not isinstance(ids,list) or not 1<=len(ids)<=8 or any(type(x) is not int for x in ids):
            return False
        start,end=self.bounds(day)
        for identifier in ids:
            if not db.execute('SELECT 1 FROM events WHERE id=? AND chat=? AND created>=? AND created<?',
                              (identifier,self.r.owner,start,end)).fetchone():return False
        return True

    def _verify(self,db,row):
        try:
            path=self.r.notes.path(row['path'])
            text=self.r.notes.content(path)
        except (OSError,ValueError):return None,'目标日记无法安全读取'
        if not path.exists() or not text.strip():return None,'目标日记不存在或为空白'
        revision=self.r.notes.revision(text)
        version=db.execute('SELECT sources FROM note_versions WHERE path=? AND new_revision=? ORDER BY id DESC LIMIT 1',
                           (row['path'],revision)).fetchone()
        if not version:return None,'当前文件没有可匹配的写入版本'
        try:ids=json.loads(version['sources'])
        except (TypeError,ValueError):return None,'日记来源格式不合法'
        if not self._sources(db,row['day'],ids):return None,'日记缺少可核实的同日私聊或阅读来源'
        return {'revision':revision,'bytes':len(text.encode('utf-8')),'sources':ids},''

    def due(self,db,now):
        """Return one eligible row; caller holds wake's BEGIN IMMEDIATE lock."""
        local=dt.datetime.fromtimestamp(now,UTC8)
        latest=local.date() if (local.hour,local.minute)>=(self.HOUR,self.MINUTE) else local.date()-dt.timedelta(days=1)
        first=latest-dt.timedelta(days=2)
        for offset in range(3):
            day=(first+dt.timedelta(days=offset)).isoformat()
            path=self.name(day)
            db.execute("INSERT OR IGNORE INTO diary_days(day,path,status,next_due,created,updated) VALUES(?,?,'pending',?,?,?)",
                       (day,path,now,now,now))
        rows=db.execute("SELECT * FROM diary_days WHERE day>=? AND day<=? AND status IN ('pending','retry') AND next_due<=? ORDER BY day LIMIT 3",
                        (first.isoformat(),latest.isoformat(),now)).fetchall()
        for row in rows:
            # Existing valid writes need no model call at all.
            verified,error=self._verify(db,row)
            if verified:
                db.execute("UPDATE diary_days SET status='completed',updated=?,last_finished=?,revision=?,bytes=?,sources=?,last_error='' WHERE day=?",
                           (now,now,verified['revision'],verified['bytes'],json.dumps(verified['sources']),row['day']))
                continue
            try:
                target=self.r.notes.path(row['path']);exists=target.exists()
                existing=self.r.notes.content(target) if exists else ''
            except (OSError,ValueError):exists=True;existing='[unreadable]'
            if exists and existing.strip() and row['task_kind']!='review':
                # Reconcile an earlier spontaneous note during the daily pass.
                # Keep its prose as input and attach evidence through a real save.
                db.execute("UPDATE diary_days SET task_kind='review',updated=?,last_error=? WHERE day=?",
                           (now,error+'；保留原文并核实来源。',row['day']))
                row=db.execute('SELECT * FROM diary_days WHERE day=?',(row['day'],)).fetchone()
            start,end=self.bounds(row['day'])
            has_events=db.execute('SELECT 1 FROM events WHERE chat=? AND created>=? AND created<? LIMIT 1',
                                  (self.r.owner,start,end)).fetchone()
            if not has_events:
                db.execute("UPDATE diary_days SET status='skipped',next_due=NULL,updated=?,last_finished=?,last_error=? WHERE day=?",
                           (now,now,'当日没有可核实的聊天或阅读记录；不生成虚构经历。',row['day']))
                continue
            return row
        return None

    def claim(self,db,wake_id,now):
        if db.execute('SELECT 1 FROM diary_runs WHERE wake_id=?',(wake_id,)).fetchone():return None
        if db.execute('SELECT 1 FROM diary_runs WHERE finished IS NULL').fetchone():return None
        row=self.due(db,now)
        if row is None:return None
        db.execute("UPDATE diary_days SET status='running',attempts=attempts+1,last_started=?,updated=? WHERE day=?",
                   (now,now,row['day']))
        db.execute('INSERT INTO diary_runs(wake_id,day,started) VALUES(?,?,?)',(wake_id,row['day'],now))
        return db.execute('SELECT * FROM diary_days WHERE day=?',(row['day'],)).fetchone()

    def request_review(self,day,now=None):
        """Explicit repair entry point; does not change existing file contents."""
        now=time.time() if now is None else now
        if dt.date.fromisoformat(day)>dt.datetime.fromtimestamp(now,UTC8).date():raise ValueError('不能核实未来日记')
        with self.r.db() as db:
            db.execute('BEGIN IMMEDIATE')
            old=db.execute('SELECT status FROM diary_days WHERE day=?',(day,)).fetchone()
            if old and old['status']=='running':raise ValueError('这天的日记核实正在进行')
            db.execute("INSERT INTO diary_days(day,path,status,task_kind,next_due,created,updated) VALUES(?,?,'pending','review',?,?,?) ON CONFLICT(day) DO UPDATE SET status='pending',task_kind='review',next_due=excluded.next_due,attempts=0,updated=excluded.updated,last_error=''",
                       (day,self.name(day),now,now,now))

    def finish(self,db,wake_id,outcome,summary,now=None):
        now=time.time() if now is None else now
        run=db.execute('SELECT * FROM diary_runs WHERE wake_id=?',(wake_id,)).fetchone()
        if not run or run['finished'] is not None:return False
        row=db.execute('SELECT * FROM diary_days WHERE day=?',(run['day'],)).fetchone()
        verified,error=self._verify(db,row)
        completed=outcome=='completed' and verified is not None
        clean=redact(str(summary))[:1200]
        if completed:
            db.execute("UPDATE diary_days SET status='completed',next_due=NULL,updated=?,last_finished=?,revision=?,bytes=?,sources=?,last_error='' WHERE day=?",
                       (now,now,verified['revision'],verified['bytes'],json.dumps(verified['sources']),row['day']))
        else:
            error=error or '模型或维护轮未正常完成；不能仅凭答复文字记为写入成功'
            attempts=row['attempts'];retry=attempts<=len(self.RETRIES)
            due=now+self.RETRIES[attempts-1] if retry else None
            db.execute("UPDATE diary_days SET status=?,next_due=?,updated=?,last_finished=?,last_error=? WHERE day=?",
                       ('retry' if retry else 'failed',due,now,now,redact(error)[:600],row['day']))
        db.execute('UPDATE diary_runs SET finished=?,outcome=?,summary=? WHERE wake_id=?',
                   (now,'completed' if completed else 'error',(clean+'\n'+error).strip()[:1800],wake_id))
        db.execute('DELETE FROM diary_runs WHERE finished IS NOT NULL AND finished<?',(now-30*86400,))
        return True

    def recover_stale(self,db,now):
        for run in db.execute('SELECT r.wake_id FROM diary_runs r LEFT JOIN wakes w ON w.id=r.wake_id WHERE r.finished IS NULL AND COALESCE(w.heartbeat_at,r.started)<?',(now-self.LEASE_SECONDS,)).fetchall():
            self.finish(db,run['wake_id'],'error','日记维护超时或服务中断；等待有上限的退避重试。',now)
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='wakes'").fetchone():
                db.execute('UPDATE wakes SET finished=1 WHERE id=?',(run['wake_id'],))

    @staticmethod
    def is_diary(db,wake_id):
        return bool(wake_id and db.execute('SELECT 1 FROM diary_runs WHERE wake_id=?',(wake_id,)).fetchone())

    def tool_allowed(self,db,wake_id,name,args=None):
        run=db.execute('SELECT * FROM diary_runs WHERE wake_id=? AND finished IS NULL',(wake_id,)).fetchone()
        if not run:return False
        args=args or {}
        if name in self.READ_TOOLS:
            return not args.get('chat_id') or str(args['chat_id'])==self.r.owner
        if name=='memory_note_write':
            return args.get('path')==self.name(run['day'])
        return False

    def validate_write(self,args,context,chat):
        """Call only for an authenticated diary wake, before MemoryNotes.write."""
        message=str(context.get('message_id',''))
        wake_id=message.removeprefix('heartbeat:')
        with self.r.db() as db:
            run=db.execute('SELECT * FROM diary_runs WHERE wake_id=? AND finished IS NULL',(wake_id,)).fetchone()
            if not run or chat!=self.r.owner:raise ValueError('日记维护轮次不存在或已结束')
            if args.get('path')!=self.name(run['day']):raise ValueError('日记维护只修改本次指定日期文件')
            if not self._sources(db,run['day'],args.get('source_ids')):
                raise ValueError('日记维护需要1至8条同日真实私聊或阅读来源编号')
            if dt.date.fromisoformat(run['day'])<dt.datetime.now(UTC8).date() and '追记' not in str(args.get('content','')):
                raise ValueError('事后补写需要注明追记及补写日期')

    def prompt(self,row):
        start,end=self.bounds(row['day'])
        after=dt.datetime.fromtimestamp(start,UTC8).isoformat()
        before=dt.datetime.fromtimestamp(end,UTC8).isoformat()
        return ('[日记维护]\n日期：'+row['day']+'；目标：'+row['path']+'\n'
                '历史范围：after='+after+'，before='+before+'\n'
                '回顾当天真实聊天与阅读，记下值得留下的经历、当时表达的感想和想接着聊的事。'
                '用mcp_reader_history_find与history_expand核对原文。动作成果依据实际回执；'
                '证据有限时写成“当时对话提到”，说明把握程度。'
                '先memory_note_read读取目标文件及revision，保留已有真实内容，'
                '再用memory_note_write提交完整日记、最新expected_revision及1至8条同日source_ids。'
                '事后补写注明“事后追记”和补写日期，现在形成的看法标成“现在回看”。'
                '保存后读回，核对exists、正文和revision；遇到冲突，重读后合并。'
                '结束时留下简短的内部保存结果。')
