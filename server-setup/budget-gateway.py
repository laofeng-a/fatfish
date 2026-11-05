"""Bounded text/image OpenAI gateway. Reserve before every paid call.

No streaming, native search, external image URLs or automatic upstream retries.
The ledger is an application estimate, not the provider's live billing record.
"""
import datetime as dt
import base64
import hmac
import http.server
import json
import math
import os
import re
from pathlib import Path
import sqlite3
import ssl
import socket
import time
import threading
import uuid
import urllib.error
import urllib.parse
import urllib.request
from contextlib import closing
from activity_budget import ActivityLedger
from spending_control import limits_enabled

MAX_BODY = 6 * 1024 * 1024
MAX_IMAGE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_IMAGE_BYTES = 3 * 1024 * 1024
MAX_IMAGES = 4
MAX_OUTPUT = 8192
# Deliberately above the official DeepSeek image budget; never count base64 as text.
IMAGE_TOKEN_RESERVE = 16384
MODEL = 'deepseek-v4.1-flash'
# CNY per million, worst listed regional peak price checked 2026-10-21.
INPUT_PRICE = 2.188
OUTPUT_PRICE = 8.75
DAILY_MICROYUAN = 9_500_000  # Keep 0.50 yuan/day outside the model allocation.
PRICE_VALID_UNTIL = '2026-11-20'
UTC8 = dt.timezone(dt.timedelta(hours=8))


class Denied(Exception):
    status = 400
    code = 'invalid_request'


class ContextOverflow(Denied):
    status = 413
    code = 'context_length_exceeded'


class BudgetDenied(Denied):
    status = 402
    code = 'local_budget_limit'


class UpstreamFailure(Exception):
    def __init__(self, status, kind, detail, upstream_status=None, request_id='', upstream_code='', body_summary=''):
        super().__init__(detail)
        self.status=status;self.kind=kind;self.upstream_status=upstream_status;self.request_id=request_id
        self.upstream_code=upstream_code;self.body_summary=body_summary


def upstream_retryable(status, code='', summary=''):
    if status == 403:
        try:
            envelope=json.loads(summary)
            error=envelope.get('error',{}) if isinstance(envelope,dict) else {}
            if isinstance(error,dict):
                code=code or error.get('code','')
                summary=str(error.get('message',''))
        except (ValueError,TypeError):pass
        text=(str(code)+' '+str(summary)).lower()
        if 'invalid_request' in str(code).lower():return False
        permanent=('content_filter','content_policy','data_inspection_failed','safety_violation',
                   'inappropriate_content','content_blocked','content_moderation','invalid_parameter',
                   'invalidparameter','invalid_argument','bad_request','invalid parameter','invalid argument',
                   '内容违规','内容审核未通过','参数错误')
        return not any(marker in text for marker in permanent)
    return status in (408,429) or status >= 500


def fetch_upstream(opener, request):
    """One attempt only. The harness owns retry/backoff for the whole turn."""
    try:
        with opener.open(request, timeout=90) as response:
            payload=response.read(2_000_001)
        if len(payload)>2_000_000:raise UpstreamFailure(502,'invalid_response','Upstream response exceeds size bound',200)
        result=json.loads(payload)
        if not isinstance(result,dict) or not isinstance(result.get('choices'),list) or not result['choices']:
            raise UpstreamFailure(502,'invalid_response','Upstream returned no completion choices',200)
        return result
    except urllib.error.HTTPError as exc:
        request_id=next((exc.headers.get(name) for name in ('x-request-id','x-dashscope-request-id','request-id') if exc.headers and exc.headers.get(name)),'')
        status=exc.code if 400<=exc.code<=599 else 502
        detail=str(exc);kind='http_error';code='';summary=''
        try:
            raw_body=exc.read(8192)
            summary=raw_body.decode('utf-8',errors='replace')[:512]
            body=json.loads(raw_body);error=body.get('error',{}) if isinstance(body,dict) else {}
            if isinstance(error,dict):
                code=str(error.get('code',''))
                detail+=' '+code+' '+str(error.get('message',''))
        except (ValueError,OSError):pass
        if status==400 and any(s in detail.lower() for s in ('context_length_exceeded','maximum context length','context window','prompt is too long')):kind='context_length_exceeded'
        exc.close()
        raise UpstreamFailure(status,kind,detail,status,str(request_id)[:160],code,summary) from None
    except (TimeoutError,socket.timeout) as exc:
        raise UpstreamFailure(504,'timeout',str(exc)) from None
    except urllib.error.URLError as exc:
        kind='timeout' if isinstance(exc.reason,(TimeoutError,socket.timeout)) else 'network_error'
        raise UpstreamFailure(504 if kind=='timeout' else 503,kind,str(exc)) from None
    except (json.JSONDecodeError,UnicodeDecodeError) as exc:
        raise UpstreamFailure(502,'invalid_response',str(exc),200) from None
    except OSError as exc:
        raise UpstreamFailure(503,'network_error',str(exc)) from None


