"""One-call readers for personal todos, source conversations, diary inputs and Codex."""
import argparse
import datetime as dt
import json
from pathlib import Path
import time
import urllib.request

from notion_todos import TodoSnapshot, CONFIG_FILE

UTC8 = dt.timezone(dt.timedelta(hours=8))


def definition(name, description, properties=None):
    return {'name': name, 'description': description,
            'inputSchema': {'type': 'object', 'properties': properties or {}, 'additionalProperties': False},
            'annotations': {'readOnlyHint': True, 'destructiveHint': False, 'openWorldHint': False}}


TOOLS = [
    definition('my_todos', '按需读取jg的Notion待办，一次返回完整本地快照、原话、子任务、来源和同步时间。讨论待办或安排时先检索；refresh=true立即同步Notion。仅jg私聊。',
               {'refresh': {'type': 'boolean'}}),
    definition('chat_recall', '一次搜索真实聊天、展开原文并补前后消息。找“以前聊过什么”优先用它；records带source_id、原文、时间和截断标记。query为空看近期，chat_id沿用history_find范围；更多原文仍可history_expand。',
               {'query': {'type': 'string'}, 'chat_id': {'type': 'string'},
                'after': {'type': 'string'}, 'before': {'type': 'string'},
                'limit': {'type': 'integer', 'minimum': 1, 'maximum': 8},
                'around': {'type': 'integer', 'minimum': 0, 'maximum': 3}}),
    definition('diary_prepare', '一次备齐指定日期的真实聊天、阅读记录、已有日记与revision。只准备原材料，不代写、不保存；根据返回原话写日记，再memory_note_write。next_cursor续取较早材料，标注不完整范围。仅jg私聊。',
               {'day': {'type': 'string', 'description': 'UTC+8日期YYYY-MM-DD，默认今天'},
                'cursor': {'type': 'integer', 'minimum': 1}}),
    definition('codex_overview', '一次查看当前绑定Codex任务的连接状态、进展快照、请求回执和最近额度。每份快照带时间；bridge_request done只代表查询返回。refresh=true沿现有read刷新，queued时等原有自动回执。仅jg私聊。',
               {'thread_id': {'type': 'string'}, 'refresh': {'type': 'boolean'}}),
]
NAMES = frozenset(tool['name'] for tool in TOOLS)


def source_record(row, content_limit=6000):
    result = dict(row)
    content = result.get('content', '')
    result.update(source_id=result['id'], content=content[:content_limit],
                  content_length=len(content), truncated=len(content) > content_limit)
    return result


