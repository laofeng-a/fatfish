"""DSH adapter sharing the proven collaboration lifecycle, with separate SQL/state."""
from contextlib import contextmanager
import json
from codex_bridge import CodexBridge


class _Connection:
    def __init__(self,db,backend='dsh'):self.db=db;self.backend=backend
    def execute(self,sql,*args):return self.db.execute(sql.replace('codex_',self.backend+'_'),*args)
    def executescript(self,sql):return self.db.executescript(sql.replace('codex_',self.backend+'_'))
    def __getattr__(self,name):return getattr(self.db,name)


class _Runtime:
    def __init__(self,runtime,backend='dsh'):
        self.runtime=runtime;self.backend=backend
        self.settings=dict(runtime.settings)
        for name in ('default_thread_id','default_thread_name'):
            self.settings['codex_'+name]=runtime.settings.get(backend+'_'+name)
    @contextmanager
    def db(self):
        with self.runtime.db() as db:yield _Connection(db,self.backend)
    def _get(self,db,key,*args):return self.runtime._get(db,key.replace('codex_',self.backend+'_'),*args)
    def _set(self,db,key,*args):return self.runtime._set(db,key.replace('codex_',self.backend+'_'),*args)
    def __getattr__(self,name):return getattr(self.runtime,name)


def _notes(value):
    # Only machine-generated notes. Never rewrite actual assistant/user text.
    if isinstance(value,list):return [_notes(item) for item in value]
    if isinstance(value,dict):
        return {key:val.replace('Codex','DSH') if key=='note' and isinstance(val,str) else _notes(val) for key,val in value.items()}
    return value


class DshBridge:
    def __init__(self,runtime):
        self.r=runtime;self.scoped=_Runtime(runtime);self.bridge=CodexBridge(self.scoped)
    def call(self,args,context,chat):
        if isinstance(args.get('answers'),dict):
            args=dict(args,text=json.dumps(args['answers'],ensure_ascii=False))
        if args.get('action')=='usage':
            return {'backend':'dsh','state':'unavailable','note':'DSH额度尚无已核实的统一接口；不能用Codex额度代替。'}
        return dict(_notes(self.bridge.call(args,context,chat)),backend='dsh')
    def worker(self,data):return dict(self.bridge.worker(data),backend='dsh')
    def has_event(self,identifier):
        with self.scoped.db() as db:return db.execute('SELECT 1 FROM codex_events WHERE id=?',(identifier,)).fetchone() is not None
    def event_action(self,name,data):
        result=self.bridge.runtime_action(name,data)
        event=result.get('event')
        if event:
            event['backend']='dsh'
            event['content']=event['content'].replace('Codex回合状态：','DSH回合状态：',1).replace('Codex回复：','DSH回复：',1).replace('先前的Codex查询已返回','先前的DSH查询已返回',1)
            event['content']=event['content'].replace('Codex执行入口返回错误','DSH执行入口返回错误',1)
            event['content']+='\n这是DSH执行端的回执；续任务、收口或等jg时使用dsh工具及本次job_id。'
        return result


def collaboration_event(runtime,name,data):
    adapters=[runtime.dsh]+([runtime.claude] if hasattr(runtime,'claude') else [])
    if name=='codex_event_poll':
        candidates=[]
        for bridge in (runtime.codex,*(a.bridge for a in adapters)):
            with bridge.r.db() as db:
                row=db.execute("SELECT created FROM codex_events WHERE status IN ('pending','claimed','processing') ORDER BY created LIMIT 1").fetchone()
                if row:candidates.append((row['created'],bridge))
        ordered=[item[1] for item in sorted(candidates,key=lambda item:item[0])]
        for bridge in ordered:
            adapter=next((a for a in adapters if a.bridge is bridge),None)
            result=adapter.event_action(name,data) if adapter else bridge.runtime_action(name,data)
            if result.get('event'):return result
        return {'event':None}
    for adapter in adapters:
        if adapter.has_event(data.get('event_id','')):return adapter.event_action(name,data)
    return runtime.codex.runtime_action(name,data)