def refresh_runtime_clock(data, tail_context=False):
    """Refresh on every model call, including tool continuations and background turns."""
    messages=data.get('messages')
    if not isinstance(messages,list) or not messages:return data
    now=dt.datetime.now(UTC8)
    weekday='一二三四五六日'[now.weekday()]
    clock=('[fatfish_runtime_clock]\n'
           '当前日期时间：'+now.isoformat(timespec='seconds')+'（星期'+weekday+'）\n'
           '时区：UTC+08:00，jg当地时间。每次模型调用前刷新，今天、明天、昨天按此理解。\n'
           '用途：时间附注，用于理解日期、时刻与相对时间；本轮处理事项依据真实消息及事件来源。\n'
           '[/fatfish_runtime_clock]')
    result=dict(data);result['messages']=[dict(m) if isinstance(m,dict) else m for m in messages]
    if tail_context:
        # DeepSeek reuses exact input prefixes. A changing system clock before
        # the history discards that whole reusable suffix. Keep a fresh, equally
        # authoritative system clock after the history instead, including tool
        # continuations. This transforms only the outbound request, not history.
        cleaned=[]
        for message in result['messages']:
            if not isinstance(message,dict) or message.get('role')!='system':
                cleaned.append(message);continue
            def stable_text(text):
                text=re.sub(r'\n*\[fatfish_runtime_clock\].*?\[/fatfish_runtime_clock\]','',text,flags=re.S)
                # Remove only PicoClaw's exact generated minute-clock header.
                return re.sub(r'(?m)^## Current Time\n\d{4}-\d{2}-\d{2} \d{2}:\d{2} \([A-Za-z]+\)\n\n(?=## Runtime\n)','',text).rstrip()
            content=message.get('content')
            if isinstance(content,str):
                message['content']=stable_text(content)
                if message['content']:cleaned.append(message)
            elif isinstance(content,list):
                blocks=[]
                for block in content:
                    if isinstance(block,dict) and block.get('type')=='text' and isinstance(block.get('text'),str):
                        block={**block,'text':stable_text(block['text'])}
                        if not block['text']:continue
                    blocks.append(block)
                if blocks:message['content']=blocks;cleaned.append(message)
            else:cleaned.append(message)
        result['messages']=[*cleaned,{'role':'system','content':clock}]
        return result
    system=next((m for m in result['messages'] if isinstance(m,dict) and m.get('role')=='system'),None)
    if system is None:result['messages'].insert(0,{'role':'system','content':clock})
    elif isinstance(system.get('content'),str):
        content=re.sub(r'\n*\[fatfish_runtime_clock\].*?\[/fatfish_runtime_clock\]','',system['content'],flags=re.S)
        system['content']=content.rstrip()+'\n\n'+clock
    elif isinstance(system.get('content'),list):
        blocks=[b for b in system['content'] if not (isinstance(b,dict) and b.get('type')=='text' and str(b.get('text','')).startswith('[fatfish_runtime_clock]'))]
        system['content']=[*blocks,{'type':'text','text':clock}]
    else:system['content']=clock
    return result


