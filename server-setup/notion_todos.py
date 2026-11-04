"""Read one Notion checklist into an atomic, timestamped local snapshot."""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import tempfile
import time
import urllib.parse

from notion_reader import NotionReader, identifier, rich

CONFIG_FILE = '/etc/picoclaw-memory/my-todos.json'
SNAPSHOT_FILE = '/var/lib/picoclaw-readers/context/my-todos.json'


def utc_now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.my-todos-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
        os.chmod(temporary, 0o640)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class TodoSnapshot:
    def __init__(self, config_file=CONFIG_FILE, reader=None):
        self.config_file = Path(config_file)
        self.config = json.loads(self.config_file.read_text(encoding='utf-8'))
        self.page_id = identifier(self.config['page_id'])
        self.path = Path(self.config.get('snapshot_file', SNAPSHOT_FILE))
        self.reader = reader or NotionReader(self.config.get('token_file', '/etc/picoclaw-memory/notion.token'))

    def read(self):
        if not self.path.exists():
            return {'status': 'unavailable', 'source': 'notion', 'items': [],
                    'note': '待办尚未同步，不能据此判断没有待办。'}
        result = json.loads(self.path.read_text(encoding='utf-8'))
        if result.get('page_id') != self.page_id:
            raise ValueError('待办缓存来源与配置不一致，请刷新')
        at = dt.datetime.fromisoformat(result['synced_at']).timestamp() if result.get('synced_at') else 0
        result['age_seconds'] = round(max(0, time.time() - at)) if at else None
        result['stale'] = result.get('status') != 'ok' or not at or time.time() - at > 600
        return result

    def refresh(self):
        items, visited, block_count = [], set(), 0
        started = time.monotonic()

        def children(parent, ancestors=()):
            nonlocal block_count
            if parent in visited:
                return
            visited.add(parent)
            cursor = None
            while True:
                if time.monotonic() - started > 90:
                    raise ValueError('待办同步超时，保留上次完整快照')
                query = {'page_size': 100}
                if cursor:
                    query['start_cursor'] = cursor
                page = self.reader.request('GET', 'blocks/' + parent + '/children?' + urllib.parse.urlencode(query))
                for block in page.get('results', []):
                    block_count += 1
                    if block_count > 2000:
                        raise ValueError('待办页面超过2000个区块，保留上次完整快照')
                    kind, block_id = block.get('type'), block.get('id')
                    body = block.get(kind) or {}
                    text = rich(body.get('rich_text', []))
                    path = ancestors
                    if kind == 'to_do':
                        # Parent and child checkboxes keep their own actual states.
                        if not body.get('checked') and text.strip():
                            items.append({'id': block_id, 'text': text, 'parents': list(ancestors),
                                          'url': 'https://www.notion.so/' + self.page_id.replace('-', '') + '#' + block_id.replace('-', '')})
                        if text.strip():
                            path = ancestors + (text,)
                    elif kind in ('heading_1', 'heading_2', 'heading_3', 'toggle') and text.strip():
                        path = ancestors + (text,)
                    if block.get('has_children') and kind not in ('child_page', 'child_database', 'link_to_page'):
                        children(block_id, path)
                if not page.get('has_more'):
                    break
                next_cursor = page.get('next_cursor')
                if not next_cursor or next_cursor == cursor:
                    raise ValueError('Notion分页未完成，保留上次完整快照')
                cursor = next_cursor

        attempted_at = utc_now()
        try:
            page = self.reader.request('GET', 'pages/' + self.page_id)
            if page.get('in_trash') or page.get('archived'):
                raise ValueError('待办页面已归档，保留上次完整快照')
            children(self.page_id)
            result = {'status': 'ok', 'source': 'notion', 'page_id': self.page_id,
                      'source_url': 'https://www.notion.so/' + self.page_id.replace('-', ''),
                      'synced_at': utc_now(), 'last_attempt_at': attempted_at,
                      'page_edited_at': page.get('last_edited_time'), 'items': items,
                      'pending_count': len(items), 'blocks_read': block_count}
        except Exception as error:
            # A failed read must never turn the last known checklist into an empty one.
            result = self.read()
            result.pop('age_seconds', None)
            result.pop('stale', None)
            result.update(status='sync_failed', page_id=self.page_id, last_attempt_at=attempted_at,
                          error=str(error) if isinstance(error, ValueError) else type(error).__name__)
            atomic_json(self.path, result)
            raise ValueError(result['error']) from None
        atomic_json(self.path, result)
        return self.read()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default=CONFIG_FILE)
    args = parser.parse_args()
    try:
        result = TodoSnapshot(args.config).refresh()
        print(json.dumps({key: result.get(key) for key in ('status', 'synced_at', 'pending_count', 'blocks_read')}, ensure_ascii=False))
    except (ValueError, OSError) as error:
        parser.exit(1, str(error) + '\n')


if __name__ == '__main__':
    main()
