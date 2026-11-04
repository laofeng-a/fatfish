"""Parallel batches, bounded read reuse, and confirmed sent-media history."""
from collections import OrderedDict
from concurrent.futures import Future, ThreadPoolExecutor
import copy
import datetime as dt
import json
import re
import threading
import time

CACHE_TTLS = {'emote_find': 300, 'telegram_chats': 300, 'emote_history': 300,
              'history_find': 300, 'history_expand': 300, 'memory_note_read': 300,
              'chat_status': 1, 'notion': 30}

PARALLEL_READS = frozenset((
    'my_todos', 'chat_recall', 'diary_prepare', 'codex_overview',
    'emote_find', 'emote_history', 'telegram_chats', 'history_find',
    'history_expand', 'chat_status', 'memory_note_read', 'memory_find',
    'recall', 'get', 'list_recent', 'yesterday_health', 'x_feed', 'x_post_read',
    'bilibili_video', 'bilibili_comments', 'bilibili_discover', 'image_read',
    'douyin_video',
))
READ_ACTIONS = {
    'notion': frozenset(('search', 'read', 'children', 'block', 'database', 'data_source', 'query')),
    'codex': frozenset(('list', 'read', 'status', 'usage')),
    'dsh': frozenset(('list', 'read', 'status')),
    'claude_code': frozenset(('list', 'read', 'status')),
    'todo': frozenset(('list',)),
    'silent_alarm': frozenset(('list',)),
    'reminder': frozenset(('list',)),
}

BATCH_TOOL = {'name': 'batch', 'description':
    '把相互独立的本服务工具放在calls中，一次并行取得结果，例如表情目录、最近发图、群号、历史和Codex状态。'
    '每项用name（如emote_find、codex）及arguments；可沿用实际mcp_reader_前缀。'
    '相同读取会复用；需要前一结果的操作放在下一批，发送和修改按目标顺序执行。慢查询返回queued时可先做别的，结果回来自动接续。',
    'inputSchema': {'type': 'object', 'properties': {'calls': {'type': 'array', 'minItems': 1,
        'items': {'type': 'object', 'properties': {'name': {'type': 'string'},
            'arguments': {'type': 'object', 'additionalProperties': True}},
            'required': ['name'], 'additionalProperties': False}}}, 'required': ['calls'], 'additionalProperties': False},
    'annotations': {'readOnlyHint': False, 'destructiveHint': False, 'openWorldHint': True}}


def sent_media(runtime, args, context, chat):
    target = runtime.conversations.scope(args, context, chat)
    limit = args.get('limit', 10)
    if type(limit) is not int or not 1 <= limit <= 50: raise ValueError('limit需要1到50')
    cursor = args.get('cursor')
    if cursor is not None and (type(cursor) is not int or cursor < 1): raise ValueError('cursor需要已有记录编号')
    query = str(args.get('query', '')).strip()
    clauses = ["s.chat=?", "a.status='sent'", "a.method IN ('sendSticker','sendAnimation','sendPhoto')"]
    params = [target]
    if cursor is not None: clauses.append('a.rowid<?'); params.append(cursor)
    if query: clauses.append('instr(a.content,?)>0'); params.append(query)
    params.append(limit + 1)
    with runtime.db() as db:
        rows = db.execute('SELECT a.rowid AS cursor,a.content,a.message_id,a.updated,s.created,s.ticket '
            'FROM send_actions a JOIN sends s ON s.ticket=a.ticket WHERE ' + ' AND '.join(clauses) +
            ' ORDER BY a.rowid DESC LIMIT ?', params).fetchall()
    catalog = runtime.catalog + runtime.emotes.catalog(target)
    items = []
    for row in rows[:limit]:
        body = json.loads(row['content'])
        identifier = body.get('emote_id')
        candidates = [x for x in catalog if (x['id'] == identifier if identifier else x['name'] == body.get('media_name'))]
        selected = candidates[0] if len(candidates) == 1 else None
        item = {'cursor': row['cursor'], 'chat_id': target, 'message_id': row['message_id'],
                'sent_at': dt.datetime.fromtimestamp(row['updated'], dt.timezone.utc).isoformat(),
                'kind': body.get('media_kind'), 'name': body.get('media_name'), 'text': body.get('text', ''),
                'emote_id': identifier or (selected['id'] if selected else None),
                'id_source': 'delivery_record' if identifier else 'unique_catalog_name' if selected else 'unresolved'}
        if selected and selected.get('path'): item['path'] = selected['path']
        if not item['emote_id']: item['note'] = '旧记录缺少确切素材编号；保留实际发送名称和消息号。'
        if target.startswith('-100'): item['message_url'] = 'https://t.me/c/' + target[4:] + '/' + row['message_id']
        items.append(item)
    return {'chat_id': target, 'media': items, 'has_more': len(rows) > limit,
            'next_cursor': items[-1]['cursor'] if len(rows) > limit else None,
            'note': '这是已确认发送的图片、动图和贴纸记录；emote_id可直接交给emote_send重发。'}


