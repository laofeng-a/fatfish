"""Read scoped, stable conversation rounds without expanding all tool output."""
import datetime as dt
import json
import sqlite3
import zlib


class TurnHistory:
    def __init__(self,runtime):
        self.r=runtime
        self.path=runtime.directory/'turn-context.sqlite'

    def read(self,args,context,chat):
        target=args.get('chat_id',chat)
        with self.r.db() as db:
            if chat!=self.r.owner and target!=chat:raise ValueError('群聊只能读取当前会话的轮次')
            if target!=chat and chat!=self.r.owner:raise ValueError('无权读取其他会话')
            if not self.r._allowed(db,target):raise ValueError('需要当前或已登记会话')
        number=args.get('number');record=args.get('record_id')
        if number is None and record is None:raise ValueError('需要固定轮号number，或内部来源record_id')
        for value in [number,record]:
            if value is not None and (type(value) is not int or value<1):raise ValueError('编号需为正整数')
        part=args.get('part','all');offset=args.get('offset',0);limit=args.get('limit',6000)
        if part not in ('all','dialogue','tools','reasoning'):raise ValueError('part使用all、dialogue、tools或reasoning')
        if type(offset) is not int or offset<0 or type(limit) is not int or not 1<=limit<=12000:raise ValueError('分页参数无效')
        if not self.path.is_file():return {'status':'not_indexed_yet','chat_id':target}
        with sqlite3.connect('file:'+str(self.path)+'?mode=ro',uri=True,timeout=3) as db:
            db.row_factory=sqlite3.Row
            session=args.get('session_key')
            if session is None:
                candidate=context.get('session_key')
                if candidate and db.execute('SELECT 1 FROM context_sessions WHERE session=? AND chat=?',(candidate,target)).fetchone():session=candidate
                else:
                    row=db.execute('SELECT s.session FROM context_sessions s JOIN context_turns t ON t.session=s.session WHERE s.chat=? AND t.number>0 ORDER BY t.at DESC,t.id DESC LIMIT 1',(target,)).fetchone()
                    session=row['session'] if row else None
            if not isinstance(session,str) or not session:raise ValueError('当前会话还没有可查轮次')
            field,value=('id',record) if record is not None else ('number',number)
            row=db.execute('SELECT t.* FROM context_turns t JOIN context_sessions s ON s.session=t.session WHERE s.chat=? AND t.session=? AND t.'+field+'=?',(target,session,value)).fetchone()
            if not row:raise ValueError('此轮次不存在或不在当前会话范围')
            turn=dict(row);deliveries=json.loads(turn['deliveries']) or []
            messages=[]
            for item in db.execute('SELECT p.data FROM context_items i JOIN context_payloads p ON p.hash=i.payload_hash WHERE i.turn_id=? ORDER BY i.id',(turn['id'],)):
                raw=zlib.decompressobj().decompress(item['data'],16*1024*1024+1)
                if len(raw)>16*1024*1024:raise ValueError('单项记录过大，请沿原artifact读取')
                messages.append(json.loads(raw))
        reasoning=[];seen=set()
        for msg in messages:
            text=msg.get('reasoning_content','')
            if text and text not in seen:reasoning.append(text);seen.add(text)
        time=dt.datetime.fromtimestamp(turn['at'],dt.timezone(dt.timedelta(hours=8))).isoformat(timespec='seconds') if turn['at']>0 else None
        detail={'number':turn['number'] or None,'record_id':turn['id'],'time':time,'source_kind':turn['kind'],'received_message_id':turn['message_id'] or None,
                '用户原话':turn['user_text'],'实际发送':deliveries,'思考':reasoning,'思考说明':'当时记录的推断；不代表用户确认的事实。',
                '工具明细':[msg for msg in messages if msg.get('role')=='tool' or msg.get('tool_calls')],
                '原始记录':messages}
        if part=='dialogue':selected={k:detail[k] for k in ['time','source_kind','用户原话','实际发送']}
        elif part=='reasoning':selected={'思考':reasoning,'说明':detail['思考说明']}
        elif part=='tools':selected=detail['工具明细']
        else:selected=detail
        text=json.dumps(selected,ensure_ascii=False,indent=2)
        return {'status':'ok','chat_id':target,'session_key':session,'number':turn['number'] or None,'record_id':turn['id'],
                'part':part,'offset':offset,'total_chars':len(text),'content':text[offset:offset+limit],
                'next_offset':offset+limit if offset+limit<len(text) else None}
