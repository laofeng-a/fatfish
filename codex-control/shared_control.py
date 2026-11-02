"""Local-only human interface to the same AppServer used by the Telegram bridge."""
import hashlib
import hmac
import http.server
import json
import secrets
import threading
import os
import re
from pathlib import Path


class SharedControl:
    def __init__(self,app=None):
        self.app=app
        self.lock=threading.RLock()
        self.requests={}
        self.bridge_online=False
        self.phone_access=None

    def set_app(self,app):
        with self.lock:self.app=app

    def require_app(self):
        if self.app is None:raise ConnectionError('Codex 正在连接，页面会自动重试。')

    def execute(self,job,wait_callback=None):
        with self.lock:
            self.require_app()
            self.app.wait_callback=wait_callback
            if wait_callback and job.get('action')=='send' and not job.get('auto_reply'):
                job=dict(job,text='[大肥鱼转达 jg]\n'+job['text'])
            try:return self.app.execute(job)
            finally:self.app.wait_callback=None

    def call(self,args):
        action=args.get('action')
        if action=='status':
            # A large thread read must not block the local liveness endpoint.
            app=self.app
            return {'engine_ready':app is not None,'bridge_online':self.bridge_online,'active_turns':len(app.active) if app else 0}
        with self.lock:
            if action=='phone_status':
                return self.phone_access.public_status() if self.phone_access else {'state':'disabled'}
            self.require_app()
            if action=='list':
                result=self.app.execute({'action':'list','query':str(args.get('query',''))[:120]})
                result['default_thread_id']=next((row['thread_id'] for row in reversed(list(self.app.routes.values())) if row.get('shared')),None)
                return result
            if action=='usage':return self.app.execute({'action':'usage'})
            identifier=args.get('thread_id')
            if not isinstance(identifier,str) or not 12<=len(identifier)<=80 or not all(c.isalnum() or c in '_-' for c in identifier):
                raise ValueError('请选择真实会话')
            if action=='attach':return self.app.attach_shared(identifier)
            actual=self.app.routes.get(identifier,{}).get('thread_id',identifier)
            if action=='history':
                params={'threadId':actual,'limit':5,'sortDirection':'desc','itemsView':'full'}
                if args.get('cursor'):params['cursor']=str(args['cursor'])[:1000]
                page=self.app.rpc('thread/turns/list',params)
                turns=[];remaining_core=120000;remaining_tools=40000
                for turn in page.get('data',[]):
                    messages=[];tool_count=0;omitted=0
                    for item in turn.get('items',[]):
                        kind=item.get('type','unknown')
                        if kind=='agentMessage':text=str(item.get('text',''))
                        elif kind=='userMessage':text='\n'.join(str(x.get('text','')) for x in item.get('content',[]) if isinstance(x,dict))
                        else:
                            if tool_count>=20 or remaining_tools<=0:omitted+=1;continue
                            text=json.dumps(item,ensure_ascii=False);tool_count+=1
                        core=kind in ('agentMessage','userMessage')
                        limit=min(24000,remaining_core) if core else min(4000,remaining_tools)
                        messages.append({'type':kind,'text':text[:limit],'truncated':len(text)>limit})
                        if core:remaining_core-=min(len(text),limit)
                        else:remaining_tools-=min(len(text),limit)
                    turns.append({'id':turn.get('id'),'status':turn.get('status'),'messages':messages,'error':turn.get('error'),'omitted_tool_items':omitted})
                metadata=self.app.rpc('thread/read',{'threadId':actual})['thread']
                return {'thread_id':actual,'source_thread_id':identifier if identifier!=actual else None,'name':metadata.get('name'),
                    'status':metadata.get('status'),'turns':turns,'next_cursor':page.get('nextCursor'),
                    'native':any(row.get('native') and row['thread_id']==actual for row in self.app.routes.values())}
            if action not in ('send','stop'):raise ValueError('不支持的操作')
            if actual not in {row['thread_id'] for row in self.app.routes.values()}:
                raise ValueError('先点击“设为共用会话”，再发送或停止')
            key=args.get('request_id')
            if not isinstance(key,str) or not 8<=len(key)<=100:raise ValueError('缺少请求编号')
            job={'action':action,'thread_id':actual,'id':'control-'+key}
            if action=='send':
                text=args.get('text')
                if not isinstance(text,str) or not text.strip() or len(text)>12000:raise ValueError('请输入1到12000字')
                job['text']='[jg · 共用控制页]\n'+text
            fingerprint=hashlib.sha256(json.dumps(job,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
            if key in self.requests:
                cached=self.requests[key]
                if cached['fingerprint']!=fingerprint:raise ValueError('请求编号已用于其他内容')
                if cached.get('error'):raise ValueError(cached['error'])
                return cached['result']
            # Record uncertainty too: retrying a timed-out submission must not execute it twice.
            try:result=self.execute(job)
            except Exception:
                self.requests[key]={'fingerprint':fingerprint,'error':'本次提交未取得确认；请先查看记录，勿重复发送。'}
                raise
            else:self.requests[key]={'fingerprint':fingerprint,'result':result}
            finally:
                while len(self.requests)>128:self.requests.pop(next(iter(self.requests)))
            return result


def start_control(control,state_file,port=0):
    root=Path(__file__).parent
    state_file=Path(state_file)
    try:previous=json.loads(state_file.read_text(encoding='utf-8'))
    except (OSError,ValueError):previous={}
    if not isinstance(previous,dict):previous={}
    # Preserve this local capability so an already-open tab reconnects after restart.
    token=previous.get('token','')
    if not isinstance(token,str) or not re.fullmatch(r'[A-Za-z0-9_-]{30,80}',token):token=secrets.token_urlsafe(32)
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def send(self,status,value,mime='application/json; charset=utf-8'):
            data=json.dumps(value,ensure_ascii=False).encode() if mime.startswith('application/json') else value
            self.send_response(status)
            self.send_header('Content-Type',mime);self.send_header('Content-Length',str(len(data)))
            self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers();self.wfile.write(data)
        def valid_host(self):return self.headers.get('Host')=='127.0.0.1:'+str(self.server.server_port)
        def do_GET(self):
            if not self.valid_host():self.send(403,{'error':'Local access only'});return
            if self.path=='/healthz':
                self.send(200,{'service':'fatfish-control','pid':os.getpid(),'page_online':True});return
            if self.path=='/bootstrap':self.send(200,{'mode':'local'});return
            if self.path=='/favicon.svg':
                self.send(200,b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 40 40"><rect width="40" height="40" rx="10" fill="#eeece5"/><path d="M7 20c4-8 14-11 21-6l6-4-1 10 1 10-6-4C21 31 11 28 7 20Z" fill="none" stroke="#ac533a" stroke-width="2" stroke-linejoin="round"/><circle cx="16" cy="18" r="1.6" fill="#ac533a"/></svg>','image/svg+xml');return
            files={'/':('control.html','text/html; charset=utf-8'),'/control.js':('control.js','text/javascript; charset=utf-8'),'/control.css':('control.css','text/css; charset=utf-8')}
            item=files.get(self.path)
            if not item:self.send(404,{'error':'Not found'});return
            self.send(200,(root/item[0]).read_bytes(),item[1])
        def do_POST(self):
            origin='http://127.0.0.1:'+str(self.server.server_port)
            if not self.valid_host() or self.headers.get('Origin',origin)!=origin:
                self.send(403,{'error':'Local access only'});return
            if self.path!='/api' or not hmac.compare_digest(self.headers.get('Authorization',''),'Bearer '+token):
                self.send(401,{'error':'请通过本机打开脚本进入控制页'});return
            try:
                length=int(self.headers.get('Content-Length','0'))
                if not 0<length<=65536 or self.headers.get('Content-Type','').split(';',1)[0]!='application/json':raise ValueError('Invalid request')
                args=json.loads(self.rfile.read(length))
                if not isinstance(args,dict):raise ValueError('Invalid request')
                result=control.call(args)
                raw=json.dumps(result,ensure_ascii=False).encode()
                if len(raw)>2*1024*1024:raise ValueError('本页记录太大，请缩小范围')
                self.send(200,{'result':result})
            except Exception as error:self.send(503 if isinstance(error,ConnectionError) else 400,{'error':str(error)[:600]})
        def do_OPTIONS(self):self.send(405,{'error':'Not allowed'})
    class Server(http.server.ThreadingHTTPServer):
        daemon_threads=True
        request_queue_size=4
        slots=threading.BoundedSemaphore(4)
        def process_request(self,request,address):
            if not self.slots.acquire(blocking=False):self.shutdown_request(request);return
            try:super().process_request(request,address)
            except Exception:self.slots.release();raise
        def process_request_thread(self,request,address):
            try:super().process_request_thread(request,address)
            finally:self.slots.release()
    server=Server(('127.0.0.1',port),Handler)
    state={'url':'http://127.0.0.1:'+str(server.server_port)+'/','token':token,'pid':os.getpid()}
    pending=state_file.with_suffix('.pending');pending.write_text(json.dumps(state),encoding='utf-8');os.replace(pending,state_file)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    return server
