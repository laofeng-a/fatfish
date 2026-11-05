"""Loopback MCP facade for memory, personal tools and asynchronous work."""
import hmac
import http.server
import importlib.util
import json
import os
from pathlib import Path
import socket
import threading
import ssl
import urllib.error
import urllib.parse
import urllib.request
from tool_efficiency import ToolEfficiency, BATCH_TOOL, CACHE_TTLS
from runtime_policy import Runtime, TOOLS as PERSONAL_TOOLS
from second_brain_schedule import SecondBrainSchedule

ORIGIN = os.environ.get('FATFISH_SECOND_BRAIN_ORIGIN', 'https://second-brain.example.invalid')
SETTINGS = Path('/etc/picoclaw-memory/settings.json')
TOOLS = [
    {'name':'recall','description':'按主题查询Second Brain中的偏好、项目和旧决定；get展开全文，结合事件日期理解记录。',
     'inputSchema':{'type':'object','properties':{
         'query':{'type':'string','description':'明确主题的自然语言查询'},
         'topK':{'type':'integer','minimum':1,'maximum':20,'default':5},
         'tag':{'type':'string'},'project':{'type':'string'}},'required':['query'],'additionalProperties':False}},
    {'name':'get','description':'按已有id分段读取记忆，next_offset继续取回。',
     'inputSchema':{'type':'object','properties':{'id':{'type':'string'},'offset':{'type':'integer','minimum':0}},'required':['id'],'additionalProperties':False}},
    {'name':'list_recent','description':'查看Second Brain近期记录，带记录日期与同步信息。',
     'inputSchema':{'type':'object','properties':{'n':{'type':'integer','minimum':1,'maximum':20,'default':10},
         'tag':{'type':'string'},'project':{'type':'string'}},'additionalProperties':False}},
]
for tool in TOOLS:
    tool['annotations']={'readOnlyHint':True,'destructiveHint':False,'openWorldHint':False}
resource_spec=importlib.util.spec_from_file_location('personal_read_tools',Path(__file__).with_name('personal-read-tools.py'))
resources=importlib.util.module_from_spec(resource_spec)
resource_spec.loader.exec_module(resources)
TOOLS.extend(resources.TOOLS)
TOOLS.extend(PERSONAL_TOOLS)
TOOLS.append(BATCH_TOOL)
for item in TOOLS:
    if item['name'] in CACHE_TTLS:
        item['inputSchema']['properties']['refresh']={'type':'boolean','description':'需要重新读取时用true；默认复用仍有效的已读结果。'}
RESOURCE_NAMES={tool['name'] for tool in resources.TOOLS}
PERSONAL_NAMES={tool['name'] for tool in PERSONAL_TOOLS}
HEAVY=resources.BUSY

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args):return None

def parameters(name,args):
    if not isinstance(args,dict):raise ValueError('记忆查询参数必须为对象')
    if name=='get':
        entry_id=args.get('id')
        if not isinstance(entry_id,str) or not entry_id.strip() or len(entry_id)>128:
            raise ValueError('需要有效的记忆 id')
        return '/entry',{'id':entry_id.strip()}
    query={'workspace':'personal'}
    if name=='recall':
        text=args.get('query')
        limit=args.get('topK',5)
        if not isinstance(text,str) or not text.strip() or len(text)>2000:
            raise ValueError('需要明确的记忆查询主题')
        query.update(query=text.strip(),topK=limit)
        path='/recall'
    elif name=='list_recent':
        limit=args.get('n',10)
        query['n']=limit
        path='/list'
    else:raise ValueError('仅提供查询和读取记忆工具')
    if type(limit) is not int or not 1<=limit<=20:raise ValueError('条数需要为 1 到 20')
    for key in ('tag','project'):
        value=args.get(key)
        if value is not None:
            if not isinstance(value,str) or not 0<len(value)<=128:raise ValueError('筛选条件格式不合法')
            query[key]=value
    return path,query

