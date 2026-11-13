"""Scoped conversation reads and small, factual turn receipts; no shell access."""
import datetime as dt
import json
import re
import time

from memory_recall import prepare, rank_match

CHAT=re.compile(r'-?[1-9][0-9]{0,19}\Z')
MESSAGE=re.compile(r'[1-9][0-9]{0,19}\Z')
STATES={'processing','completed','no_reply','error','interrupted'}

class ConversationAccess:
    def __init__(self,runtime):
        self.r=runtime
        with runtime.db() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS conversation_receipts(
                chat TEXT,msg TEXT,sender TEXT,mentioned INTEGER,replied INTEGER,
                received REAL,state TEXT,updated REAL,PRIMARY KEY(chat,msg))''')

    def scope(self,args,context,chat):
        target=args.get('chat_id',chat)
        if not isinstance(target,str) or not CHAT.fullmatch(target):raise ValueError('chat_id需要真实会话编号，可先用telegram_chats查看登记群')
        return target

    def receive(self,db,data):
        chat=str(data.get('chat_id',''));msg=str(data.get('message_id',''))
        if not CHAT.fullmatch(chat) or not MESSAGE.fullmatch(msg):return
        now=time.time()
        db.execute('INSERT OR IGNORE INTO conversation_receipts VALUES(?,?,?,?,?,?,?,?)',
                   (chat,msg,str(data.get('sender_id','')),int(data.get('mentioned') is True),int(data.get('reply_to_self') is True),now,'received',now))
        db.execute('DELETE FROM conversation_receipts WHERE chat=? AND rowid NOT IN (SELECT rowid FROM conversation_receipts WHERE chat=? ORDER BY rowid DESC LIMIT 2000)',(chat,chat))

    def trace(self,data):
        chat=str(data.get('chat_id',''));msg=str(data.get('message_id',''));state=data.get('state')
        if not CHAT.fullmatch(chat) or not MESSAGE.fullmatch(msg) or state not in STATES:return {'allowed':False}
        with self.r.db() as db:
            if not self.r._allowed(db,chat):return {'allowed':False}
            row=db.execute('SELECT state FROM conversation_receipts WHERE chat=? AND msg=?',(chat,msg)).fetchone()
            if not row:return {'allowed':True}
            # Replayed observations must not erase a finished turn.
            if state=='processing' and row['state'] not in ('received','processing'):return {'allowed':True}
            db.execute('UPDATE conversation_receipts SET state=?,updated=? WHERE chat=? AND msg=?',(state,time.time(),chat,msg))
        return {'allowed':True}

    @staticmethod
    def timestamp(value):
        if not isinstance(value,str) or len(value)>64:raise ValueError('时间需要含时区的ISO格式，如2026-10-22T22:50:00+08:00')
        try:
            parsed=dt.datetime.fromisoformat(value.replace('Z','+00:00'))
            if parsed.tzinfo is None:raise ValueError()
            return parsed.timestamp()
        except (ValueError,OverflowError):raise ValueError('时间需要含时区的ISO格式') from None

    def history(self,name,args,context,chat):
        target=self.scope(args,context,chat)
        with self.r.db() as db:
            if name=='history_expand':
                identifier=args.get('id')
                if type(identifier) is not int or identifier<1:raise ValueError('需要history_find返回的真实id')
                row=db.execute('SELECT * FROM events WHERE id=? AND chat=?',(identifier,target)).fetchone()
                if not row:raise ValueError('这条记录不存在或不在指定会话范围；跨群时同时指定chat_id')
                return dict(row)
            query=args.get('query','');limit=args.get('limit',8);kind=args.get('kind','all')
            if not isinstance(query,str) or type(limit) is not int or not 1<=limit<=20:raise ValueError('检索参数不合法')
            if kind not in ('all','user','assistant','reading'):raise ValueError('未知记录类别')
            query=query.strip()[:120];snippet=1600 if query else 400
            params=[target];where=['chat=?']
            if not query:where.append("content!=''")
            if kind=='user':where.append("kind IN ('user','member','forwarded','bot')")
            elif kind!='all':where.append('kind=?');params.append(kind)
            for field,operator in (('after','>='),('before','<=')):
                if field in args:where.append('created'+operator+'?');params.append(self.timestamp(args[field]))
            if 'after' in args and 'before' in args and self.timestamp(args['after'])>self.timestamp(args['before']):raise ValueError('after不能晚于before')
            if not query:
                rows=db.execute(f'SELECT id,chat AS chat_id,msg,kind,substr(content,1,{snippet}) AS snippet,created FROM events WHERE '+' AND '.join(where)+' ORDER BY id DESC LIMIT ?',params+[limit])
                records=[dict(row) for row in rows]
            else:
                # Token overlap, not one contiguous substring. Newer id only breaks ties.
                fetched=list(db.execute('SELECT id,chat AS chat_id,msg,kind,content,created FROM events WHERE '+' AND '.join(where),params))
                idf,average=prepare(query,(row['content'] for row in fetched))
                ranked=[]
                for row in fetched:
                    exact,score,hits=rank_match(row['content'],query,idf,average)
                    if not score and not exact:continue
                    ranked.append((1 if exact else 0,score,row['id'],row,hits))
                ranked.sort(key=lambda item:item[:3],reverse=True)
                records=[]
                for _exact,_score,_id,row,_hits in ranked[:limit]:
                    item={key:row[key] for key in ('id','chat_id','msg','kind','created')}
                    item['snippet']=row['content'][:snippet]
                    records.append(item)
            result={'chat_id':target,'records':records}
            if not query:
                result['view']='recent'
                if target==self.r.owner:
                    result['open_topics']=[dict(r) for r in db.execute("SELECT id,substr(content,1,320) AS content,source_id,created,expires FROM memories WHERE chat=? AND category='topic' AND active=1 AND (expires IS NULL OR expires>?) ORDER BY id DESC LIMIT 4",(target,time.time()))]
                    result['recent_proactive_shares']=[dict(r) for r in db.execute("SELECT kind,substr(content,1,320) AS content,created,message_ids FROM sends WHERE chat=? AND mode='background' AND status='sent' ORDER BY created DESC LIMIT 3",(target,))]
            return result

    def status(self,args,context,chat):
        target=self.scope(args,context,chat);msg=args.get('message_id')
        if msg is not None and (not isinstance(msg,str) or not MESSAGE.fullmatch(msg)):raise ValueError('message_id需要真实消息编号，可先history_find定位')
        with self.r.db() as db:
            titles=json.loads(self.r._get(db,'group_titles','{}'))
            result={'chat_id':target,'title':'与jg私聊' if target==self.r.owner else titles.get(target,'已登记群'),
                    'registered':True,'group_chat_mode':self.r.settings.get('group_chat_mode','owner') if target.startswith('-') else None,
                    'peer_bot_chat':self.r.settings.get('group_bot_chat') is True if target.startswith('-') else None,
                    'platform_permissions':'未实时查询Telegram平台权限；收到记录与以下处理回执才是本工具证据'}
            if chat==self.r.owner:
                inventory=json.loads(self.r._get(db,'agent_tool_inventory','[]'))
                result['capabilities']={'history':'当前私聊及全部已登记群，可关键词、时间筛选、展开原文',
                    'delivery':'可向当前私聊和登记群发文字、表情、引用回复与消息反应',
                    'memory':'自主整理自己的结构化记忆与文字笔记；共享库只读',
                    'codex_control':'已接入，电脑连接'+('在线' if self.r.codex.online(db) else '离线') if self.r.settings.get('codex_bridge_token_file') else '尚未接入',
                    'server_shell':'exec已加载，可在Linux服务器执行命令；Codex执行端在jg的Windows电脑' if 'exec' in inventory else '当前工具清单未记录exec；可查看available_tools',
                    'available_tools':inventory,'inventory_updated_at':self.r._get(db,'agent_tool_inventory_at'),
                    'interpretation':'查不到记录不等于平台没收到；no_reply表示模型选择安静；rejected是明确拒绝，unknown是无法确认，partial仅部分动作成功；只把delivery_receipt里的内容视为已送达'}
            if msg is None:
                result['recent_messages']=[self.message_status(db,target,r['msg']) for r in db.execute("SELECT msg FROM events WHERE chat=? AND msg IS NOT NULL AND kind IN ('user','forwarded','member','bot') ORDER BY id DESC LIMIT 6",(target,))]
            else:result['message']=self.message_status(db,target,msg)
            return result

    def message_status(self,db,chat,msg):
        event=db.execute("SELECT id,created FROM events WHERE chat=? AND msg=? AND kind IN ('user','forwarded','member','bot') ORDER BY id DESC LIMIT 1",(chat,msg)).fetchone()
        receipt=db.execute('SELECT * FROM conversation_receipts WHERE chat=? AND msg=?',(chat,msg)).fetchone()
        result={'message_id':msg,'recorded':event is not None,'received_at':event['created'] if event else None,
                'mentions_you':bool(receipt['mentioned']) if receipt else None,
                'replies_to_you':bool(receipt['replied']) if receipt else None,
                'processing':receipt['state'] if receipt else 'not_recorded',
                'delivery':'not_recorded','sent_message_ids':[]}
        send=db.execute('SELECT sends.ticket,sends.status,sends.message_ids FROM send_turns JOIN sends USING(ticket) WHERE send_turns.chat=? AND send_turns.msg=?',(chat,msg)).fetchone()
        if send:
            result['delivery']=send['status'];result['sent_message_ids']=json.loads(send['message_ids'] or '[]')
            result['delivery_receipt']=self.r.delivery_receipt(send['ticket'])
            result['delivery_actions']=self.r.delivery_actions(send['ticket'])
        if receipt and receipt['state']=='processing' and time.time()-receipt['updated']>1200:result['processing']='stale_processing_unknown'
        if receipt and receipt['state']=='no_reply' and not send:result['delivery']='intentionally_silent'
        if not event:result['note']='本地未找到这条消息，不能据此断言Telegram平台未投递'
        elif receipt is None:result['note']='旧记录缺少处理回执；只确认本地收到，不猜测为什么未回复'
        return result
