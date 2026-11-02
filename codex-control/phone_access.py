"""Email-protected Cloudflare Quick Tunnel to the existing local controller.

The origin requires a private, random Host capability set only by our cloudflared
process. The external HTTPS origin is exact-matched for browser POST requests.
No local Codex capability, token file, or arbitrary proxy destination is exposed.
"""
import http.server
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

HIDDEN=getattr(subprocess,'CREATE_NO_WINDOW',0)
STATIC={'/':'control.html','/control.js':'control.js','/control.css':'control.css'}
MIMES={'.html':'text/html; charset=utf-8','.js':'text/javascript; charset=utf-8','.css':'text/css; charset=utf-8'}


class PhoneAccess:
    def __init__(self,control,directory):
        self.control=control;self.directory=Path(directory);self.origin='';self.armed=False
        self.host=secrets.token_hex(32)+'.localhost';self.process=None;self.server=None
        self.allowed_mail='';self.state='disabled';self.started_at=None;self.last_error=None
        self.closed=threading.Event();self.lock=threading.RLock()
        self.diagnostics=[]

    def public_status(self):
        return {'state':self.state,'url':self.origin if self.armed else None,
            'allowed_mail':self.allowed_mail,'temporary':True,'started_at':self.started_at,
            'error':self.last_error}

    def publish_status(self):
        path=self.directory/'phone-status.json';pending=path.with_suffix('.pending')
        with self.lock:
            pending.write_text(json.dumps(self.public_status()),encoding='utf-8');os.replace(pending,path)
            debug=self.directory/'phone-diagnostic.json'
            debug.write_text(json.dumps({'candidate_url':self.origin,'events':self.diagnostics[-12:]}),encoding='utf-8')

    def start(self):
        config_path=self.directory/'phone-access.json'
        if not config_path.exists():return
        config=json.loads(config_path.read_text(encoding='utf-8'))
        if not config.get('enabled'):return
        email=config.get('allowed_mail','')
        if not re.fullmatch(r'[A-Za-z0-9.!#$%&\x27+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}',email) or '*' in email:
            raise ValueError('Phone access needs one explicit owner email')
        self.allowed_mail=email
        binary=self.directory/'cloudflared.exe'
        help_result=subprocess.run([str(binary),'tunnel','--help'],capture_output=True,text=True,encoding='utf-8',creationflags=HIDDEN,timeout=15)
        if '--allowed-mail' not in help_result.stdout:raise RuntimeError('Protected Quick Tunnels unavailable; refusing to start')
        self.server=self.make_server(int(config.get('port',55358)))
        threading.Thread(target=self.server.serve_forever,daemon=True).start()
        self.state='starting';self.publish_status()
        environment=dict(os.environ,TUNNEL_HTTP_HOST_HEADER=self.host)
        # The capability is in the child environment, never a process argument or URL.
        command=[str(binary),'tunnel','--no-autoupdate','--protocol','http2',
            '--url','http://127.0.0.1:'+str(self.server.server_port),'--allowed-mail',email]
        self.process=subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
            text=True,encoding='utf-8',errors='replace',env=environment,creationflags=HIDDEN)
        threading.Thread(target=self.read_output,daemon=True).start()
        threading.Thread(target=self.monitor,daemon=True).start()

    def read_output(self):
        for line in self.process.stdout:
            lower=line.lower()
            for needle,label in [('requesting new quick tunnel','requesting_tunnel'),('registered tunnel','connected'),('timeout','timeout'),('proxyconnect','proxy_connection_error'),('failed to request','registration_failed'),('error','cloudflared_error')]:
                if needle in lower:self.diagnostics.append(label);self.publish_status();break
            match=re.search(r'https://([a-z0-9-]+\.trycloudflare\.com)\b',line)
            if match and not self.origin:
                self.origin='https://'+match[1];self.publish_status()
        if not self.closed.is_set() and self.state!='blocked':
            self.armed=False;self.state='offline';self.last_error='手机入口已断开；本机控制台仍可使用。';self.publish_status()

    def verify_login_gate(self):
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self,*args,**kwargs):return None
        # Respect this machine's existing per-process proxy environment; do not
        # change DNS/routes or bypass the user's working network configuration.
        opener=urllib.request.build_opener(NoRedirect())
        try:
            response=opener.open(urllib.request.Request(self.origin+'/',headers={'User-Agent':'Fatfish-Phone-Gate-Check/1'}),timeout=8)
        except urllib.error.HTTPError as error:
            if error.code not in (301,302,303,307,308):return False
            location=urllib.parse.urljoin(self.origin,error.headers.get('Location',''))
            parsed=urllib.parse.urlsplit(location)
            # A successful app response never counts as proof of email protection.
            access_redirect=(parsed.hostname or '').endswith('.cloudflareaccess.com') and '/cdn-cgi/access/login' in parsed.path
            quick_redirect=parsed.hostname=='login.trycloudflare.com' and parsed.path=='/authorize' and urllib.parse.parse_qs(parsed.query).get('hostname')==[urllib.parse.urlsplit(self.origin).hostname]
            return parsed.scheme=='https' and (access_redirect or quick_redirect)
        else:
            response.close();return False

    def monitor(self):
        deadline=time.monotonic()+65
        while not self.closed.wait(2) and self.process.poll() is None:
            if self.origin:
                try:protected=self.verify_login_gate()
                except (OSError,urllib.error.URLError):protected=False
                if protected:
                    self.armed=True;self.state='online';self.started_at=time.time();self.publish_status();return
            if time.monotonic()>deadline:break
        if not self.closed.is_set():
            self.last_error='没有确认邮箱登录保护，手机入口未开放。';self.state='blocked';self.armed=False
            if self.process.poll() is None:self.process.terminate()
            self.publish_status()

    def make_server(self,port):
        manager=self;root=Path(__file__).parent
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def send(self,status,value,mime='application/json; charset=utf-8'):
                data=json.dumps(value,ensure_ascii=False).encode() if mime.startswith('application/json') else value
                self.send_response(status);self.send_header('Content-Type',mime);self.send_header('Content-Length',str(len(data)))
                self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff')
                self.send_header('Referrer-Policy','no-referrer')
                self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'; base-uri 'none'")
                self.end_headers();self.wfile.write(data)
            def authorized_hop(self):
                return secrets.compare_digest(self.headers.get('Host',''),manager.host)
            def gate(self):
                if not self.authorized_hop():self.send(403,{'error':'Invalid tunnel origin'});return False
                if not manager.armed:self.send(503,{'error':'手机入口正在检查登录保护，请稍后刷新。'});return False
                return True
            def do_GET(self):
                if not self.gate():return
                if self.path=='/bootstrap':self.send(200,{'mode':'phone'});return
                if self.path=='/favicon.svg':
                    self.send(200,b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><rect width="32" height="32" rx="9" fill="#ac533a"/><text x="7" y="24" fill="#fff" font-size="23">C</text></svg>','image/svg+xml');return
                name=STATIC.get(self.path)
                if not name:self.send(404,{'error':'Not found'});return
                self.send(200,(root/name).read_bytes(),MIMES[Path(name).suffix])
            def do_POST(self):
                if not self.gate():return
                if self.headers.get('Origin')!=manager.origin:self.send(403,{'error':'Invalid browser origin'});return
                if self.path!='/api':self.send(404,{'error':'Not found'});return
                try:
                    length=int(self.headers.get('Content-Length','0'))
                    if not 0<length<=65536 or self.headers.get('Content-Type','').split(';')[0]!='application/json':raise ValueError('Invalid request')
                    args=json.loads(self.rfile.read(length))
                    if not isinstance(args,dict) or args.get('action') not in ('status','list','history','attach','send','stop'):
                        raise ValueError('Unsupported phone action')
                    result=manager.control.call(args)
                    if len(json.dumps(result,ensure_ascii=False).encode())>2*1024*1024:raise ValueError('Result too large')
                    self.send(200,{'result':result})
                except Exception as error:self.send(503 if isinstance(error,ConnectionError) else 400,{'error':str(error)[:600]})
            def do_OPTIONS(self):self.send(405,{'error':'Not allowed'})
        class Server(http.server.ThreadingHTTPServer):
            daemon_threads=True;request_queue_size=4
            slots=threading.BoundedSemaphore(4)
            def process_request(self,request,address):
                if not self.slots.acquire(blocking=False):self.shutdown_request(request);return
                try:super().process_request(request,address)
                except Exception:self.slots.release();raise
            def process_request_thread(self,request,address):
                try:super().process_request_thread(request,address)
                finally:self.slots.release()
        return Server(('127.0.0.1',port),Handler)

    def close(self):
        self.closed.set();self.armed=False;self.state='offline'
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:self.process.kill()
        if self.server:self.server.shutdown();self.server.server_close()