def serve(*,runtime=None,settings_path=None,address=('127.0.0.1',18792),on_ready=None):
    runtime=Runtime() if runtime is None else runtime
    settings=json.loads(Path(settings_path or SETTINGS).read_text())
    if settings['origin']!=ORIGIN:raise RuntimeError('Unexpected memory origin')
    upstream_key=Path(settings['upstream_key_file']).read_text().strip()
    local_token=Path(settings['local_token_file']).read_text().strip()
    bridge_path=runtime.settings.get('codex_bridge_token_file')
    bridge_token=Path(bridge_path).read_text().strip() if bridge_path else ''
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect(),
                                      urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    efficiency=ToolEfficiency(runtime)
    def uncached_call(name,args):
        if name in PERSONAL_NAMES:
            heavy=name.startswith('bilibili_') or name in ('image_read','notion','douyin_video') or (name=='emote_save' and 'url' in args)
            if heavy:HEAVY.acquire()
            try:
                if heavy:resources.check_memory()
                return runtime.call(name,args)
            finally:
                if heavy:HEAVY.release()
        clean,context,chat=runtime.context(args,private=name in ('get','recall','list_recent','yesterday_health'))
        if name in RESOURCE_NAMES:
            runtime.platform_consent.ensure(name,clean,context,chat)
            result=resources.call(name,clean)
            return runtime.record_reading(chat,name,result)
        args=clean
        path,query=parameters(name,args)
        schedule=SecondBrainSchedule(runtime)
        tick_ticket,skip=schedule.before(context)
        if skip is not None:return skip
        request=urllib.request.Request(ORIGIN+path+'?'+urllib.parse.urlencode(query),method='GET',
            headers={'Authorization':'Bearer '+upstream_key,'Accept':'application/json','User-Agent':'FatfishMemoryReader/1.0'})
        try:
            with opener.open(request,timeout=20) as response:raw=response.read(1_048_577)
        except urllib.error.HTTPError as error:
            raise ValueError('记忆服务返回 HTTP '+str(error.code)+'，本次未取得可用记录')
        except (urllib.error.URLError,TimeoutError):raise ValueError('记忆服务暂时不可达，本次没有读取到记录')
        if len(raw)>1_048_576:raise ValueError('记忆返回内容过大，请缩小查询范围')
        value=json.loads(raw)
        if name=='get':
            entry=value.get('entry',{})
            if not isinstance(entry,dict) or entry.get('workspace') not in (None,'personal'):
                raise ValueError('仅提供个人记忆的读取')
            content=entry.get('content')
            offset=args.get('offset',0)
            if type(offset) is not int or offset<0:raise ValueError('正文偏移不合法')
            if isinstance(content,str):
                entry['content']=content[offset:offset+12000]
                value.update(content_length=len(content),offset=offset,next_offset=offset+12000 if len(content)>offset+12000 else None)
        if name=='list_recent' and isinstance(value,list):
            # /list is the Worker's dashboard route. Keep bounded previews like its MCP tool.
            for entry in value:
                if isinstance(entry,dict) and isinstance(entry.get('content'),str):
                    content=entry['content']
                    entry.update(content=content[:1600],truncated=len(content)>1600,full_length=len(content))
        schedule.success(tick_ticket,name)
        return value
    def call(name,args):
        if name=='batch':return efficiency.batch(args,call)
        return efficiency.invoke(name,args,lambda clean:uncached_call(name,clean))
    def dispatch(message):
        method=message.get('method')
        params=message.get('params') or {}
        if method=='initialize':
            version=params.get('protocolVersion','2025-03-26')
            if version not in ('2024-11-05','2025-03-26','2025-06-18','2025-11-25'):version='2025-03-26'
            return {'protocolVersion':version,'capabilities':{'tools':{}},'serverInfo':{'name':'personal-read-tools','version':'1.1.0'}}
        if method=='ping':return {}
        if method=='tools/list':return {'tools':TOOLS}
        if method=='tools/call':
            try:
                result=call(params.get('name'),params.get('arguments',{}))
                if isinstance(result,dict) and '_image' in result:
                    media=result['_image']
                    return {'content':[{'type':'text','text':result.get('source','')+'\n'+media['note']+'\nrecord_id: '+str(result.get('record_id',''))},
                        {'type':'image','mimeType':media['mime_type'],'data':media['base64']}],'isError':False}
                if isinstance(result,dict) and isinstance(result.get('results'),list):
                    images=[]
                    for item in result['results']:
                        value=item.get('result') or {}
                        if isinstance(value,dict) and '_image' in value:
                            media=value.pop('_image')
                            value['image_note']=media['note']
                            images.extend([{'type':'text','text':'批次第'+str(item['index']+1)+'项 '+item['name']},
                                {'type':'image','mimeType':media['mime_type'],'data':media['base64']}])
                    if images:return {'content':[{'type':'text','text':json.dumps(result,ensure_ascii=False)},*images],'isError':False}
                return {'content':[{'type':'text','text':json.dumps(result,ensure_ascii=False)}],'isError':False}
            except ValueError as error:
                return {'content':[{'type':'text','text':str(error)}],'isError':True}
            except Exception:
                return {'content':[{'type':'text','text':'记忆读取工具暂时不可用，本次没有取得记录'}],'isError':True}
        return None
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def send(self,status,value=None):
            raw=b'' if value is None else json.dumps(value,ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(raw)))
            self.end_headers()
            if raw:self.wfile.write(raw)
        def do_POST(self):
            if self.path not in ('/mcp','/bridge','/video-bridge') and not self.path.startswith('/runtime/'):self.send(404);return
            credential=bridge_token if self.path in ('/bridge','/video-bridge') else local_token
            if not credential or not hmac.compare_digest(self.headers.get('Authorization',''),'Bearer '+credential):
                self.send(401);return
            try:
                length=int(self.headers.get('Content-Length','0'))
                limit=2*1024*1024 if self.path=='/runtime/loop_context' else 65536
                if not 0<length<=limit:raise ValueError('Invalid size')
                message=json.loads(self.rfile.read(length))
                if not isinstance(message,dict):raise ValueError('Invalid message')
                if self.path=='/bridge':
                    endpoint={'dsh':runtime.dsh,'claude':runtime.claude}.get(message.get('backend'),runtime.codex)
                    self.send(200,endpoint.worker(message));return
                if self.path=='/video-bridge':
                    self.send(200,runtime.video.worker(message));return
                if self.path.startswith('/runtime/'):
                    self.send(200,runtime.action(self.path[len('/runtime/'):],message));return
                if 'id' not in message:self.send(202);return
                result=dispatch(message)
                response={'jsonrpc':'2.0','id':message['id']}
                if result is None:response['error']={'code':-32601,'message':'Method not found'}
                else:response['result']=result
                self.send(200,response)
            except Exception:self.send(400,{'error':'Invalid MCP request'})
        def do_GET(self):self.send(405)
        def do_DELETE(self):self.send(405)
    class BoundedServer(http.server.ThreadingHTTPServer):
        daemon_threads=True
        request_queue_size=8
        slots=threading.BoundedSemaphore(4)
        def process_request(self,request,address):
            if not self.slots.acquire(blocking=False):self.shutdown_request(request);return
            try:super().process_request(request,address)
            except Exception:self.slots.release();raise
        def process_request_thread(self,request,address):
            try:super().process_request_thread(request,address)
            finally:self.slots.release()
    server=BoundedServer(address,Handler)
    runtime.reminders.start()
    notify=os.environ.get('NOTIFY_SOCKET')
    if notify:
        if notify.startswith('@'):notify='\0'+notify[1:]
        with socket.socket(socket.AF_UNIX,socket.SOCK_DGRAM) as channel:
            channel.connect(notify);channel.sendall(b'READY=1')
    if on_ready:on_ready(server)
    server.serve_forever()

if __name__=='__main__':
    # notes inherits group=picoclaw from its root; only that assistant may read.
    # 027 permits new daily directories to retain group-read without chmod SGID.
    os.umask(0o027)
    serve()