def prepare_request(data, provider='bailian'):
    if data.get('model') != MODEL or data.get('stream') is True:
        raise Denied('Only the configured model and non-streaming requests are supported')
    if not isinstance(data.get('messages'), list) or not data['messages']:
        raise Denied('messages must be a nonempty array')
    text_bytes = 0
    image_count = 0
    image_bytes = 0
    for message in data['messages']:
        if not isinstance(message, dict):
            raise Denied('Invalid message')
        content = message.get('content')
        if isinstance(content, str):
            text_bytes += len(content.encode())
        elif content is not None:
            if not isinstance(content, list) or len(content) > 12:
                raise Denied('Invalid multimodal content')
            for block in content:
                if not isinstance(block, dict):
                    raise Denied('Invalid content block')
                if block.get('type') == 'text' and isinstance(block.get('text'), str):
                    text_bytes += len(block['text'].encode())
                elif block.get('type') == 'image_url' and message.get('role') == 'user':
                    value = block.get('image_url')
                    address = value.get('url') if isinstance(value, dict) else None
                    if not isinstance(address, str) or len(address) > MAX_IMAGE_BYTES * 4 // 3 + 128:
                        raise Denied('Invalid image size')
                    prefix, separator, encoded = address.partition(',')
                    if not separator or prefix not in ('data:image/jpeg;base64', 'data:image/png;base64',
                                                         'data:image/webp;base64', 'data:image/gif;base64'):
                        raise Denied('Images must be bounded inline JPEG/PNG/WebP/GIF data')
                    try:
                        decoded = base64.b64decode(encoded, validate=True)
                    except (ValueError, TypeError):
                        raise Denied('Invalid image encoding') from None
                    size = len(decoded)
                    valid = (decoded.startswith(b'\xff\xd8\xff') or decoded.startswith(b'\x89PNG\r\n\x1a\n')
                             or decoded.startswith((b'GIF87a', b'GIF89a'))
                             or (decoded.startswith(b'RIFF') and decoded[8:12] == b'WEBP'))
                    if not valid or not 0 < size <= MAX_IMAGE_BYTES:
                        raise Denied('Invalid image content or size')
                    image_count += 1
                    image_bytes += size
                    del decoded
                else:
                    raise Denied('Unsupported content block or role')
        text_bytes += len(json.dumps({k: v for k, v in message.items() if k != 'content'}, ensure_ascii=False).encode())
    if image_count > MAX_IMAGES or image_bytes > MAX_TOTAL_IMAGE_BYTES:
        raise Denied('At most four images and 3 MiB of image data per request')
    text_bytes += len(json.dumps(data.get('tools', []), ensure_ascii=False).encode())
    # The harness and upstream model own token-context management. UTF-8 bytes
    # are retained here for accounting, not used as a second context ceiling.
    # Forward only fields whose cost/shape this gate supports.
    allowed = {'messages', 'tools', 'tool_choice', 'temperature', 'top_p', 'stop', 'enable_thinking', 'thinking',
               'frequency_penalty', 'presence_penalty', 'response_format', 'reasoning_effort'}
    result = {k: v for k, v in data.items() if k in allowed}
    maximum = data.get('max_completion_tokens', data.get('max_tokens', MAX_OUTPUT))
    if not isinstance(maximum, int) or isinstance(maximum, bool) or not 1 <= maximum <= MAX_OUTPUT:
        raise Denied('max_tokens must be between 1 and 8192')
    if data.get('reasoning_effort') not in (None, 'low', 'high', 'max'):
        raise Denied('Invalid reasoning effort')
    result.update(model=MODEL, max_tokens=maximum, stream=False, n=1, enable_search=False)
    if provider == 'deepseek':
        # Official DeepSeek uses thinking.type, not Bailian's enable_thinking.
        enabled = result.pop('enable_thinking', True)
        result.setdefault('thinking', {'type': 'enabled' if enabled else 'disabled'})
        result.pop('enable_search', None)
    raw = json.dumps(result, ensure_ascii=False, separators=(',', ':')).encode()
    if len(raw) > MAX_BODY:
        raise ContextOverflow('Request too large: gateway transport body exceeds 6 MiB')
    # Deliberately generous allowance for text tokenization, role framing and tools.
    input_allowance = text_bytes * 4 + 8192 + image_count * IMAGE_TOKEN_RESERVE
    reserve = math.ceil(input_allowance * INPUT_PRICE + maximum * OUTPUT_PRICE)
    return raw, reserve, input_allowance, maximum


