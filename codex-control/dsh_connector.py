"""Outbound-only DSH queue worker; local host API, credentials stay on the PC."""
import argparse,json,os,sys,time,urllib.request,urllib.error
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent.parent/'fatfish-codex'))
from worker import SshBridge

class DshClient:
    def __init__(self,directory):self.directory=Path(directory)
    def execute(self,job):
        config=json.loads((self.directory/'host.json').read_text(encoding='utf-8'));token=(self.directory/'host.token').read_text().strip()
        request=urllib.request.Request(config['url'],data=json.dumps(job,ensure_ascii=False).encode(),headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'})
        try:
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request,timeout=30) as response:return json.load(response)
        except urllib.error.HTTPError as error:
            value=json.loads(error.read(4000));raise RuntimeError(value.get('error','DSH接口失败')) from None

def run(config):
    root=Path(config['state_file']).parent;token=Path(config['token_file']).read_text().strip();client=DshClient(root)
    def state(value):
        value.update(pid=os.getpid(),at=time.time());p=Path(config['state_file']);pending=p.with_suffix('.pending');pending.write_text(json.dumps(value),encoding='utf-8');os.replace(pending,p)
    while True:
        bridge=None
        try:
            client.execute({'action':'status'})
            bridge=SshBridge(config,token)
            def post(value):return bridge.post(dict(value,backend='dsh'))
            while True:
                host=client.execute({'action':'status'})
                poll=post({'action':'poll','protocol_version':5})
                if poll.get('backend')!='dsh':raise RuntimeError('Server has not enabled the DSH queue')
                job=poll.get('job')
                if job:
                    try:result=client.execute(job);payload={'action':'result','id':job['id'],'claim':job['claim'],'result':result}
                    except Exception as error:payload={'action':'result','id':job['id'],'claim':job['claim'],'error':True,'result':{'error':type(error).__name__,'message':str(error)[:1000]}}
                    post(payload)
                for watch in poll.get('watches',[]):
                    value=client.execute({'action':'watch','id':watch['job_id']})
                    if value.get('terminal_confirmed'):
                        post({'action':'completed','job_id':watch['job_id'],'turn_id':watch['turn_id'],'status':value['status'],'text':value.get('text',''),
                            'terminal_confirmed':True,'status_source':'dsh_durable_turn_end' if value['state']!='waiting_input' else 'dsh_pending_question','completion_outcome':value['state']})
                state({'online':True,'host_pid':host['pid']})
                time.sleep(1 if job or poll.get('watches') else 2)
        except Exception as error:state({'online':False,'error':type(error).__name__,'message':str(error)[:250]})
        finally:
            if bridge:bridge.close()
        time.sleep(5)

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--config',required=True);args=parser.parse_args()
    config=json.loads(Path(args.config).read_text(encoding='utf-8-sig'))
    import msvcrt
    lock=Path(config['state_file']).with_suffix('.lock').open('a+b');lock.write(b'0');lock.flush();lock.seek(0)
    try:msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
    except OSError:raise SystemExit(0)
    run(config)
