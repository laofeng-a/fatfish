"""Versioned Markdown memory and self-managed profile files."""
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import time

UTC8=dt.timezone(dt.timedelta(hours=8))
PROFILE_FILES=frozenset(('USER.md','HEARTBEAT.md','AGENT.md','SOUL.md'))


class MemoryNotes:
    def __init__(self,runtime):
        self.runtime=runtime
        self.root=runtime.directory/'notes'
        if self.root.is_symlink():raise ValueError('记忆目录不能是符号链接')
        self.root.mkdir(mode=0o750,exist_ok=True)
        self.profile=runtime.directory/'profile'
        if self.profile.is_symlink():raise ValueError('偏好目录不能是符号链接')
        self.profile.mkdir(mode=0o750,exist_ok=True)
        with runtime.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS note_versions(id INTEGER PRIMARY KEY,path TEXT,previous_content TEXT,revision TEXT,new_revision TEXT,sources TEXT,context TEXT,created REAL)')

    def path(self,name):
        if not isinstance(name,str):raise ValueError('需要记忆路径')
        if name in PROFILE_FILES:
            path=self.profile/name
            if self.profile.is_symlink() or path.is_symlink():raise ValueError('偏好路径不能链接到其他文件')
            return path
        if name!='MEMORY.md':
            match=re.fullmatch(r'(\d{6})/(\d{8})\.md',name)
            if not match or not match[2].startswith(match[1]):raise ValueError('路径使用MEMORY.md、YYYYMM/YYYYMMDD.md、USER.md、HEARTBEAT.md、AGENT.md或SOUL.md')
            try:day=dt.datetime.strptime(match[2],'%Y%m%d').date()
            except ValueError:raise ValueError('日记日期不合法') from None
            if day>dt.datetime.now(UTC8).date():raise ValueError('不能预写未来经历')
        path=self.root/name
        if self.root.is_symlink() or path.parent.is_symlink() or path.is_symlink():raise ValueError('记忆路径不能链接到其他文件')
        return path

    def content(self,path):
        if not path.exists():return ''
        info=path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_size>8000:raise ValueError('记忆文件类型或大小不合法')
        fd=os.open(path,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
        with os.fdopen(fd,'rb') as stream:raw=stream.read(8001)
        if len(raw)>8000:raise ValueError('记忆文件过大')
        try:return raw.decode('utf-8')
        except UnicodeDecodeError:raise ValueError('记忆需要UTF-8文字') from None

    @staticmethod
    def revision(content):return hashlib.sha256(content.encode('utf-8')).hexdigest()

    def name(self,path):
        return path.name if path.parent==self.profile else path.relative_to(self.root).as_posix()

    def files(self):
        result=[]
        for path in sorted(self.root.glob('*/*.md'),reverse=True):
            self.path(path.relative_to(self.root).as_posix())
            result.append(path)
            if len(result)>400:raise ValueError('日记数量达到上限，请先整理')
        core=self.root/'MEMORY.md'
        profiles=[self.path(name) for name in sorted(PROFILE_FILES) if (self.profile/name).exists() or (self.profile/name).is_symlink()]
        return ([core] if core.exists() else [])+profiles+result

    def read(self,name='MEMORY.md',version=None):
        if name=='index':
            if version is not None:raise ValueError('目录没有历史版本')
            files=self.files()
            return {'files':[{'path':self.name(p),'bytes':p.stat().st_size} for p in files[:40]],'total_files':len(files),'truncated':len(files)>40}
        path=self.path(name);content=self.content(path)
        result={'path':name,'content':content,'revision':self.revision(content),'exists':path.exists()}
        with self.runtime.db() as db:
            if version is not None:
                if type(version) is not int or version<1:raise ValueError('历史版本编号不合法')
                row=db.execute('SELECT id,previous_content,revision,created FROM note_versions WHERE path=? AND id=?',(name,version)).fetchone()
                if not row:raise ValueError('没有这个文件的历史版本')
                result.update(content=row['previous_content'],revision=row['revision'],current_revision=result['revision'],historical=True,version=row['id'],created=row['created'])
            else:
                result['versions']=[dict(row) for row in db.execute('SELECT id,revision,created FROM note_versions WHERE path=? ORDER BY id DESC LIMIT 5',(name,))]
        return result

    def search(self,query):
        if not isinstance(query,str) or not query.strip():return []
        from memory_recall import search_notes
        return search_notes(self,query)

    def write(self,args,context,chat):
        name=args.get('path');path=self.path(name)
        content=args.get('content');expected=args.get('expected_revision')
        if not isinstance(content,str) or '\x00' in content:raise ValueError('需要有效的记忆文字')
        limit=8000 if name=='MEMORY.md' or name in PROFILE_FILES else 6000
        if name in PROFILE_FILES and not content.strip():raise ValueError('偏好与后台文件不能清空；可以删除已完成的临时事项')
        if name!='MEMORY.md' and name not in PROFILE_FILES and not content.strip():
            raise ValueError('日记不能为空白；没有可核实的新经历时记录实际情况，不编造经历')
        if len(content.encode('utf-8'))>limit:raise ValueError('记忆太长，请整理后再保存；本文件字节上限为'+str(limit))
        sources=args.get('source_ids',[])
        if not isinstance(sources,list) or len(sources)>8 or any(type(x) is not int for x in sources):raise ValueError('来源编号不合法')
        with self.runtime.db() as db:
            db.execute('BEGIN IMMEDIATE')
            for identifier in sources:
                if not db.execute('SELECT 1 FROM events WHERE id=?',(identifier,)).fetchone():raise ValueError('没有找到该来源记录')
            old=self.content(path);revision=self.revision(old)
            if expected!=revision:raise ValueError('记忆已变更或尚未读取，请先读取最新内容与revision再修改')
            files=self.files()
            if name not in PROFILE_FILES and not path.exists() and len([p for p in files if p.parent!=self.profile])>=401:raise ValueError('记忆文件数量达到上限')
            if sum(p.stat().st_size for p in files)+len(content.encode('utf-8'))-len(old.encode('utf-8'))>4*1024*1024:raise ValueError('记忆空间达到上限，请先整理')
            if old==content:return {'status':'unchanged','path':name,'revision':revision,'exists':path.exists(),'bytes':len(old.encode('utf-8'))}
            path.parent.mkdir(mode=0o750,exist_ok=True)
            fd,temporary=tempfile.mkstemp(prefix='.note-',dir=path.parent)
            try:
                with os.fdopen(fd,'w',encoding='utf-8',newline='\n') as stream:
                    stream.write(content);stream.flush();os.fsync(stream.fileno())
                os.chmod(temporary,0o640)
                new_revision=self.revision(content)
                db.execute('INSERT INTO note_versions(path,previous_content,revision,new_revision,sources,context,created) VALUES(?,?,?,?,?,?,?)',
                    (name,old,revision,new_revision,json.dumps(sources),json.dumps({'chat':chat,'message_id':context.get('message_id'),'session_key':context.get('session_key')},ensure_ascii=False),time.time()))
                # Keep profile recovery points even when many diary versions arrive later.
                db.execute('DELETE FROM note_versions WHERE path=? AND id NOT IN (SELECT id FROM note_versions WHERE path=? ORDER BY id DESC LIMIT 5)',(name,name))
                profiles=tuple(sorted(PROFILE_FILES));slots=','.join('?' for _ in profiles)
                db.execute(f'DELETE FROM note_versions WHERE path NOT IN ({slots}) AND id NOT IN (SELECT id FROM note_versions WHERE path NOT IN ({slots}) ORDER BY id DESC LIMIT 190)',profiles+profiles)
                os.replace(temporary,path)
                # A successful write means the actual target can be read back,
                # not just that a temporary file was accepted.
                actual=self.content(path)
                if actual!=content or self.revision(actual)!=new_revision:
                    raise ValueError('记忆写入后的读取核对失败，请先重读实际文件再处理')
            finally:
                if os.path.exists(temporary):os.unlink(temporary)
        return {'status':'saved','path':name,'revision':new_revision,'bytes':len(content.encode('utf-8')),'source_ids':sources,'exists':True,'readback_verified':True}