class Ledger:
    def __init__(self, path):
        self.path = str(path)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS calls (id INTEGER PRIMARY KEY, day TEXT NOT NULL, reserved INTEGER NOT NULL, charged INTEGER NOT NULL, settled INTEGER NOT NULL DEFAULT 0)')
            db.execute('CREATE TABLE IF NOT EXISTS state (name TEXT PRIMARY KEY, value TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS call_events (id INTEGER PRIMARY KEY,call_id INTEGER,created REAL,request_id TEXT,phase TEXT,upstream_status INTEGER,error_class TEXT,detail TEXT,elapsed_ms INTEGER)')
            db.execute('CREATE TABLE IF NOT EXISTS cache_usage (call_id INTEGER PRIMARY KEY,created REAL,model TEXT,prompt_tokens INTEGER,hit_tokens INTEGER,miss_tokens INTEGER,elapsed_ms INTEGER)')

    def cache_usage(self,call_id,model,usage,elapsed_ms):
        if not isinstance(usage,dict):return
        prompt=usage.get('prompt_tokens');hit=usage.get('prompt_cache_hit_tokens')
        details=usage.get('prompt_tokens_details')
        if hit is None and isinstance(details,dict):hit=details.get('cached_tokens')
        if type(prompt) is not int or type(hit) is not int or not 0<=hit<=prompt:return
        with closing(sqlite3.connect(self.path,timeout=10)) as db,db:
            db.execute('INSERT OR REPLACE INTO cache_usage VALUES(?,?,?,?,?,?,?)',
                       (call_id,time.time(),model,prompt,hit,prompt-hit,elapsed_ms))

    def event(self,call_id,request_id,phase,upstream_status=None,error_class='',detail='',elapsed_ms=0):
        with closing(sqlite3.connect(self.path,timeout=10)) as db,db:
            db.execute('INSERT INTO call_events(call_id,created,request_id,phase,upstream_status,error_class,detail,elapsed_ms) VALUES(?,?,?,?,?,?,?,?)',
                       (call_id,time.time(),request_id,phase,upstream_status,error_class,detail[:1200],elapsed_ms))

    def reserve(self, day, amount, limit=DAILY_MICROYUAN):
        with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
            db.execute('BEGIN IMMEDIATE')
            if limits_enabled() and db.execute("SELECT 1 FROM state WHERE name='halted'").fetchone():
                raise BudgetDenied('Spending gate is paused for manual review')
            used = db.execute('SELECT COALESCE(SUM(charged),0) FROM calls WHERE day=?', (day,)).fetchone()[0]
            if limits_enabled() and used + amount > limit:
                raise BudgetDenied('Daily model allowance reached; retry after the next UTC+8 day')
            return db.execute('INSERT INTO calls(day,reserved,charged) VALUES(?,?,?)', (day, amount, amount)).lastrowid

    def settle(self, call_id, usage, input_limit, output_limit):
        # Missing/invalid usage: keep the reservation, including on network errors.
        if not isinstance(usage, dict):
            return False
        incoming, outgoing = usage.get('prompt_tokens'), usage.get('completion_tokens')
        if any(type(x) is not int or x < 0 for x in (incoming, outgoing)):
            return False
        charge = math.ceil(incoming * INPUT_PRICE + outgoing * OUTPUT_PRICE)
        exceeded = incoming > input_limit or outgoing > output_limit
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('UPDATE calls SET charged=?, settled=1 WHERE id=?', (charge, call_id))
            if exceeded:
                db.execute("INSERT OR REPLACE INTO state(name,value) VALUES('halted','usage exceeded reservation')")
        if exceeded and limits_enabled():
            raise BudgetDenied('Provider usage exceeded the configured reservation; manual review required')
        return True


