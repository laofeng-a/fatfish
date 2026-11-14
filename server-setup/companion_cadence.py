"""One companionship deadline, set explicitly or by a confirmed delivery."""
import json
import time

from reminders import Reminders

INTERVAL_SECONDS=60


class CompanionCadence:
    def __init__(self,runtime):self.r=runtime

    @property
    def enabled(self):return self.r.settings.get('companion_cadence_enabled') is True

    def get(self,db,key,default=None):
        return json.loads(self.r._get(db,key,json.dumps(default)))

    def put(self,db,key,value):self.r._set(db,key,json.dumps(value,ensure_ascii=False))

    def audit(self,db,kind,**data):
        epoch=db.execute('SELECT epoch FROM loop_autonomy WHERE id=1').fetchone()[0]
        db.execute('INSERT INTO loop_activity(epoch,kind,content,created)VALUES(?,?,?,?)',
                   (epoch,'chat_'+kind,json.dumps(data,ensure_ascii=False)[:2000],time.time()))

    def latest(self,db):
        row=db.execute("SELECT id,msg FROM events WHERE chat=? AND kind IN ('user','forwarded') ORDER BY id DESC LIMIT 1",(self.r.owner,)).fetchone()
        return dict(row) if row else {'id':0,'msg':''}

    def source(self,db):return self.get(db,'companion_source',{'version':0,'source_id':0,'source_message_id':''})

    def disarm(self,db,reason):
        old=self.get(db,'next_chat_due')
        self.put(db,'next_chat_due',None)
        self.put(db,'companion_due_explicit',False)
        if old is not None:self.audit(db,'cancelled',reason=reason,previous_due=old)

    def inbound(self,db,message):
        if not self.enabled:return
        latest=self.latest(db);old=self.source(db)
        if latest['msg']!=str(message) or latest['id']==old.get('source_id'):return
        self.disarm(db,'new_human_message')
        self.put(db,'companion_source',{'version':old.get('version',0)+1,'source_id':latest['id'],'source_message_id':latest['msg']})

    def available(self,db):
        auto=db.execute('SELECT enabled,epoch FROM loop_autonomy WHERE id=1').fetchone()
        return bool(self.enabled and auto['enabled'] and self.r._get(db,'paused','0')!='1')

    def configure(self,db,args,context):
        if not self.available(db):
            raise ValueError('陪聊当前未开启或已暂停')
        latest=self.latest(db)
        human=self.r.loops.source(db,context)
        wake=self.wake_id(context)
        current_wake=self.claim_for(db,wake) is not None and not self.send_gate(db,context)
        if not self.r.loops.admitted(db,context) or (human.get('id')!=latest['id'] and not current_wake):
            raise ValueError('请在当前真人私聊、陪聊或自由活动回合安排陪聊')

        if args['action']=='chat_cancel':
            self.disarm(db,'explicit_cancel')
            self.put(db,'companion_cancelled_source_id',latest['id'])
            return

        has_time='delay_seconds' in args or 'due_at' in args
        if not has_time and 'interval_seconds' not in args:
            raise ValueError('请填写delay_seconds、due_at或interval_seconds')
        interval=args.get('interval_seconds',self.interval(db))
        if type(interval) is not int or not INTERVAL_SECONDS<=interval<=366*86400:
            raise ValueError('陪聊间隔最短60秒，最长一年')
        now=time.time()
        due=Reminders.parse_due(args,now) if has_time else now+interval
        if due-now<INTERVAL_SECONDS:
            raise ValueError('下次陪聊至少在60秒后')

        self.put(db,'companion_interval_seconds',interval)
        self.put(db,'companion_cancelled_source_id',None)
        self.put(db,'next_chat_due',due)
        self.put(db,'companion_due_explicit',True)
        self.audit(db,'scheduled',due=due,interval_seconds=interval,source_id=latest['id'],wake_id=wake)

    def interval(self,db):
        return self.get(db,'companion_interval_seconds',INTERVAL_SECONDS)

    def peek(self,db,now=None):
        now=time.time()if now is None else now;due=self.get(db,'next_chat_due')
        if due is None:return False
        if not self.available(db):
            self.disarm(db,'master_closed_or_paused');return False
        return due<=now

    def capture(self,db,wake,now):
        auto=db.execute('SELECT epoch FROM loop_autonomy WHERE id=1').fetchone()
        latest=self.latest(db);source=self.source(db)
        record={'wake_id':wake,'epoch':auto['epoch'],'version':source.get('version',0),
                'source_id':latest['id'],'claimed_at':now,'blocked':False}
        self.put(db,'companion_current_claim',record)
        return record

    def claim(self,db,wake,now):
        if not self.peek(db,now):return None
        due=self.get(db,'next_chat_due');self.put(db,'next_chat_due',None)
        self.put(db,'companion_due_explicit',False)
        record=self.capture(db,wake,now);epoch=record['epoch']
        db.execute('INSERT INTO loop_wakes VALUES(?,NULL,?,?)',(wake,epoch,'chat'))
        db.execute('INSERT INTO wakes(id,created,day,kind)VALUES(?,?,?,?)',(wake,now,time.strftime('%Y-%m-%d'),'chat'))
        self.audit(db,'claimed',wake_id=wake,scheduled_at=due,started_at=now,delay_seconds=round(now-due,3))
        recent=[dict(row)for row in db.execute("SELECT id,msg,kind,content,created FROM events WHERE chat=? AND kind IN ('user','forwarded','assistant') ORDER BY id DESC LIMIT 24",(self.r.owner,))]
        auto=db.execute('SELECT note FROM loop_autonomy WHERE id=1').fetchone()
        context=('这是陪聊／自主活动机会，没有新的真人消息。接着最近真实交流，你可以补一句想说的话、接梗、分享，也可以阅读、探索、整理或做其他已授权且自己感兴趣的事；有想表达的就显式发送，没有也可安静结束。\n'
                 f'文本、表情、图片都可以表达。真实发送后按当前{self.interval(db)}秒间隔续排；安静结束时，临时链自然停止。想约个时间再来，可以用autonomy的chat_schedule设置下次，或用chat_cancel取消。内部结论留内部。\n'
                 '最近真实往来（按时间）：'+json.dumps(list(reversed(recent)),ensure_ascii=False)+'\n已有活动续记：'+auto['note']+
                 '\n相关记忆线索：'+self.r.memory_recall.background_context(db,self.r.owner))
        return {'allowed':True,'silent':True,'wake_id':wake,'kind':'chat','session_key':'agent:autonomy-'+str(epoch),'context':context}

    def wake_id(self,data):
        message=str(data.get('message_id')or data.get('turn')or'')
        return message.removeprefix('heartbeat:')if message.startswith('heartbeat:')else ''

    def claim_for(self,db,wake):
        row=db.execute('SELECT kind FROM loop_wakes WHERE id=?',(wake,)).fetchone()if wake else None
        if not row or row['kind']not in ('chat','autonomy'):return None
        record=self.get(db,'companion_current_claim',{})
        return record if record.get('wake_id')==wake else {'wake_id':wake,'stale':True}

    def send_gate(self,db,data):
        if self.turn_blocked(db,data):return 'explicit_internal_note'
        if not self.enabled:return ''
        record=self.claim_for(db,self.wake_id(data))
        if record is None:return ''
        if record.get('blocked'):return 'explicit_internal_note'
        if record.get('stale'):return 'stale_companion_context'
        source=self.source(db);latest=self.latest(db)
        if record['source_id']!=latest['id']or record['version']!=source.get('version',0):return 'stale_companion_context'
        wake=db.execute('SELECT finished FROM wakes WHERE id=?',(record['wake_id'],)).fetchone()
        if wake and wake['finished']:return 'companion_turn_finished'
        return ''

    def blocked(self,db,data):
        source=str(data.get('message_id')or data.get('turn')or data.get('session_key')or'')
        if source:self.put(db,'internal_note_blocked:'+source,True)
        if not self.enabled:return
        record=self.claim_for(db,self.wake_id(data))
        if record is not None:
            record['blocked']=True;self.put(db,'companion_current_claim',record)
            if not self.get(db,'companion_due_explicit',False):
                self.disarm(db,'internal_note_filtered')
            self.audit(db,'filtered',wake_id=record['wake_id'])

    def turn_blocked(self,db,data):
        source=str(data.get('message_id')or data.get('turn')or data.get('session_key')or'')
        if source and self.get(db,'internal_note_blocked:'+source,False):return True
        record=self.claim_for(db,self.wake_id(data))if self.enabled else None
        return bool(record and record.get('blocked'))

    def reserve(self,db,ticket,data):
        if not self.enabled or str(data.get('chat_id','')).split('/',1)[0]!=self.r.owner or data.get('feedback'):return
        source=str(data.get('message_id')or data.get('turn')or'');latest=self.latest(db)
        wake=self.wake_id(data);claim=self.claim_for(db,wake)
        human=db.execute("SELECT id FROM events WHERE chat=? AND msg=? AND kind IN ('user','forwarded')",(self.r.owner,source)).fetchone()
        if not human and claim is None:return
        current=self.source(db);epoch=db.execute('SELECT epoch FROM loop_autonomy WHERE id=1').fetchone()[0]
        if human and human['id']!=latest['id']:return
        record={'epoch':epoch,'source_id':latest['id'],'version':current.get('version',0),
                'wake_id':wake,'source_message_id':source,'confirmed':False}
        self.put(db,'companion_send:'+ticket,record)

    def complete(self,db,ticket,status,ids,now=None):
        now=time.time()if now is None else now
        record=self.get(db,'companion_send:'+str(ticket))
        if not record or record.get('confirmed'):return
        record['confirmed']=True;self.put(db,'companion_send:'+str(ticket),record)
        if status not in ('sent','partial')or not ids:
            self.audit(db,'delivery_failed',ticket=ticket,status=status);return
        if record.get('wake_id'):
            db.execute('UPDATE wakes SET contacted=1 WHERE id=?',(record['wake_id'],))
        auto=db.execute('SELECT epoch FROM loop_autonomy WHERE id=1').fetchone()
        current=self.source(db);latest=self.latest(db)
        if record['epoch']!=auto['epoch']or record['version']!=current.get('version',0)or record['source_id']!=latest['id']:return
        claim=self.claim_for(db,record.get('wake_id'))
        if claim and (claim.get('blocked')or claim.get('stale')):return
        if not self.available(db):return
        if self.get(db,'companion_due_explicit',False):return
        if self.get(db,'companion_cancelled_source_id')==latest['id']:return
        due=now+self.interval(db)
        self.put(db,'next_chat_due',due)
        self.audit(db,'armed',ticket=ticket,message_ids=ids,due=due,source_id=record['source_id'])

    def finish(self,db,wake,outcome):
        record=self.claim_for(db,wake)
        if record is None:return
        row=db.execute('SELECT contacted FROM wakes WHERE id=?',(wake,)).fetchone()
        disposition='filtered'if record.get('blocked')else ('sent'if row and row['contacted']else ('quiet'if outcome=='completed'else 'failed'))
        if (disposition in ('quiet','failed','filtered')
                and record.get('version')==self.source(db).get('version',0)
                and not self.get(db,'companion_due_explicit',False)):
            self.disarm(db,disposition)
        self.audit(db,'finished',wake_id=wake,disposition=disposition,outcome=outcome)

    def status(self,db):
        return {'enabled':self.enabled,'next_chat_due':self.get(db,'next_chat_due'),
                'interval_seconds':self.interval(db),'explicit':self.get(db,'companion_due_explicit',False)}

    def legacy_chat_request(self,args):
        with self.r.db()as db:
            result=self.status(db)
        return dict(result,managed=True,note='用autonomy的chat_schedule或chat_cancel管理陪聊计划。')
