"""On-demand Notion pages and blocks, using the existing connection token."""
import json
from pathlib import Path
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid

VERSION = '2026-03-11'
MAX_RESPONSE = 2 * 1024 * 1024
MAX_RESULT = 50000
WRITE_ACTIONS = {'create', 'append', 'update'}


class NotionAPIError(ValueError):
    def __init__(self, message, status):
        self.status = status
        super().__init__(message)


def rich_input(text):
    if not isinstance(text, str):
        raise ValueError('text/title应为文字')
    # Notion limits each rich-text element to 2,000 characters.
    return [{'type': 'text', 'text': {'content': text[i:i + 2000]}}
            for i in range(0, len(text), 2000)]


def input_children(args):
    if 'children' in args and 'text' in args:
        raise ValueError('正文用text或children其中一种')
    if 'children' in args:
        children = args['children']
        if not isinstance(children, list) or not all(isinstance(item, dict) for item in children):
            raise ValueError('children应为Notion区块对象数组')
        return children
    if 'text' in args:
        text = args['text']
        if not isinstance(text, str):
            raise ValueError('text应为文字')
        return [{'object': 'block', 'type': 'paragraph',
                 'paragraph': {'rich_text': rich_input(part)}}
                for part in re.split(r'\n\s*\n', text) if part.strip()]
    return []


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def identifier(value):
    if not isinstance(value, str):
        raise ValueError('需要真实的Notion页面或资料库编号/链接')
    if value.startswith('https://'):
        parsed = urllib.parse.urlsplit(value)
        if parsed.hostname not in ('notion.so', 'www.notion.so', 'notion.com', 'www.notion.com', 'app.notion.com') or parsed.username or parsed.password or parsed.port:
            raise ValueError('只接受Notion链接或真实编号')
        value = urllib.parse.unquote(parsed.path.rstrip('/').rsplit('/', 1)[-1])
        match = re.search(r'([a-fA-F0-9]{32}|[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12})$', value)
        value = match[1] if match else ''
    try:
        if not re.fullmatch(r'[a-fA-F0-9-]{32,36}', value):
            raise ValueError()
        return str(uuid.UUID(value))
    except ValueError:
        raise ValueError('Notion编号格式不正确；请用真实链接或编号') from None


def rich(items):
    return ''.join(str(item.get('plain_text', (item.get('text') or {}).get('content', ''))) for item in items or [])


def properties(raw):
    result = {}
    for name, prop in raw.items():
        kind = prop.get('type'); value = prop.get(kind)
        if kind in ('title', 'rich_text'):
            value = rich(value)
        elif kind in ('select', 'status'):
            value = value.get('name') if isinstance(value, dict) else value
        elif kind == 'multi_select':
            value = [item.get('name') for item in value or []]
        elif kind == 'people':
            value = [{'id': item.get('id'), 'name': item.get('name')} for item in value or []]
        elif kind in ('created_by', 'last_edited_by'):
            value = {'id': (value or {}).get('id')}
        result[name] = {'type': kind, 'value': value}
        if prop.get('has_more'):
            result[name]['has_more'] = True
    return result


def summary(item):
    result = {key: item[key] for key in ('object', 'id', 'url', 'parent', 'last_edited_time', 'in_trash', 'is_archived') if key in item}
    result['title'] = rich(item.get('title'))
    if item.get('object') == 'page':
        result['properties'] = properties(item.get('properties', {}))
        result['title'] = next((p['value'] for p in result['properties'].values() if p['type'] == 'title'), '')
    elif 'properties' in item:
        result['property_schema'] = {name: {'id': p.get('id'), 'type': p.get('type')} for name, p in item['properties'].items()}
    if 'data_sources' in item:
        result['data_sources'] = item['data_sources']
    if item.get('description'):
        result['description'] = rich(item['description'])
    return result


def block(item):
    kind = item.get('type', 'unsupported'); body = item.get(kind) or {}
    result = {'id': item.get('id'), 'type': kind, 'has_children': bool(item.get('has_children'))}
    if 'rich_text' in body:
        result['text'] = rich(body['rich_text'])
        # Preserve actual inline links, including ones with a custom label.
        links = [part.get('href') or (part.get('text') or {}).get('link', {}).get('url') for part in body['rich_text'] if part.get('href') or (part.get('text') or {}).get('link')]
        if links: result['links'] = links
    if 'caption' in body: result['caption'] = rich(body['caption'])
    for key in ('title', 'url', 'language', 'checked', 'expression', 'table_width', 'has_column_header', 'has_row_header', 'synced_from'):
        if key in body: result[key] = body[key]
    if 'cells' in body: result['cells'] = [rich(cell) for cell in body['cells']]
    if kind == 'link_to_page': result['target'] = body
    if kind in ('image', 'video', 'file', 'pdf', 'audio'):
        # Signed file URLs are not needed for textual note reading; don't fetch attachments.
        result['attachment'] = True
        result['attachment_not_read'] = True
    if kind == 'unsupported': result['not_read'] = True
    return result