class BoundedModelServer(http.server.ThreadingHTTPServer):
    """Allow a chat request alongside one background summary, with bounded RAM."""
    daemon_threads=True
    request_queue_size=8
    def __init__(self,*args,**kwargs):
        self.slots=threading.BoundedSemaphore(2)
        super().__init__(*args,**kwargs)
    def process_request(self,request,address):
        self.slots.acquire()
        try:super().process_request(request,address)
        except Exception:self.slots.release();raise
    def process_request_thread(self,request,address):
        try:super().process_request_thread(request,address)
        finally:self.slots.release()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def validated_upstream(base):
    url = urllib.parse.urlsplit(base)
    legacy = {'dashscope.aliyuncs.com', 'dashscope-intl.aliyuncs.com', 'dashscope-us.aliyuncs.com'}
    host = url.hostname or ''
    official = ((host in legacy or host.endswith('.maas.aliyuncs.com'))
                and url.path.rstrip('/') == '/compatible-mode/v1')
    official = official or (host == 'api.deepseek.com' and url.path.rstrip('/') in ('', '/v1'))
    if (url.scheme != 'https' or url.port not in (None, 443) or url.username or url.password
            or url.query or url.fragment or not official):
        raise Denied('Upstream must be an official Bailian or DeepSeek HTTPS base URL')
    return base.rstrip('/') + '/chat/completions'


def configure_upstream(settings):
    global MODEL, INPUT_PRICE, OUTPUT_PRICE
    upstream = validated_upstream(settings['upstream_base_url'])
    provider = 'deepseek' if urllib.parse.urlsplit(upstream).hostname == 'api.deepseek.com' else 'bailian'
    MODEL = 'deepseek-flash' if provider == 'deepseek' else 'deepseek-v4.1-flash'
    # Conservative peak, uncached estimates, not the provider's live invoice.
    # DeepSeek official CNY rates checked 2026-10-29; existing gates stay as configured.
    INPUT_PRICE, OUTPUT_PRICE = (2.0, 8.0) if provider == 'deepseek' else (2.188, 8.75)
    return upstream, provider


