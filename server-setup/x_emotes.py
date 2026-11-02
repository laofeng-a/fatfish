"""Import a real X post photo into the scoped emote library, one bounded worker at a time."""
import hashlib,json,os,re,subprocess,tempfile,threading,time,urllib.parse
from pathlib import Path

IMPORT_LOCK=threading.Lock()
MAX_FILES=256
MAX_BYTES=64*1024*1024

def photo_url(value):
    if not isinstance(value,str) or len(value)>2048:raise ValueError('需要X帖子中真实的配图地址')
    try:
        url=urllib.parse.urlsplit(value)
        if url.scheme!='https' or url.hostname!='pbs.twimg.com' or url.port not in (None,443) or url.username or url.password or url.fragment:raise ValueError()
        match=re.fullmatch(r'/media/([A-Za-z0-9_-]+)(?:\.(jpg|jpeg|png|webp))?',url.path)
        if not match:raise ValueError()
        query=urllib.parse.parse_qs(url.query);formats=query.get('format',[])
        if len(formats)>1:raise ValueError()
        fmt=(formats[0] if formats else match[2] or '').lower().replace('jpeg','jpg')
        if fmt not in ('jpg','png','webp'):raise ValueError()
        if match[2] and match[2].replace('jpeg','jpg')!=fmt:raise ValueError()
        return 'https://pbs.twimg.com/media/'+match[1]+'?format='+fmt+'&name=medium'
    except (ValueError,TypeError):raise ValueError('这里只接X帖子里的静态配图，不接视频、头像或任意下载地址') from None

