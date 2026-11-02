"""Incremental review and corrections, using the existing memory/note tools."""
import datetime as dt
import json
import os
from pathlib import Path
import tempfile
import time

UTC8=dt.timezone(dt.timedelta(hours=8))
EXPRESSION_PREFIX='表达经验（模型归纳）：'


def revised_reference(text,previous,current):
    if not previous or previous==current:return text
    if not current:return text.replace(previous,'')
    return current.join(part.replace(previous,current) for part in text.split(current))


class MemoryLearning:
    def __init__(self,runtime):
        self.r=runtime
        with runtime.db() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS memory_review_cursor(chat TEXT PRIMARY KEY,event_id INTEGER NOT NULL,updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS memory_review_batches(wake_id TEXT PRIMARY KEY,chat TEXT,after_id INTEGER,through_id INTEGER,finished REAL);
            CREATE TABLE IF NOT EXISTS memory_correction_reviews(memory_id INTEGER PRIMARY KEY,chat TEXT,reviewed REAL);
            ''')
            latest=db.execute("SELECT COALESCE(MAX(id),0) FROM events WHERE chat=? AND kind IN ('user','assistant')",(runtime.owner,)).fetchone()[0]
            # Existing history has its own diary/notes. Start with new exchanges,
            # rather than silently replaying years of material after deployment.
            db.execute('INSERT OR IGNORE INTO memory_review_cursor VALUES(?,?,?)',(runtime.owner,latest,time.time()))
            self.export_corrections(db)

    def review_window(self,db,chat):
        row=db.execute('SELECT event_id FROM memory_review_cursor WHERE chat=?',(chat,)).fetchone()
        after=row[0] if row else 0
        upper,count=db.execute("SELECT COALESCE(MAX(id),?),COUNT(*) FROM events WHERE chat=? AND kind IN ('user','assistant') AND id>?",(after,chat,after)).fetchone()
        previous=db.execute('SELECT created FROM events WHERE id=? AND chat=?',(after,chat)).fetchone()
        return {'after_id':after,'through_id':upper,'new_exchanges':count,
                'after':dt.datetime.fromtimestamp(previous[0],UTC8).isoformat() if previous else None}

    def correction_rows(self,db,chat,pending=False):
        return [dict(row) for row in db.execute('''SELECT n.id,n.source_id,n.category,n.content AS current,o.content AS previous,
               n.created,r.reviewed FROM memory_correction_reviews r JOIN memories n ON n.id=r.memory_id
               JOIN memories o ON o.id=n.replaces WHERE r.chat=? AND n.active=1
               AND (n.expires IS NULL OR n.expires>?) '''+(' AND r.reviewed IS NULL ' if pending else '')+
               ' ORDER BY n.id DESC LIMIT 8',(chat,time.time()))]

    def record_replacement(self,db,identifier,chat):
        db.execute('INSERT OR IGNORE INTO memory_correction_reviews(memory_id,chat) VALUES(?,?)',(identifier,chat))

    def export_corrections(self,db):
        rows=[dict(row) for row in db.execute('SELECT id,content,replaces,active,expires,category,source_id FROM memories WHERE chat=? ORDER BY id',(self.r.owner,))]
        children={row['replaces']:row for row in rows if row['replaces']}
        corrections=[]
        for old in rows:
            if old['id'] not in children:continue
            current=children[old['id']];seen={old['id']}
            while current['id'] in children and current['id'] not in seen:
                seen.add(current['id']);current=children[current['id']]
            valid=current['active'] and (current['expires'] is None or current['expires']>time.time())
            corrections.append({'old':old['content'],'new':current['content'] if valid else '',
                'new_id':current['id'],'source_id':current['source_id'],'category':current['category'],'expires':current['expires']})
        corrections=sorted(corrections,key=lambda row:row['new_id'],reverse=True)[:32]
        payload=json.dumps({'chat':self.r.owner,'corrections':corrections},ensure_ascii=False,sort_keys=True)+'\n'
        target=self.r.directory/'corrections.json'
        if target.exists() and target.read_text(encoding='utf-8')==payload:return
        fd,temporary=tempfile.mkstemp(prefix='.corrections-',dir=self.r.directory)
        try:
            with os.fdopen(fd,'w',encoding='utf-8') as stream:stream.write(payload);stream.flush();os.fsync(stream.fileno())
            os.chmod(temporary,0o640);os.replace(temporary,target)
        finally:
            if os.path.exists(temporary):os.unlink(temporary)

    def reconcile_notes(self,identifier,chat,context):
        # Exact references can be corrected immediately, with real revisions.
        # Paraphrases are left for the normal background model to review.
        if chat!=self.r.owner:return []
        with self.r.db() as db:
            row=db.execute('''SELECT n.content,n.source_id,o.content AS previous FROM memories n
                JOIN memories o ON o.id=n.replaces WHERE n.id=? AND n.chat=? AND n.active=1''',(identifier,chat)).fetchone()
            if not row:return []
            row=dict(row)
        changed=[]
        # USER is the current preference projection. Diaries, MEMORY prose and
        # personality archives may describe a true past state; the background
        # review reconciles those with their dates instead of rewriting history.
        for name in ('USER.md',):
            current=self.r.notes.read(name)
            if not current['exists'] or row['previous'] not in current['content']:continue
            content=revised_reference(current['content'],row['previous'],row['content'])
            try:
                result=self.r.notes.write({'path':name,'content':content,'expected_revision':current['revision'],
                    'source_ids':[row['source_id']]},context,chat)
                if result.get('readback_verified'):changed.append(name)
            except (OSError,ValueError):
                continue
        return changed

    def correction_context(self,db,chat):
        rows=self.correction_rows(db,chat,pending=True)[:3]
        if not rows:return ''
        return ('[最新记忆纠正]\n以下是有来源的新记录，接话以当前记录为准；关联文字与摘要在后台整理。\n'+
                json.dumps([{k:(row[k][:240] if isinstance(row[k],str) else row[k]) for k in ('id','source_id','category','previous','current')} for row in rows],ensure_ascii=False))

    def expression_context(self,db,chat,text):
        if not text.strip():return ''
        rows=self.r.memory_recall.find({'query':text,'category':'interaction','limit':12},chat,db)
        selected=[row for row in rows if row['content'].startswith(EXPRESSION_PREFIX)][:2]
        if not selected:return ''
        return ('[当前相关的表达经验]\n这是从真实互动归纳的参考，结合眼下的话用自己的方式接；不是固定台词。\n'+
                json.dumps([{'id':r['id'],'experience':r['content'][:360],'source_id':r['source_id'],'quote':r['quote'][:160]} for r in selected],ensure_ascii=False))

    def review_context(self,db,chat,wake_id=None):
        window=self.review_window(db,chat);pending=self.correction_rows(db,chat,pending=True)
        if not window['new_exchanges'] and not pending:return ''
        if wake_id:
            db.execute('INSERT OR IGNORE INTO memory_review_batches(wake_id,chat,after_id,through_id) VALUES(?,?,?,?)',
                       (wake_id,chat,window['after_id'],window['through_id']))
        return ('[新增交流的长期整理]\n'+json.dumps(window,ensure_ascii=False)+'\n'
                '沿用本轮日记与自省机会，只整理这个范围内的新增交流和待处理纠正。日记留经历；USER/MEMORY留持续有用的信息；SOUL按互动小幅调整。'
                '有反复适用的表达收获时，用现有memory_save保存category=interaction，content以“'+EXPRESSION_PREFIX+
                '”开头，写清“适用情境 → 表达习惯”，引用真实source_id与quote。已有经验用replace_id更新，少量保留。'
                '纠正还要检查偏好、文字记忆和摘要的旧引用；语义相同但措辞不同的引用也按最新原话修订，写入沿用revision。'
                '没有值得新增的信息就保持原样。\n'+self.correction_context(db,chat))

    def ordinary_review_context(self,db,chat,wake_id):
        # Ordinary opportunities stay conversational. Daily diary work owns
        # consolidation; this only catches a missed day or a pending correction.
        row=db.execute('SELECT updated FROM memory_review_cursor WHERE chat=?',(chat,)).fetchone()
        if row and time.time()-row[0]<86400 and not self.correction_rows(db,chat,pending=True):return ''
        return self.review_context(db,chat,wake_id)

    def finish_review(self,db,wake_id,outcome='completed'):
        batch=db.execute('SELECT * FROM memory_review_batches WHERE wake_id=? AND finished IS NULL',(wake_id,)).fetchone()
        diary=db.execute('SELECT outcome FROM diary_runs WHERE wake_id=?',(wake_id,)).fetchone()
        if not batch:return False
        if diary:
            if diary['outcome']!='completed':return False
        else:
            wake=db.execute('SELECT kind FROM wakes WHERE id=?',(wake_id,)).fetchone()
            if not wake or wake['kind']!='ordinary' or outcome!='completed':return False
        now=time.time()
        db.execute('UPDATE memory_review_cursor SET event_id=MAX(event_id,?),updated=? WHERE chat=?',(batch['through_id'],now,batch['chat']))
        db.execute('UPDATE memory_review_batches SET finished=? WHERE wake_id=?',(now,wake_id))
        # The daily model reviewed these corrections; later corrections remain pending.
        db.execute('UPDATE memory_correction_reviews SET reviewed=? WHERE chat=? AND reviewed IS NULL AND memory_id IN (SELECT id FROM memories WHERE source_id<=?)',
                   (now,batch['chat'],batch['through_id']))
        return True