def serve(settings_path):
    settings = json.loads(Path(settings_path).read_text())
    upstream, provider = configure_upstream(settings)
    upstream_key = Path(settings['upstream_key_file']).read_text().strip()
    local_token = Path(settings['local_token_file']).read_text().strip()
    if not upstream_key or not local_token:
        raise Denied('Credentials are not configured')
    ledger = Ledger(settings['ledger_file'])
    activity = ActivityLedger()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(),
                                        urllib.request.HTTPSHandler(context=ssl.create_default_context()))

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # No prompts, keys or responses in access logs.

        def send_json(self, status, body):
            raw = json.dumps(body).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self):
            call_id=None;request_id=uuid.uuid4().hex;started=time.monotonic();upstream_attempted=False
            def diagnostic(phase,status=None,kind='',detail=''):
                safe=str(detail).replace(upstream_key,'[redacted]').replace(local_token,'[redacted]')[:1200]
                elapsed=int((time.monotonic()-started)*1000)
                ledger.event(call_id,request_id,phase,status,kind,safe,elapsed)
                print(json.dumps({'at':dt.datetime.now(UTC8).isoformat(timespec='seconds'),'call_id':call_id,'request_id':request_id,
                                 'phase':phase,'upstream_status':status,'error_class':kind,'detail':safe,'elapsed_ms':elapsed},ensure_ascii=False),flush=True)
            try:
                if self.path != '/v1/chat/completions':
                    self.send_json(404, {'error': {'message': 'Unsupported endpoint'}})
                    return
                if not hmac.compare_digest(self.headers.get('Authorization', ''), 'Bearer ' + local_token):
                    self.send_json(401, {'error': {'message': 'Unauthorized'}})
                    return
                now = dt.datetime.now(UTC8)
                if limits_enabled() and (now.date().isoformat() > PRICE_VALID_UNTIL or now.date().isoformat() < '2026-10-21'):
                    raise BudgetDenied('Pricing snapshot expired or system date is invalid; review prices before resuming')
                length = int(self.headers.get('Content-Length', '0'))
                if length > MAX_BODY:
                    raise ContextOverflow('Request too large: gateway transport body exceeds 6 MiB')
                if length <= 0:
                    raise Denied('Invalid body size')
                raw, reserve, input_limit, output_limit = prepare_request(
                    refresh_runtime_clock(json.loads(self.rfile.read(length)),tail_context=(provider=='deepseek')), provider)
                call_id = ledger.reserve(now.date().isoformat(), reserve)
                activity_id = None
                if self.headers.get('X-Fatfish-Activity') == 'background':
                    try:
                        activity_id = activity.reserve(reserve, 'model')
                    except ValueError:
                        ledger.settle(call_id, {'prompt_tokens':0,'completion_tokens':0}, input_limit, output_limit)
                        raise BudgetDenied('Background exploration allowance reached') from None
                request = urllib.request.Request(upstream, data=raw, headers={
                    'Authorization': 'Bearer ' + upstream_key, 'Content-Type': 'application/json'})
                # Exactly one upstream attempt. Uncertain outcomes retain the reserve.
                diagnostic('upstream_start');upstream_attempted=True
                result=fetch_upstream(opener,request)
                settled=ledger.settle(call_id, result.get('usage'), input_limit, output_limit)
                usage = result.get('usage') or {}
                try:
                    ledger.cache_usage(call_id,MODEL,usage,int((time.monotonic()-started)*1000))
                except (OSError,sqlite3.Error):
                    pass  # Metrics never turn a successful response into a retry.
                if type(usage.get('prompt_tokens')) is int and type(usage.get('completion_tokens')) is int:
                    activity.settle(activity_id, math.ceil(usage['prompt_tokens']*INPUT_PRICE+usage['completion_tokens']*OUTPUT_PRICE))
                diagnostic('completed' if settled else 'completed_usage_unconfirmed',200)
                self.send_json(200, result)
            except UpstreamFailure as exc:
                diagnostic('upstream_failed_reservation_unresolved',exc.upstream_status,exc.kind,str(exc)+((' upstream_request_id='+exc.request_id) if exc.request_id else ''))
                summary=exc.body_summary.replace(upstream_key,'[redacted]').replace(local_token,'[redacted]')
                self.send_json(exc.status, {'error': {
                    'upstream_status':exc.upstream_status,'upstream_code':exc.upstream_code,
                    'retryable':upstream_retryable(exc.status,exc.upstream_code,summary),
                    'upstream_body':summary,'message':'Model service request failed',
                    'type':'upstream_error','code':exc.kind,'request_id':request_id}})
            except Denied as exc:
                diagnostic('local_rejection',kind=exc.code,detail=str(exc))
                self.send_json(exc.status, {'error': {'message':str(exc),'type':'invalid_request_error' if exc.status!=402 else 'local_budget_error',
                                                    'code':exc.code,'request_id':request_id,'retryable':False}})
            except (ValueError, TypeError, KeyError):
                diagnostic('invalid_data',kind='invalid_data')
                self.send_json(400, {'error': {'message':'Invalid request or upstream data','type':'invalid_request_error',
                                             'code':'invalid_request','request_id':request_id,'retryable':False}})
            except (BrokenPipeError,ConnectionResetError) as exc:
                diagnostic('client_disconnected',kind=type(exc).__name__,detail=str(exc))
            except Exception as exc:
                diagnostic('upstream_failed_reservation_unresolved' if upstream_attempted else 'gateway_failed',kind=type(exc).__name__,detail=str(exc))
                self.send_json(502, {'error': {'message':'Model gateway request failed','type':'gateway_error','request_id':request_id}})

    # One process, at most two in-flight requests. Ledger transactions retain
    # reservation ordering while a slow summary leaves capacity for chatting.
    server = BoundedModelServer(('127.0.0.1', 18791), Handler)
    server.timeout = 100
    server.serve_forever()


if __name__ == '__main__':
    import sys
    os.umask(0o077)
    serve(sys.argv[1] if len(sys.argv) > 1 else '/etc/picoclaw-budget/settings.json')
