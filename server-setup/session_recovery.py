"""Recover concatenated JSONL appends while retaining original bytes for rollback."""
import datetime as dt
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3


def atomic(path,raw,stat):
    temporary=path.with_name('.recovery-'+path.name)
    with temporary.open('wb') as out:out.write(raw);out.flush();os.fsync(out.fileno())
    os.chmod(temporary,stat.st_mode&0o777)
    if hasattr(os,'chown'):os.chown(temporary,stat.st_uid,stat.st_gid)
    os.replace(temporary,path)


def recover_session(path,backup,lookup,apply=False):
    path=Path(path);raw=path.read_bytes();pieces=[];changes=[];old_count=0;skip_count=0
    meta_path=path.with_suffix('.meta.json');meta=json.loads(meta_path.read_text()) if meta_path.exists() else {}
    decoder=json.JSONDecoder()
    for line in raw.splitlines(keepends=True):
        if not line.strip():pieces.append(line);continue
        old_count+=1;records=[]
        try:json.loads(line);records=[line if line.endswith(b'\n') else line+b'\n']
        except (ValueError,UnicodeError):
            text=line.decode('utf-8','replace');tail=None;offset=len(text)
            for match in re.finditer(r'\{',text):
                try:value,end=decoder.raw_decode(text[match.start():])
                except ValueError:continue
                if isinstance(value,dict) and value.get('role') in ('user','assistant','tool','system') and not text[match.start()+end:].strip():
                    tail=value;offset=match.start();break
            prefix=text[:offset];marker=re.search(r'"content"\s*:',prefix);restored=None;source=None;strategy='matching_recorded_content'
            if marker:
                try:
                    header=json.loads(prefix[:marker.end()]+'""}')
                    candidate,source=lookup(header)
                    fragment=prefix[marker.end():].lstrip()
                    partial=None
                    if fragment.startswith('"'):
                        fragment=fragment[1:]
                        for trim in range(9):
                            try:partial=json.loads('"'+(fragment[:-trim] if trim else fragment)+'"');break
                            except ValueError:continue
                    injected=(header.get('source_kind')=='human' and str(header.get('source_message_id','')).isdecimal()
                              and partial is not None and partial.startswith('[当前相关的表达经验]\n'))
                    if candidate is not None and (partial is None or candidate.startswith(partial.rstrip('\ufffd')) or injected):
                        restored=dict(header,content=candidate)
                        if injected:strategy='recorded_user_text_without_truncated_injected_context'
                except (ValueError,KeyError):pass
            if restored:records.append((json.dumps(restored,ensure_ascii=False)+'\n').encode())
            if tail:records.append((json.dumps(tail,ensure_ascii=False)+'\n').encode())
            changes.append({'raw_line':old_count,'raw_bytes':len(line),'restored_prefix':bool(restored),
                'retained_tail':bool(tail),'source':source,'strategy':strategy,'original_sha256':hashlib.sha256(line).hexdigest()})
        if old_count<=meta.get('skip',0):skip_count+=len(records)
        pieces.extend(records)
    fixed=b''.join(pieces)
    result={'path':str(path),'before_sha256':hashlib.sha256(raw).hexdigest(),'after_sha256':hashlib.sha256(fixed).hexdigest(),'changes':changes,'applied':False}
    if changes and apply:
        backup=Path(backup);backup.mkdir(parents=True,exist_ok=True)
        with gzip.open(backup/(path.name+'.before.gz'),'wb') as out:out.write(raw)
        if meta_path.exists():
            with gzip.open(backup/(meta_path.name+'.before.gz'),'wb') as out:out.write(meta_path.read_bytes())
        for line in fixed.splitlines():
            if line.strip():json.loads(line)
        atomic(path,fixed,path.stat())
        if meta_path.exists():
            meta.update(count=sum(bool(line.strip())for line in fixed.splitlines()),skip=skip_count)
            atomic(meta_path,json.dumps(meta,ensure_ascii=False).encode(),meta_path.stat())
        result['applied']=True
    return result


class RecordedSources:
    def __init__(self,runtime,ledger):
        self.runtime=sqlite3.connect('file:'+str(runtime)+'?mode=ro',uri=True)
        self.ledger=sqlite3.connect('file:'+str(ledger)+'?mode=ro',uri=True)

    def __call__(self,header):
        message=str(header.get('source_message_id',''));history=header.get('history_id','')
        if header.get('role')!='user':return None,None
        clauses=[];values=[]
        if history:clauses.append('source_key IN (?,?)');values.extend([history,'internal:'+history])
        if message:clauses.append('message_id=?');values.append(message)
        row=self.ledger.execute('SELECT user_text FROM context_turns WHERE '+' OR '.join(clauses)+' ORDER BY id DESC LIMIT 1',values).fetchone() if clauses else None
        if row:return row[0],'context_turns'
        if message.startswith('codex-event:'):
            identifier=message.removeprefix('codex-event:')
            for backend in ('codex','dsh','claude'):
                row=self.runtime.execute('SELECT kind,content FROM '+backend+'_events WHERE id=?',(identifier,)).fetchone()
                if row:
                    kind='Codex查询结果' if row[0]=='query' else 'Codex协作回执'
                    return '['+kind+']\n'+row[1],backend+'_events:'+identifier
        if message.isdecimal():
            row=self.runtime.execute("SELECT content FROM events WHERE msg=? AND chat=? AND kind='user'",(message,header.get('source_chat_id'))).fetchone()
            if row:return row[0],'events:'+message
        return None,None
