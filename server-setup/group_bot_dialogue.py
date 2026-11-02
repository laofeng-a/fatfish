"""Bounded peer-bot opportunities. Each message is delivered to the model once."""
import time

class GroupBotDialogue:
    def __init__(self,runtime):
        self.r=runtime
        with runtime.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS bot_dialogue_receipts(chat TEXT,msg TEXT,epoch INTEGER,status TEXT,created REAL,PRIMARY KEY(chat,msg))')

    def consider(self,db,data,new_message,text):
        chat=str(data['chat_id']);message=str(data['message_id']);now=time.time()
        phase=data.get('delivery_phase','deliver')
        enabled=self.r.settings.get('group_bot_chat') is True and self.r.settings.get('group_chat_mode')=='members'
        old=db.execute('SELECT * FROM bot_dialogue_receipts WHERE chat=? AND msg=?',(chat,message)).fetchone()
        if old:
            if not enabled:
                db.execute("UPDATE bot_dialogue_receipts SET status='ignored' WHERE chat=? AND msg=?",(chat,message))
                return False
            if old['status']!='pending' or now-old['created']>120:return False
            if phase=='deliver':db.execute("UPDATE bot_dialogue_receipts SET status='delivered' WHERE chat=? AND msg=?",(chat,message))
            return True
        epoch=db.execute("SELECT id,created FROM events WHERE chat=? AND kind IN ('user','member','forwarded') AND content NOT LIKE '/%' ORDER BY id DESC LIMIT 1",(chat,)).fetchone()
        allowed=bool(enabled and new_message and text and not text.startswith('/') and str(data.get('thread_id','0'))=='0' and epoch and now-epoch['created']<1800)
        if allowed:
            count=db.execute("SELECT COUNT(*) FROM bot_dialogue_receipts WHERE chat=? AND epoch=? AND status IN ('pending','delivered')",(chat,epoch['id'])).fetchone()[0]
            last=db.execute("SELECT MAX(created) FROM bot_dialogue_receipts WHERE chat=? AND status IN ('pending','delivered')",(chat,)).fetchone()[0]
            allowed=count<2 and (last is None or now-last>=5)
        status=('pending' if phase=='observe' else 'delivered') if allowed else 'ignored'
        db.execute('INSERT INTO bot_dialogue_receipts VALUES(?,?,?,?,?)',(chat,message,epoch['id'] if epoch else 0,status,now))
        db.execute('DELETE FROM bot_dialogue_receipts WHERE chat=? AND rowid NOT IN (SELECT rowid FROM bot_dialogue_receipts WHERE chat=? ORDER BY rowid DESC LIMIT 1000)',(chat,chat))
        return allowed
