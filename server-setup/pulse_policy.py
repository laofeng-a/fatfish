"""Admission and compact context for jg's three explicitly chosen pulse types."""
import json
import time

PURPOSES={'chat':120,'quota_watch':300,'quota_reset':0}


class PulsePolicy:
    def __init__(self,runtime):self.r=runtime

    @property
    def enabled(self):return self.r.settings.get('pulse_policy',{}).get('enabled') is True

    def maintenance(self,row):
        return row and row['id'] in self.r.settings.get('pulse_policy',{}).get('existing_maintenance_ids',[])

    def schedule(self,task,interval,purpose=None,row=None):
        if not self.enabled:return interval,purpose or (row['purpose'] if row else '')
        if self.maintenance(row):return interval,''
        purpose=purpose or (row['purpose'] if row else '')
        if not purpose:
            if any(word in task for word in ('陪聊','陪jg聊天','闲聊','续聊','聊天节拍')):purpose='chat'
            elif '额度' in task:purpose='quota_reset' if interval==0 and any(word in task for word in ('重置','恢复')) else 'quota_watch'
        if purpose not in PURPOSES:raise ValueError('节拍只有chat（陪聊120秒）、quota_watch（任务运行时盯额度300秒）、quota_reset（重置时一次）；请填写purpose。')
        return PURPOSES[purpose],purpose

    def stop_reason(self,db,row,now):
        if not self.enabled or self.maintenance(row):return ''
        if row['purpose'] not in PURPOSES:return '未归入jg最新约定的三种节拍，已撤销。'
        if row['purpose']=='quota_watch':
            active=any(db.execute('SELECT 1 FROM '+prefix+"_chains WHERE state IN ('queued','running') LIMIT 1").fetchone()
                       for prefix in ('codex','dsh','claude'))
            if not active:return '没有正在执行的协作任务，已撤销盯额度节拍。'
        if row['purpose']=='chat':
            user=db.execute("SELECT created FROM events WHERE chat=? AND kind='user' ORDER BY id DESC LIMIT 1",(self.r.owner,)).fetchone()
            since=max(row['created'],user[0] if user else row['created'])
            count=db.execute('SELECT COUNT(*) FROM silent_alarm_runs WHERE alarm_id=? AND started>=? AND finished IS NOT NULL',(row['id'],since)).fetchone()[0]
            if count>=2:return '最近真人消息后已检查两拍，未等到新接话，已撤销陪聊节拍；新交流可按需再挂。'
        return ''

    def snapshot(self,db,row,now):
        if not self.enabled or row['purpose'] not in PURPOSES:return ''
        data={'purpose':row['purpose'],'alarm_id':row['id'],'interval_seconds':row['interval_seconds']}
        if row['purpose']=='chat':
            rows=db.execute("SELECT id,msg,kind,content,created FROM events WHERE chat=? AND kind IN ('user','assistant') ORDER BY id DESC LIMIT 4",(self.r.owner,)).fetchall()
            data['recent_messages']=[{**dict(r),'content':r['content'][:500]} for r in reversed(rows)]
            instruction='以上为实际收发记录，直接接话；需要更早细节时才查历史。不适合继续就撤销本节拍。'
        else:
            usage=db.execute('SELECT payload,created FROM codex_usage_cache WHERE id=1').fetchone()
            if usage:data['last_usage']={'data':json.loads(usage['payload']),'age_seconds':round(now-usage['created'])}
            data['active_tasks']=[dict(r) for r in db.execute("SELECT thread_id,state,last_turn FROM codex_chains WHERE state IN ('queued','running','waiting') ORDER BY updated DESC LIMIT 3")]
            instruction='复用已有任务与回执，额度记录带采样年龄；确需当前额度才查询usage。任务结束撤销盯额度节拍；等待重置改在实际重置时间只挂一次。'
        return '[本次节拍的现成记录]\n'+json.dumps(data,ensure_ascii=False)+'\n'+instruction

    def ended_without_share(self,db,row,wake_id,outcome):
        if not self.enabled or row['purpose']!='chat' or outcome!='completed':return False
        wake=db.execute('SELECT contacted FROM wakes WHERE id=?',(wake_id,)).fetchone()
        return bool(wake and not wake['contacted'])
