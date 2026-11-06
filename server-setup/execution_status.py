"""Execution evidence is independent from receipt processing and file delivery."""
import json


def normalized_output(output):
    result=dict(output or {})
    if result.get('submission_status') in ('superseded','cancelled') and result.get('terminal_confirmed') is not True:
        result.pop('requires_reconciliation',None)
        result.update(state=result['submission_status'],execution_state='not_started')
        return result
    if result.get('terminal_confirmed') is True:
        result.pop('requires_reconciliation',None)
        state=result.get('execution_state')
        if state not in ('completed','failed','cancelled','interrupted'):
            state='completed' if result.get('turn_status')=='completed' else result.get('turn_status','unknown')
        result['submission_state']=result.get('submission_state') or result.get('state')
        result.update(state=state,execution_state=state,started=True)
        if '已加入原会话' in str(result.get('note','')):
            result.setdefault('submission_note',result['note'])
            result['note']='实际执行终态已确认；回执处理与成果交付状态分别记账。'
    elif result.get('turn_id') and result.get('execution_state')=='running':
        result['submission_state']=result.get('submission_state') or result.get('state')
        result.update(state='running',started=True)
    return result


def receipt_summary(row):
    args=json.loads(row.get('args') or '{}')
    output=normalized_output(json.loads(row.get('output') or '{}'))
    result={k:row.get(k) for k in ('id','backend','action','state','created','updated','chain_id')}
    result['args']={k:v for k,v in args.items() if k in ('thread_id','text','path','detail')}
    result['output']={k:output[k] for k in ('thread_id','turn_id','state','execution_state','terminal_confirmed',
        'completion_outcome','observed_at','final','submission_status','queued_submission_id','artifact','delivery') if k in output}
    if 'turns' in output:
        result['output']['turns']=[{k:t[k] for k in ('id','status','execution_state','terminal_confirmed','completion_outcome','status_source') if k in t} for t in output['turns']]
    return result
