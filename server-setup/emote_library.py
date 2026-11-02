"""Collect Telegram file IDs with actual incoming-message evidence."""
import hashlib,json,re,time
from x_emotes import XEmotes

class EmoteLibrary:
    def __init__(self,runtime):
        self.r=runtime
        with runtime.db() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS emote_inbox(chat TEXT,msg TEXT,kind TEXT,file_id TEXT,unique_id TEXT,emoji TEXT,created REAL,PRIMARY KEY(chat,msg));
            CREATE TABLE IF NOT EXISTS collected_emotes(id TEXT PRIMARY KEY,file_id TEXT,unique_id TEXT UNIQUE,name TEXT,tags TEXT,kind TEXT,mime TEXT,source_chat TEXT,source_msg TEXT,shared INTEGER DEFAULT 0,active INTEGER DEFAULT 1,updated REAL);
            CREATE TABLE IF NOT EXISTS emote_group_labels(id TEXT,chat TEXT,name TEXT,tags TEXT,updated REAL,PRIMARY KEY(id,chat));
            ''')
        self.external=XEmotes(runtime)
    def receive(self,db,chat,message,media):
        if not isinstance(media,dict):return
        kind=media.get('kind');file_id=media.get('file_id');unique=media.get('unique_id')
        if kind not in ('sticker','image','animation') or not all(isinstance(x,str) and 0<len(x)<=512 for x in (file_id,unique)):return
        if not re.fullmatch(r'[1-9][0-9]*',str(message)):return
        db.execute('INSERT OR REPLACE INTO emote_inbox VALUES(?,?,?,?,?,?,?)',(chat,str(message),kind,file_id,unique,str(media.get('emoji',''))[:40],time.time()))
        db.execute('DELETE FROM emote_inbox WHERE created<?',(time.time()-7*86400,))
    def catalog(self,chat):
        with self.r.db() as db:
            rows=db.execute('SELECT e.*,g.name AS group_name,g.tags AS group_tags FROM collected_emotes e LEFT JOIN emote_group_labels g ON g.id=e.id AND g.chat=? WHERE e.active=1 ORDER BY e.updated DESC LIMIT 500',(chat,)).fetchall()
        return [dict(id=r['id'],file_id=r['file_id'],name=r['group_name'] or r['name'],tags=json.loads(r['group_tags'] if r['group_tags'] is not None else r['tags']),kind=r['kind'],mime=r['mime'],collected=True,shared=bool(r['shared'])) for r in rows]+self.external.catalog(chat)
    def inbox(self,chat,source_chat=None):
        with self.r.db() as db:
            if source_chat is not None:
                source_chat=str(source_chat)
                chats=[source_chat]
            else:chats=[self.r.owner,*json.loads(self.r._get(db,'groups','[]'))]
            return [dict(r) for r in db.execute('SELECT chat AS source_chat_id,msg AS message_id,kind,emoji,created FROM emote_inbox WHERE chat IN ('+','.join('?' for _ in chats)+') ORDER BY created DESC LIMIT 12',chats)]
    def save(self,args,context,chat):
        name,tags=self.labels(args)
        if 'url' in args:return self.external.save(args,context,chat,name,tags)
        if 'source_id' in args:raise ValueError('source_id用于X配图，需同时提供url')
        source=args.get('message_id')
        with self.r.db() as db:
            source_chat=str(args.get('source_chat_id',chat))
            if source is None:
                current=str(context.get('message_id','')) if source_chat==chat else ''
                reply=db.execute('SELECT reply FROM message_replies WHERE chat=? AND msg=?',(source_chat,current)).fetchone()
                source=reply[0] if reply else current
                row=db.execute('SELECT * FROM emote_inbox WHERE chat=? AND msg=?',(source_chat,str(source))).fetchone()
                if not row:row=db.execute('SELECT * FROM emote_inbox WHERE chat=? AND created>? ORDER BY created DESC LIMIT 1',(source_chat,time.time()-600)).fetchone()
            else:row=db.execute('SELECT * FROM emote_inbox WHERE chat=? AND msg=?',(source_chat,str(source))).fetchone()
            if not row:raise ValueError('没找到这条真实图片或贴纸；可以重发，或回复原图说收下这个')
            shared=args.get('share_in_groups',False)
            if type(shared) is not bool:raise ValueError('share_in_groups需要布尔值')
            identifier='tg_'+hashlib.sha256(row['unique_id'].encode()).hexdigest()[:16]
            old=db.execute('SELECT * FROM collected_emotes WHERE id=?',(identifier,)).fetchone()
            if old and 'share_in_groups' not in args:shared=bool(old['shared'])
            db.execute('INSERT INTO collected_emotes VALUES(?,?,?,?,?,?,?,?,?,?,1,?) ON CONFLICT(id) DO UPDATE SET file_id=excluded.file_id,name=excluded.name,tags=excluded.tags,shared=excluded.shared,active=1,updated=excluded.updated',
                (identifier,row['file_id'],row['unique_id'],name,json.dumps(tags,ensure_ascii=False),row['kind'],'image/jpeg' if row['kind']=='image' else 'image/gif',source_chat,row['msg'],int(shared),time.time()))
            if source_chat.startswith('-'):
                db.execute('INSERT OR REPLACE INTO emote_group_labels VALUES(?,?,?,?,?)',(identifier,source_chat,name,json.dumps(tags,ensure_ascii=False),time.time()))
        return {'status':'saved','id':identifier,'name':name,'tags':tags,'share_in_groups':shared,'source_chat_id':source_chat,'source_message_id':row['msg']}
    def update(self,args):
        identifier=args.get('id')
        if isinstance(identifier,str) and identifier.startswith('x_'):return self.external.update(args,self.labels)
        with self.r.db() as db:
            row=db.execute('SELECT * FROM collected_emotes WHERE id=?',(identifier,)).fetchone()
            if not row:raise ValueError('未找到这张已收藏表情')
            name,tags=self.labels({'name':args.get('name',row['name']),'tags':args.get('tags',json.loads(row['tags']))})
            shared=args.get('share_in_groups',bool(row['shared']));active=args.get('active',bool(row['active']))
            if type(shared) is not bool or type(active) is not bool:raise ValueError('表情开关需要布尔值')
            db.execute('UPDATE collected_emotes SET name=?,tags=?,shared=?,active=?,updated=? WHERE id=?',(name,json.dumps(tags,ensure_ascii=False),int(shared),int(active),time.time(),identifier))
            if row['source_chat'].startswith('-'):
                db.execute('UPDATE emote_group_labels SET name=?,tags=?,updated=? WHERE id=? AND chat=?',(name,json.dumps(tags,ensure_ascii=False),time.time(),identifier,row['source_chat']))
        return {'status':'updated','id':identifier,'name':name,'tags':tags,'active':active,'share_in_groups':shared}
    @staticmethod
    def labels(args):
        name=args.get('name');tags=args.get('tags',[])
        if not isinstance(name,str) or not 1<=len(name.strip())<=80:raise ValueError('给表情起一个简短名称')
        if not isinstance(tags,list) or len(tags)>24 or any(not isinstance(t,str) or not 1<=len(t.strip())<=48 for t in tags):raise ValueError('表情标签需要简短词语列表')
        return name.strip(),list(dict.fromkeys(t.strip() for t in tags))