class XEmotes:
    def __init__(self,runtime):
        self.r=runtime;self.root=runtime.directory/'external-emotes'
        with runtime.db() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS external_emotes(
                id TEXT PRIMARY KEY,path TEXT,name TEXT,tags TEXT,mime TEXT,chat TEXT,
                source_chat TEXT,source_record INTEGER,source_url TEXT,post_url TEXT,digest TEXT,
                shared INTEGER DEFAULT 0,active INTEGER DEFAULT 1,updated REAL)''')

    def catalog(self,chat):
        with self.r.db() as db:rows=db.execute('''SELECT e.*,g.name AS group_name,g.tags AS group_tags FROM external_emotes e
            LEFT JOIN emote_group_labels g ON g.id=e.id AND g.chat=?
            WHERE e.active=1 ORDER BY e.updated DESC LIMIT 256''',(chat,)).fetchall()
        return [dict(id=r['id'],path=r['path'],name=r['group_name'] or r['name'],tags=json.loads(r['group_tags'] if r['group_tags'] is not None else r['tags']),kind='image',mime=r['mime'],source_url=r['post_url'],collected=True,shared=bool(r['shared'])) for r in rows]

    def source(self,args,context,chat):
        if 'message_id' in args:raise ValueError('X配图用source_id和url；Telegram素材用message_id，二者不混用')
        identifier=args.get('source_id')
        if type(identifier) is not int or identifier<1:raise ValueError('先读取X帖子，再用返回的record_id作为source_id')
        source_chat=self.r.conversations.scope({'chat_id':args.get('source_chat_id',chat)},context,chat)
        requested=photo_url(args['url'])
        with self.r.db() as db:row=db.execute("SELECT content FROM events WHERE id=? AND chat=? AND kind='reading'",(identifier,source_chat)).fetchone()
        if not row:raise ValueError('没有找到当前可见会话里的真实X读取记录')
        try:record=json.loads(row['content'])
        except (TypeError,ValueError):raise ValueError('读取记录过长或无效；请用x_post_read读取对应单帖后再收藏') from None
        if not isinstance(record,dict) or record.get('tool') not in ('x_feed','x_post_read'):raise ValueError('来源必须是实际读取的X帖子或信息流')
        result=record.get('result');rows=result.get('data',[]) if isinstance(result,dict) else []
        if not isinstance(rows,list):raise ValueError('来源缺少实际帖子数据')
        for post in rows:
            if not isinstance(post,dict):continue
            media=post.get('media',[])
            if not isinstance(media,list):continue
            for item in media:
                if not isinstance(item,dict) or item.get('type')!='photo':continue
                try:matches=photo_url(item.get('url'))==requested
                except ValueError:continue
                if matches:
                    post_id=str(post.get('id',''))
                    if not re.fullmatch(r'[0-9]{1,25}',post_id):raise ValueError('来源帖子缺少真实编号')
                    author=post.get('author') or {};author=author.get('screenName','i') if isinstance(author,dict) else 'i'
                    if not isinstance(author,str) or not re.fullmatch(r'[A-Za-z0-9_]{1,15}',author):author='i'
                    return requested,source_chat,'https://x.com/'+author+'/status/'+post_id
        raise ValueError('这张静态图没有出现在指定X读取记录的配图里；不能把正文链接、视频封面当作已取得的照片')

    def save(self,args,context,chat,name,tags):
        url,source_chat,post=self.source(args,context,chat)
        shared=args.get('share_in_groups',False)
        if type(shared) is not bool:raise ValueError('share_in_groups需要布尔值')
        if not IMPORT_LOCK.acquire(blocking=False):raise ValueError('正在导入另一张图，请稍后再试')
        new_file=None
        try:
            if self.root.is_symlink():raise ValueError('图片存储目录异常')
            self.root.mkdir(mode=0o700,exist_ok=True)
            with tempfile.TemporaryDirectory(prefix='.import-',dir=self.root) as temporary:
                target=Path(temporary)/'image.jpg'
                command=[self.r.settings['worker_python'],self.r.settings['image_worker'],'--url',url,'--static-only','--output',str(target)]
                try:process=subprocess.run(command,capture_output=True,timeout=25)
                except subprocess.TimeoutExpired:raise ValueError('X图片导入超时，没有收藏成功') from None
                if process.returncode:raise ValueError('没有取得可导入的静态图：可能是动图、图片过大、损坏或读取失败')
                if target.is_symlink() or not target.is_file() or not 0<target.stat().st_size<=1_000_000:raise ValueError('没有取得合规的图片文件')
                data=target.read_bytes()
                if not data.startswith(b'\xff\xd8\xff'):raise ValueError('图片转换未得到JPEG')
                digest=hashlib.sha256(data).hexdigest();path=self.root/(digest+'.jpg')
                if path.is_symlink():raise ValueError('图片存储路径异常')
                if not path.exists():
                    files=list(self.root.glob('*.jpg'))
                    if any(p.is_symlink() or not p.is_file() for p in files):raise ValueError('图片存储目录存在异常文件')
                    if len(files)>=MAX_FILES or sum(p.stat().st_size for p in files)+len(data)>MAX_BYTES:raise ValueError('图片收藏空间已满，本次未导入；需要维护清理或调整容量')
                    os.replace(target,path);new_file=path;os.chmod(path,0o600)
                identifier='x_'+hashlib.sha256((chat+':'+digest).encode()).hexdigest()[:24]
                with self.r.db() as db:
                    old=db.execute('SELECT * FROM external_emotes WHERE id=?',(identifier,)).fetchone()
                    if old and 'share_in_groups' not in args:shared=bool(old['shared'])
                    stored_name,stored_tags=name,json.dumps(tags,ensure_ascii=False)
                    db.execute('''INSERT INTO external_emotes VALUES(?,?,?,?,?,?,?,?,?,?,?,?,1,?)
                        ON CONFLICT(id) DO UPDATE SET name=excluded.name,tags=excluded.tags,source_chat=excluded.source_chat,
                        source_record=excluded.source_record,source_url=excluded.source_url,post_url=excluded.post_url,
                        shared=excluded.shared,active=1,updated=excluded.updated''',
                        (identifier,str(path),stored_name,stored_tags,'image/jpeg',chat,source_chat,args['source_id'],url,post,digest,int(shared),time.time()))
                    if chat.startswith('-'):db.execute('INSERT OR REPLACE INTO emote_group_labels VALUES(?,?,?,?,?)',(identifier,chat,name,json.dumps(tags,ensure_ascii=False),time.time()))
                return {'status':'saved','id':identifier,'name':name,'tags':tags,'kind':'image','source_url':post,'source_record_id':args['source_id'],'share_in_groups':shared,'note':'已收藏静态图片；用emote_send发送，不会自动变成Telegram贴纸套装'}
        except Exception:
            if new_file is not None:
                try:
                    with self.r.db() as db:referenced=db.execute('SELECT 1 FROM external_emotes WHERE path=? LIMIT 1',(str(new_file),)).fetchone()
                except Exception:referenced=True
                if not referenced:new_file.unlink(missing_ok=True)
            raise
        finally:IMPORT_LOCK.release()

    def update(self,args,labels):
        with self.r.db() as db:
            row=db.execute('SELECT * FROM external_emotes WHERE id=?',(args['id'],)).fetchone()
            if not row:raise ValueError('未找到这张已导入的X图片')
            name,tags=labels({'name':args.get('name',row['name']),'tags':args.get('tags',json.loads(row['tags']))})
            shared=args.get('share_in_groups',bool(row['shared']));active=args.get('active',bool(row['active']))
            if type(shared) is not bool or type(active) is not bool:raise ValueError('表情开关需要布尔值')
            db.execute('UPDATE external_emotes SET name=?,tags=?,shared=?,active=?,updated=? WHERE id=?',(name,json.dumps(tags,ensure_ascii=False),int(shared),int(active),time.time(),args['id']))
            if row['chat'].startswith('-'):db.execute('UPDATE emote_group_labels SET name=?,tags=?,updated=? WHERE id=? AND chat=?',(name,json.dumps(tags,ensure_ascii=False),time.time(),args['id'],row['chat']))
        return {'status':'updated','id':args['id'],'name':name,'tags':tags,'active':active,'share_in_groups':shared}
