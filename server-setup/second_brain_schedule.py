"""Persisted spacing for background Second Brain reads; foreground is on demand."""
import datetime as dt
import time

KEY='second_brain_background_read'
DEFAULT_INTERVAL=48*60*60
RECENT_SUCCESS='second_brain_recent_success_at'


class SecondBrainSchedule:
    def __init__(self,runtime):self.r=runtime

    def before(self,context):
        if context.get('session_key')!='heartbeat':return None,None
        with self.r.db() as db:return self.r._get(db,'current_wake') or '',None

    def due_hint(self,db,now):
        last=float(self.r._get(db,RECENT_SUCCESS,self.r._get(db,KEY+'_at','0')))
        if last and now-last<DEFAULT_INTERVAL:return ''
        return ('Second Brain新增资料已到约两天的回顾时点。先用list_recent(n=3)看看新增，'
                '按兴趣展开或留待聊topic；这次回顾可安静完成。成功读取后程序会记录新时点。')

    def success(self,ticket,tool_name=None):
        if ticket is None and tool_name!='list_recent':return
        with self.r.db() as db:
            if tool_name=='list_recent':self.r._set(db,RECENT_SUCCESS,time.time())
            if ticket is None:return
            # Keep the original check time while expanding that tick's results.
            if ticket and self.r._get(db,KEY+'_wake')==ticket:return
            self.r._set(db,KEY+'_at',time.time());self.r._set(db,KEY+'_wake',ticket)
