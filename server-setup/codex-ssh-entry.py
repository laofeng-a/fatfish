"""Forced-command SSH entry: fixed /bridge endpoint only, bounded JSON lines.

Installed with authorized_keys 'restrict'; this key cannot run a shell or forward ports.
No model prompt, command, URL, filesystem path or token is written to logs.
"""
import json,sys,urllib.request

MAX_LINE=65536

def dispatch(message,opener):
    if not isinstance(message,dict):raise ValueError('Invalid request')
    identifier=message.get('id');token=message.get('token');data=message.get('data')
    if type(identifier) is not int or not isinstance(token,str) or not 20<=len(token)<=200 or not isinstance(data,dict):raise ValueError('Invalid request')
    action=data.get('action')
    if action not in ('poll','result','completed','observation','native_turn','unknown','video_poll','video_result'):raise ValueError('Unsupported bridge operation')
    route='/video-bridge' if action.startswith('video_') else '/bridge'
    payload=json.dumps(data,ensure_ascii=False).encode()
    request=urllib.request.Request('http://127.0.0.1:18792'+route,data=payload,
        headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'})
    with opener.open(request,timeout=18) as response:raw=response.read(MAX_LINE+1)
    if len(raw)>MAX_LINE:raise ValueError('Oversized bridge response')
    return {'id':identifier,'result':json.loads(raw)}

def serve(source,sink):
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    while True:
        line=source.readline(MAX_LINE+1)
        if not line:return
        if len(line.encode('utf-8'))>MAX_LINE or not line.endswith('\n'):return
        identifier=None
        try:
            message=json.loads(line);identifier=message.get('id') if isinstance(message,dict) else None
            result=dispatch(message,opener)
        except Exception:result={'id':identifier,'error':'Bridge request failed'}
        sink.write(json.dumps(result,ensure_ascii=False)+'\n');sink.flush()

if __name__=='__main__':serve(sys.stdin,sys.stdout)