class ToolEfficiency:
    def __init__(self, runtime, *, max_bytes=2*1024*1024, max_entries=64):
        self.r = runtime
        self.max_bytes, self.max_entries = max_bytes, max_entries
        self.cache, self.pending = OrderedDict(), {}
        self.bytes = 0
        self.generation = 0
        self.lock = threading.Lock()
        self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix='fatfish-tool')
        with runtime.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS tool_call_metrics(id INTEGER PRIMARY KEY,name TEXT,created REAL,elapsed_ms REAL,cache_hit INTEGER,failed INTEGER)')

    def close(self):
        self.pool.shutdown(wait=True)

    def version(self, name, args, context):
        target = str(args.get('chat_id') or context.get('chat_id') or self.r.owner)
        with self.r.db() as db:
            if name in ('history_find', 'history_expand'):
                return tuple(db.execute("SELECT MAX(id),COUNT(*) FROM events WHERE chat=? AND kind IN ('user','assistant','member','bot','forwarded','reading')", (target,)).fetchone())
            if name in ('emote_find', 'emote_history'):
                return (tuple(db.execute('SELECT MAX(updated),COUNT(*) FROM collected_emotes').fetchone()),
                        tuple(db.execute('SELECT MAX(updated),COUNT(*) FROM external_emotes').fetchone()),
                        tuple(db.execute('SELECT MAX(a.updated),COUNT(*) FROM send_actions a JOIN sends s ON s.ticket=a.ticket WHERE s.chat=?', (target,)).fetchone()),
                        tuple(db.execute('SELECT MAX(created),COUNT(*) FROM emote_inbox WHERE chat=?', (target,)).fetchone()))
            if name == 'telegram_chats':
                return tuple(tuple(row) for row in db.execute("SELECT k,v FROM state WHERE k IN ('groups','group_titles') ORDER BY k"))
            if name == 'chat_status':
                return tuple(db.execute('SELECT MAX(updated),COUNT(*) FROM conversation_receipts WHERE chat=?', (target,)).fetchone())
        if name == 'memory_note_read':
            path = self.r.notes.path(args.get('path', 'MEMORY.md'))
            info = path.stat() if path.exists() else None
            return (str(path), (info.st_dev, info.st_ino, info.st_size,
                    info.st_mtime_ns, info.st_ctime_ns) if info else None, args.get('version'))
        return None

    def clear(self):
        with self.lock:
            self.cache.clear(); self.bytes = 0; self.generation += 1

    def metric(self, name, started, hit=False, failed=False):
        try:
            with self.r.db() as db:
                db.execute('INSERT INTO tool_call_metrics(name,created,elapsed_ms,cache_hit,failed) VALUES(?,?,?,?,?)',
                           (name, time.time(), (time.perf_counter()-started)*1000, int(hit), int(failed)))
                db.execute('DELETE FROM tool_call_metrics WHERE id < (SELECT MAX(id)-2000 FROM tool_call_metrics)')
        except Exception: pass  # Observability must not change a completed tool outcome.

    @staticmethod
    def annotated(value, at, hit):
        result = copy.deepcopy(value)
        if isinstance(result, dict):
            result['read_cache'] = {'hit': hit, 'observed_at': dt.datetime.fromtimestamp(at, dt.timezone.utc).isoformat(),
                                    'age_seconds': round(max(0, time.time()-at), 3)}
        return result

    def invoke(self, name, arguments, execute):
        started = time.perf_counter()
        args = dict(arguments)
        refresh = bool(args.pop('refresh', False)) if name in CACHE_TTLS else False
        context = args.get('_context') or {}
        ttl = CACHE_TTLS.get(name, 0)
        if name == 'notion' and args.get('action') not in ('search','read','children','block','database','data_source','query'): ttl = 0
        if not ttl:
            try:
                result = execute(args)
                # Mutating tools invalidate prior reads; no permissions are changed.
                if name in ('emote_save','emote_update','emote_send','send_message','memory_save','memory_note_write',
                            'todo','reminder','silent_alarm','bilibili_comment_submit') or name=='notion': self.clear()
                self.metric(name, started)
                return result
            except Exception:
                self.metric(name, started, failed=True); raise
        identity={key:context.get(key) for key in ('channel','chat_id','session_key')}
        resource_args={key:value for key,value in args.items() if key!='_context'}
        key = json.dumps([name, resource_args, identity, self.version(name,args,context)], ensure_ascii=False, sort_keys=True)
        owner = False
        with self.lock:
            generation=self.generation
            entry = self.cache.get(key)
            if not refresh and entry and time.time()-entry[0] < ttl:
                self.cache.move_to_end(key)
                result = self.annotated(entry[1], entry[0], True)
            else:
                result = None
            if result is None:
                future = self.pending.get(key)
                if future is None:
                    future = Future(); self.pending[key] = future; owner = True
        if result is not None:
            self.metric(name, started, hit=True)
            return result
        if not owner:
            try:
                value, at = future.result()
                self.metric(name, started, hit=True)
                return self.annotated(value, at, True)
            except Exception:
                self.metric(name, started, failed=True); raise
        try:
            value = execute(args); at = time.time()
            size = len(json.dumps(value, ensure_ascii=False).encode())
            # Reads can record provenance; key the stored copy to the resulting state.
            stored_key = json.dumps([name, resource_args, identity, self.version(name,args,context)], ensure_ascii=False, sort_keys=True)
            with self.lock:
                if size <= self.max_bytes and generation==self.generation:
                    prior=self.cache.pop(stored_key,None)
                    if prior:self.bytes-=prior[2]
                    self.cache[stored_key]=(at,copy.deepcopy(value),size);self.bytes+=size
                    while self.bytes>self.max_bytes or len(self.cache)>self.max_entries:
                        _,removed=self.cache.popitem(last=False);self.bytes-=removed[2]
                future.set_result((value,at));self.pending.pop(key,None)
            self.metric(name,started)
            return self.annotated(value,at,False)
        except Exception as error:
            with self.lock:
                future.set_exception(error);self.pending.pop(key,None)
            self.metric(name,started,failed=True);raise

    def batch(self, args, execute):
        calls=args.get('calls');context=args.get('_context') or {}
        if not isinstance(calls,list) or not calls:raise ValueError('calls填写相互独立的工具调用列表')
        started=time.perf_counter()
        def one(index, call):
            begin=time.perf_counter();name=str(call.get('name','')).removeprefix('mcp_reader_') if isinstance(call,dict) else ''
            arguments=call.get('arguments',{}) if isinstance(call,dict) else {}
            try:
                if name=='batch':raise ValueError('batch直接列出工具即可，无需嵌套batch')
                if not name or not isinstance(arguments,dict):raise ValueError('每项需要工具name和arguments对象')
                result=execute(name,dict(arguments,_context=context))
                return {'index':index,'name':name,'result':result,'is_error':False,'elapsed_ms':round((time.perf_counter()-begin)*1000,2)}
            except ValueError as error:
                return {'index':index,'name':name,'error':str(error),'is_error':True,'elapsed_ms':round((time.perf_counter()-begin)*1000,2)}
            except Exception:
                return {'index':index,'name':name,'error':'此项工具未完成，其他结果仍可使用。','is_error':True,'elapsed_ms':round((time.perf_counter()-begin)*1000,2)}
        results=[]; pending=[]; parallel=False
        def flush():
            nonlocal parallel
            if not pending:return
            parallel = parallel or len(pending)>1
            futures=[self.pool.submit(one,i,call) for i,call in pending]
            results.extend(f.result() for f in futures)
            pending.clear()
        for index, call in enumerate(calls):
            name=str(call.get('name','')).removeprefix('mcp_reader_') if isinstance(call,dict) else ''
            arguments=call.get('arguments',{}) if isinstance(call,dict) else {}
            action=arguments.get('action') if isinstance(arguments,dict) else None
            reading=isinstance(arguments,dict) and (name in PARALLEL_READS or
                    isinstance(action,str) and action in READ_ACTIONS.get(name,()))
            if reading:
                pending.append((index,call))
            else:
                # Complete earlier reads and writes before this visible mutation.
                flush()
                results.append(one(index,call))
        flush()
        return {'results':results,'elapsed_ms':round((time.perf_counter()-started)*1000,2),
                'parallel':parallel,'tool_exchange_rounds':1}