class NotionReader:
    def __init__(self, token_file='/etc/picoclaw-memory/notion.token'):
        self.token_file = Path(token_file)
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def request(self, method, path, body=None):
        patterns = {
            'GET': r'(?:pages/[0-9a-f-]{36}|blocks/[0-9a-f-]{36}(?:/children(?:\?[^#]*)?)?|databases/[0-9a-f-]{36}|data_sources/[0-9a-f-]{36})',
            'POST': r'(?:search|pages|data_sources/[0-9a-f-]{36}/query)',
            'PATCH': r'(?:pages/[0-9a-f-]{36}|blocks/[0-9a-f-]{36}(?:/children)?)',
        }
        if method not in patterns or not re.fullmatch(patterns[method], path):
            raise ValueError('Notion接口路径不正确')
        try: token = self.token_file.read_text(encoding='utf-8').strip()
        except OSError: raise ValueError('Notion连接尚未配置') from None
        if not re.fullmatch(r'(?:ntn_|secret_)[A-Za-z0-9_-]{20,256}', token):
            raise ValueError('Notion连接令牌格式不正确')
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode()
        req = urllib.request.Request('https://api.notion.com/v1/' + path, data=data, method=method,
            headers={'Authorization': 'Bearer ' + token, 'Notion-Version': VERSION, 'Content-Type': 'application/json', 'Accept': 'application/json'})
        try:
            with self.opener.open(req, timeout=20) as response:
                raw = response.read(MAX_RESPONSE + 1)
            if len(raw) > MAX_RESPONSE:
                raise ValueError('Notion单页响应过大；请减小limit再读，不会自动遍历全部内容')
            result = json.loads(raw)
            if not isinstance(result, dict): raise ValueError('Notion返回了异常内容')
            return result
        except urllib.error.HTTPError as error:
            status = error.code
            messages = {401: 'Notion令牌无效或已撤销',
                403: 'Notion连接权限不足；写入需启用Insert content/Update content，并共享目标页面',
                404: 'Notion页面不存在，或尚未授权给此连接', 429: 'Notion暂时限流，请稍后再试'}
            raise NotionAPIError(messages.get(status, 'Notion操作失败') + '（HTTP ' + str(status) + '）', status) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            writing = method == 'PATCH' or (method == 'POST' and path == 'pages')
            detail = '写入结果尚未确认，可先读回目标再决定重试' if writing else '本次未读到内容'
            raise ValueError('Notion连接超时或网络不可用；' + detail) from None
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ValueError('Notion返回了异常内容') from None

    def call(self, args):
        allowed = {'action', 'id', 'query', 'cursor', 'limit', 'parent_id', 'parent_type',
                   'title', 'text', 'children', 'properties', 'target_type', 'block', 'filter', 'sorts'}
        if set(args) - allowed: raise ValueError('Notion不支持此参数')
        action = args.get('action')
        if {'filter', 'sorts'} & set(args):
            if action != 'query': raise ValueError('filter/sorts用于query的属性筛选与排序')
            if 'filter' in args and not isinstance(args['filter'], dict):
                raise ValueError('filter应为Notion原生筛选对象')
            if 'sorts' in args and (not isinstance(args['sorts'], list)
                                   or not all(isinstance(item, dict) for item in args['sorts'])):
                raise ValueError('sorts应为Notion原生排序对象数组')
        if action in WRITE_ACTIONS:
            return self.write(action, args)
        if action not in ('search', 'read', 'children', 'block', 'database', 'data_source', 'query'):
            raise ValueError('action需要search/read/children/block/database/data_source/query/create/append/update')
        limit = args.get('limit', 20)
        if type(limit) is not int or not 1 <= limit <= 50: raise ValueError('limit应为1到50')
        cursor = args.get('cursor')
        page = {'page_size': limit}
        if cursor:
            if not isinstance(cursor, str): raise ValueError('cursor应为上次响应的next_cursor字符串')
            # Pagination cursors are opaque; current query results can use s:...:... .
            page['start_cursor'] = cursor
        if action == 'search':
            query = args.get('query', '')
            if not isinstance(query, str) or len(query) > 200: raise ValueError('搜索词最多200字')
            raw = self.request('POST', 'search', dict(page, query=query, sort={'direction': 'descending', 'timestamp': 'last_edited_time'}))
            result = self.list_result(raw, summary)
            result['search_scope'] = 'title_only'
            result['note'] = '仅按标题搜索；无关键词时分页列出可见页面与数据源，不代表搜索过正文。'
        else:
            target = identifier(args.get('id'))
            if action == 'block':
                result = block(self.request('GET', 'blocks/' + target))
            if action in ('read', 'database', 'data_source'):
                namespace = {'read': 'pages', 'database': 'databases', 'data_source': 'data_sources'}[action]
                result = summary(self.request('GET', namespace + '/' + target))
                if action != 'read':
                    result.update(source='notion', untrusted_content=True)
                    return self.bounded(result)
            if action in ('read', 'children'):
                raw = self.request('GET', 'blocks/' + target + '/children?' + urllib.parse.urlencode(page))
                children = self.list_result(raw, block)
                if action == 'read': result['content'] = children
                else: result = dict(children, parent_id=target)
                result['note'] = '只返回这一层区块；has_children=true需用children继续读，表格/折叠块也一样；附件内容未读取。'
            elif action == 'query':
                body = dict(page)
                for name in ('filter', 'sorts'):
                    if name in args: body[name] = args[name]
                result = self.list_result(self.request('POST', 'data_sources/' + target + '/query', body), summary)
        result['source'] = 'notion'
        result['untrusted_content'] = True
        return self.bounded(result)

    def write(self, action, args):
        props = args.get('properties', {})
        if not isinstance(props, dict):
            raise ValueError('properties应为Notion属性对象')
        props = dict(props)
        if action == 'create':
            parent_type = args.get('parent_type', 'page')
            if parent_type not in ('page', 'data_source'):
                raise ValueError('parent_type需要page或data_source；资料库用database先取得data_sources')
            parent_id = identifier(args.get('parent_id'))
            children = input_children(args)
            if 'title' in args:
                name = 'title'
                if parent_type == 'data_source':
                    schema = self.request('GET', 'data_sources/' + parent_id)
                    name = next((key for key, value in schema.get('properties', {}).items()
                                 if value.get('type') == 'title'), None)
                    if not name:
                        raise ValueError('此数据源未找到标题字段；可传properties指定属性')
                props[name] = {'title': rich_input(args['title'])}
            if not props:
                raise ValueError('创建页面需要title或properties')
            body = {'parent': {parent_type + '_id': parent_id}, 'properties': props}
            if children: body['children'] = children
            result = summary(self.request('POST', 'pages', body))
        else:
            target = identifier(args.get('id'))
            if action == 'append':
                children = input_children(args)
                if not children:
                    raise ValueError('追加需要非空text或children')
                result = self.list_result(self.request('PATCH', 'blocks/' + target + '/children',
                                                      {'children': children}), block)
                result['parent_id'] = target
            elif args.get('target_type', 'page') == 'page':
                if 'title' in args:
                    page = self.request('GET', 'pages/' + target)
                    name = next((key for key, value in page.get('properties', {}).items()
                                 if value.get('type') == 'title'), 'title')
                    props[name] = {'title': rich_input(args['title'])}
                if not props: raise ValueError('更新页面需要title或properties；正文用append或更新block')
                result = summary(self.request('PATCH', 'pages/' + target, {'properties': props}))
            elif args.get('target_type') == 'block':
                body = args.get('block')
                if not isinstance(body, dict) or not body:
                    raise ValueError('更新区块需要block，例：{"to_do":{"checked":true}}')
                result = block(self.request('PATCH', 'blocks/' + target, body))
            else:
                raise ValueError('target_type需要page或block')
        result.update(source='notion', operation=action, status='saved', untrusted_content=True)
        # A large response must not turn a completed write into a failure/retry.
        if len(json.dumps(result, ensure_ascii=False).encode()) > MAX_RESULT:
            compact = {key: result[key] for key in ('source', 'operation', 'status', 'untrusted_content',
                                                   'object', 'id', 'url', 'parent_id') if key in result}
            if 'results' in result:
                compact['results'] = [{key: item[key] for key in ('id', 'type', 'has_children') if key in item}
                                      for item in result['results']]
            compact.update(content_omitted=True, note='写入已成功；正文用read/children/block按需读回。')
            return compact
        return self.bounded(result)

    @staticmethod
    def list_result(raw, convert):
        result = {'results': [convert(item) for item in raw.get('results', [])],
            'has_more': bool(raw.get('has_more')), 'next_cursor': raw.get('next_cursor')}
        if 'request_status' in raw: result['request_status'] = raw['request_status']
        return result

    @staticmethod
    def bounded(result):
        if len(json.dumps(result, ensure_ascii=False).encode()) > MAX_RESULT:
            raise ValueError('本页内容过长；请减小limit后继续读取，不会静默截掉正文')
        return result