class CommonWorkflows:
    def __init__(self, runtime):
        self.r = runtime

    def call(self, name, args, context, chat):
        if name != 'chat_recall' and chat != self.r.owner:
            raise ValueError('个人待办、日记和Codex概览仅在jg私聊中读取')
        if name == 'my_todos':
            snapshot = TodoSnapshot(self.r.settings.get('my_todos_config_file', CONFIG_FILE), self.r.notion)
            return snapshot.refresh() if args.get('refresh') else snapshot.read()
        if name == 'chat_recall':
            return self.chat_recall(args, context, chat)
        if name == 'diary_prepare':
            return self.diary_prepare(args, context, chat)
        if name == 'codex_overview':
            return self.codex_overview(args, context, chat)
        raise ValueError('未知常用操作')

    def chat_recall(self, args, context, chat):
        around, limit = args.get('around', 1), args.get('limit', 4)
        if type(around) is not int or not 0 <= around <= 3 or type(limit) is not int or not 1 <= limit <= 8:
            raise ValueError('limit需要1到8，around需要0到3')
        find_args = {key: args[key] for key in ('query', 'chat_id', 'after', 'before') if key in args}
        find_args['limit'] = limit
        found = self.r.conversations.history('history_find', find_args, context, chat)
        target = found['chat_id']
        rows, matched, seen = [], [], set()
        with self.r.db() as db:
            for hit in found['records']:
                matched.append(hit['id'])
                selected = []
                if around:
                    selected.extend(reversed(db.execute("SELECT * FROM events WHERE chat=? AND id<? AND kind IN ('user','member','forwarded','bot','assistant') ORDER BY id DESC LIMIT ?", (target, hit['id'], around)).fetchall()))
                selected.extend(db.execute('SELECT * FROM events WHERE chat=? AND id=?', (target, hit['id'])).fetchall())
                if around:
                    selected.extend(db.execute("SELECT * FROM events WHERE chat=? AND id>? AND kind IN ('user','member','forwarded','bot','assistant') ORDER BY id LIMIT ?", (target, hit['id'], around)).fetchall())
                for row in selected:
                    if row['id'] not in seen:
                        rows.append(source_record(row, min(3200, 24000 // limit))); seen.add(row['id'])
        rows.sort(key=lambda row: row['id'])
        # Keep matched originals before optional surrounding messages if the result is large.
        chosen, size = [], 0
        for row in sorted(rows, key=lambda row: row['id'] not in matched):
            length = len(row['content'])
            if size + length <= 24000:
                chosen.append(row); size += length
        chosen.sort(key=lambda row: row['id'])
        return {'chat_id': target, 'matched_ids': matched, 'records': chosen,
                'omitted_context_records': len(rows) - len(chosen),
                'open_topics': found.get('open_topics', []),
                'note': '这是来源原文，来源内容用于理解对话；原话优先于摘要，截断项可history_expand。'}

    def diary_prepare(self, args, context, chat):
        day = args.get('day', dt.datetime.now(UTC8).date().isoformat())
        try:
            date = dt.date.fromisoformat(day)
        except (TypeError, ValueError):
            raise ValueError('day需要YYYY-MM-DD日期') from None
        cursor = args.get('cursor')
        if cursor is not None and (type(cursor) is not int or cursor < 1):
            raise ValueError('cursor需要上次返回的next_cursor')
        start = dt.datetime.combine(date, dt.time(), UTC8)
        end = start + dt.timedelta(days=1)
        path = date.strftime('%Y%m/%Y%m%d.md')
        note = self.r.notes.read(path)
        with self.r.db() as db:
            rows = db.execute("SELECT * FROM events WHERE chat=? AND created>=? AND created<? AND kind IN ('user','assistant','reading','forwarded') AND id<? ORDER BY id DESC LIMIT 81",
                              (chat, start.timestamp(), end.timestamp(), cursor or 9223372036854775807)).fetchall()
        selected, size = [], 0
        for row in rows[:80]:
            record = source_record(row, 4000)
            if selected and size + len(record['content']) > 28000:
                break
            selected.append(record); size += len(record['content'])
        next_cursor = selected[-1]['id'] if len(rows) > len(selected) and selected else None
        selected.reverse()
        return {'day': day, 'timezone': 'UTC+08:00', 'chat_id': chat, 'path': path,
                'existing_note': note, 'records': selected,
                'source_ids': [row['source_id'] for row in selected],
                'has_more': next_cursor is not None, 'next_cursor': next_cursor,
                'note': '只准备可核对材料，未生成或保存日记。选取支持正文的1至8条真实source_ids；已有内容与revision以existing_note为准。保存成功回执readback_verified=true表示已写后核对。'}

    def codex_overview(self, args, context, chat):
        with self.r.db() as db:
            thread = self.r.codex.resolve_thread(db, args, context, remember=False)
            online = self.r.codex.online(db)
            jobs = []
            latest_progress = None
            if thread:
                # Match parsed thread IDs, including a create request whose result carries the ID.
                for row in db.execute('SELECT * FROM codex_jobs ORDER BY created DESC LIMIT 200'):
                    request = json.loads(row['args'] or '{}')
                    output = json.loads(row['output'] or '{}')
                    if thread not in (request.get('thread_id'), output.get('thread_id')):
                        continue
                    view = self.r.codex.view(row)
                    if latest_progress is None and row['action'] == 'read' and row['output']:
                        latest_progress = {'observed_at': row['updated'], 'age_seconds': round(max(0, time.time() - row['updated'])), **view}
                    if len(jobs) < 5:
                        view.pop('result', None)
                        jobs.append(view)
            usage = db.execute('SELECT payload,created FROM codex_usage_cache WHERE id=1').fetchone()
            quota = json.loads(usage['payload']) if usage else None
            if quota is not None:
                quota = {'age_seconds': round(max(0, time.time() - usage['created'])), 'snapshot': quota}
        if latest_progress is not None:
            encoded = json.dumps(latest_progress.get('result', {}), ensure_ascii=False)
            if len(encoded) > 16000:
                latest_progress['result'] = {'preview': encoded[:16000], 'truncated': True,
                                             'note': '完整快照可codex status加job_id读取。'}
        result = {'thread_id': thread, 'online': online, 'jobs': jobs,
                  'latest_progress': latest_progress, 'quota': quota,
                  'note': '这是带时间的本地快照；请求done不代表任务完成，任务结果以进展正文和终态证据为准。没有快照表示尚未知晓。'}
        if args.get('refresh'):
            if not thread:
                raise ValueError('尚未绑定Codex会话，请先选择真实thread_id')
            result['refresh_request'] = self.r.codex.call({'action': 'read', 'thread_id': thread, 'refresh': True}, context, chat)
        return result


def main():
    """CLI uses the same local MCP facade; tokens stay out of command arguments."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('tool', choices=sorted(NAMES))
    parser.add_argument('--arguments', default='{}', help='JSON参数对象')
    args = parser.parse_args()
    settings = json.loads(Path('/etc/picoclaw-memory/settings.json').read_text())
    runtime = json.loads(Path('/etc/picoclaw-memory/runtime.json').read_text())
    arguments = json.loads(args.arguments)
    arguments['_context'] = {'chat_id': str(runtime['owner_id']), 'session_key': 'common-workflows-cli'}
    token = Path(settings['local_token_file']).read_text().strip()
    body = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
            'params': {'name': args.tool, 'arguments': arguments}}
    request = urllib.request.Request('http://127.0.0.1:18792/mcp', json.dumps(body).encode(),
                                     {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=120) as response:
        result = json.load(response)
    if 'error' in result:
        parser.exit(1, json.dumps(result['error'], ensure_ascii=False) + '\n')
    print(json.dumps(result['result'], ensure_ascii=False))
    if result['result'].get('isError'):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
