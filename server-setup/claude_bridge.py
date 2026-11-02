"""Claude Code endpoint: independent queue, existing collaboration callbacks."""
import json
from codex_bridge import CodexBridge
from dsh_bridge import _Runtime

class ClaudeBridge:
    def __init__(self,runtime):
        self.r=runtime;self.scoped=_Runtime(runtime,'claude');self.bridge=CodexBridge(self.scoped)
    def call(self,args,context,chat):
        args=dict(args)
        if isinstance(args.get('answers'),dict):args['text']=json.dumps(args['answers'],ensure_ascii=False)
        if args.get('action')=='finish':args.setdefault('status','completed')
        if args.get('action') in ('continue','finish','wait_for_jg') and not args.get('job_id'):
            event_id=self.bridge._event_from_context(context)
            with self.scoped.db() as db:
                event=db.execute('SELECT e.kind,j.origin FROM codex_events e JOIN codex_jobs j ON j.id=e.job_id WHERE e.id=?',(event_id,)).fetchone()
                if event and event['kind']=='query':
                    parent=json.loads(event['origin'] or '{}').get('codex_chain_id')
                    chain=db.execute('SELECT thread_id FROM codex_chains WHERE id=?',(parent,)).fetchone() if parent else None
                    if chain and args.get('thread_id',chain['thread_id'])==chain['thread_id']:args['job_id']=parent
        result=self.bridge.call(args,context,chat)
        if isinstance(result.get('note'),str):result['note']=result['note'].replace('Codex','Claude Code')
        return dict(result,backend='claude')
    def worker(self,data):return dict(self.bridge.worker(data),backend='claude')
    def has_event(self,identifier):
        with self.scoped.db() as db:return db.execute('SELECT 1 FROM codex_events WHERE id=?',(identifier,)).fetchone() is not None
    def event_action(self,name,data):
        result=self.bridge.runtime_action(name,data)
        event=result.get('event')
        if event:
            event['backend']='claude'
            for before,after in [('Codex回合状态：','Claude Code回合状态：'),('Codex回复：','Claude Code回复：'),('先前的Codex查询已返回','先前的Claude Code查询已返回'),('Codex执行入口返回错误','Claude Code执行入口返回错误')]:event['content']=event['content'].replace(before,after,1)
            with self.scoped.db() as db:row=db.execute('SELECT origin FROM codex_jobs WHERE id=?',(event['job_id'],)).fetchone()
            origin=json.loads(row['origin'] or '{}') if row else {}
            if origin.get('codex_event_silent') or origin.get('session_key')=='heartbeat':
                event['silent']=True
                event['content']+='\n本轮是静默跟进，结果留内部；无需发送聊天消息。'
            event['content']+='\n这是Claude Code执行端回执；用claude_code工具继续或finish收口，沿用当前任务job_id。'
        return result
