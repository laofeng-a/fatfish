"""Model-managed reads; legacy task parsing is retained for publication evidence."""
import json
import re
import time
import urllib.parse

READ_TOOLS={'x_feed':'x','x_post_read':'x','bilibili_video':'bili','bilibili_comments':'bili','bilibili_discover':'bili'}
LABELS={'x':'X','bili':'B站'}
NEGATIVE=re.compile(r'不要|不许|不能|不想|别(?:去|看|读|刷|查|搜|打开|调用|访问)|暂不|先不|不允许|不用|禁止|未经|未.{0,4}许可|先.{0,4}(?:问|商量)|能不能|可不可以|转发|转述|引用|他说|她说|有人说|原话')
READ=re.compile(r'看|读|刷|浏览|打开|访问|搜|查|获取|找|推荐|调用')
APPROVE=re.compile(r'^(?:可以|允许|同意|好[啊的吧]?|行[啊吧]?|去[吧呗啊]|去看吧|看吧|你去看看|可以去看|允许读取)[。！!～~\s\uFE0F\u200D\U0001F300-\U0001FAFF]*$')
CONTINUE=re.compile(r'^(?:继续|继续看|接着看|接着读|往下看|往下读|下一页|还有呢)[。！!\s]*$')
REVOKE=re.compile(r'暂停|停止|先停|算了|不用了|先别|别.*(?:看|读|刷)|不(?:看|读|刷)了|撤销|收回.*许可|取消.*(?:读取|任务)|\b(?:stop|cancel)\b',re.I)
TASK_TTL=1800
TASK_CALL_LIMIT=24

def instruction_words(text):
    return re.sub(r'[“"「『].*?[”"」』]',' ',re.sub(r'https?://\S+',' ',text))

def platform_url(address):
    host=(urllib.parse.urlsplit(str(address)).hostname or '').lower().rstrip('.')
    if host in ('x.com','twitter.com','pbs.twimg.com') or host.endswith(('.x.com','.twitter.com','.twimg.com')):return 'x'
    if host in ('bilibili.com','b23.tv','hdslb.com') or host.endswith(('.bilibili.com','.b23.tv','.hdslb.com')):return 'bili'
    return None

def mentions(text,platform):
    if platform=='x':return bool(re.search(r'(?i)(?<![a-z0-9])x(?![a-z0-9])|twitter|推特|twimg\.com',text))
    return bool(re.search(r'(?i)b站|哔哩|bilibili|b23\.tv|hdslb\.com',text))

def targets(text,platform):
    if platform=='x':return set(re.findall(r'https?://(?:www\.)?(?:x\.com|twitter\.com)/[^\s/]+/status/(\d+)',text,re.I))
    values=set(re.findall(r'BV[0-9A-Za-z]{10}',text))
    values.update('av'+v for v in re.findall(r'(?i)\bav(\d+)',text))
    values.update(re.findall(r'https://b23\.tv/[A-Za-z0-9]+',text))
    return values

def web_platforms(tool,args):
    if tool=='fetch':
        platform=platform_url(args.get('url',''));return [platform] if platform else []
    query=str(args.get('query',''))
    return [p for p in LABELS if mentions(query,p)]

class PlatformConsent:
    def __init__(self,runtime):
        self.runtime=runtime
        with runtime.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS platform_reads(chat TEXT,msg TEXT,platform TEXT,calls INTEGER,PRIMARY KEY(chat,msg,platform))')
            db.execute('CREATE TABLE IF NOT EXISTS message_replies(chat TEXT,msg TEXT,reply TEXT,PRIMARY KEY(chat,msg))')

    def _source(self,context,chat,platform):
        if context.get('session_key')=='heartbeat':raise ValueError('X和B站当前不允许后台自主读取；需要jg本人的许可')
        message=str(context.get('message_id',''))
        with self.runtime.db() as db:
            source=db.execute("SELECT * FROM events WHERE chat=? AND msg=? AND kind='user'",(chat,message)).fetchone()
            if not source or time.time()-source['created']>TASK_TTL:raise ValueError('没有jg当前有效的读取许可；请先询问，不要先读取再补许可')
            if CONTINUE.fullmatch(source['content'].strip()):
                previous=db.execute("SELECT e.* FROM events e JOIN platform_reads p ON p.chat=e.chat AND p.msg=e.msg WHERE e.chat=? AND e.kind='user' AND e.id<? AND p.platform=? AND p.calls>0 ORDER BY e.id DESC LIMIT 1",(chat,source['id'],platform)).fetchone()
                # A valid task may have failed before its first network call.
                # "继续" can resume that actual owner instruction too; it will
                # still pass all the normal instruction/scope checks below.
                candidates=db.execute("SELECT * FROM events WHERE chat=? AND kind='user' AND id<? AND created>=? ORDER BY id DESC LIMIT 8",(chat,source['id'],time.time()-TASK_TTL)).fetchall()
                pending=next((row for row in candidates if mentions(instruction_words(row['content']),platform) and READ.search(instruction_words(row['content'])) and not CONTINUE.fullmatch(row['content'].strip())),None)
                if pending and (not previous or pending['id']>previous['id']):previous=pending
                if not previous or time.time()-previous['created']>TASK_TTL:raise ValueError('没有尚在有效范围内的读取任务可以继续')
                source=previous;message=source['msg']
            newer=db.execute("SELECT content FROM events WHERE chat=? AND kind='user' AND id>?",(chat,source['id'])).fetchall()
            if any(REVOKE.search(row[0]) for row in newer):raise ValueError('jg已暂停或取消这次读取，旧许可不继续使用')
            text=source['content'].strip()
            # A reply such as "去呗，去刷刷大肥鱼相关视频" can omit the
            # platform just discussed. Only the adjacent exchange supplies it;
            # unrelated histories and retrieved/forwarded text do not.
            nearby=db.execute("SELECT kind,content FROM events WHERE chat=? AND id<? AND created>=? AND kind IN ('user','assistant','forwarded') ORDER BY id DESC LIMIT 2",(chat,source['id'],source['created']-300)).fetchall()
            adjacent_platforms={p for row in nearby if row['kind']!='forwarded' for p in LABELS if mentions(instruction_words(row['content']),p) and READ.search(instruction_words(row['content']))}
            contextual=bool(re.match(r'^(?:可以|好[啊的吧]?|行[啊吧]?|去[吧呗啊]?|那(?:就)?(?:你)?去|你去|接着|继续)',text))
            inferred_platform=platform if contextual and adjacent_platforms=={platform} else None
            if APPROVE.fullmatch(text):
                reply=db.execute('SELECT reply FROM message_replies WHERE chat=? AND msg=?',(chat,message)).fetchone()
                if reply:
                    previous=db.execute("SELECT * FROM events WHERE chat=? AND msg=? AND kind='assistant'",(chat,reply[0])).fetchone()
                    # The history now preserves confirmed paragraphs of partial
                    # deliveries. An incomplete bundle still cannot supply a
                    # complete permission question in strict mode.
                    incomplete=False
                    deliveries=db.execute("SELECT status,message_ids FROM sends WHERE chat=? AND created>=? AND created<=? ORDER BY created DESC LIMIT 30",(chat,source['created']-300,source['created'])).fetchall()
                    for delivery in deliveries:
                        try:ids=json.loads(delivery['message_ids'] or '[]')
                        except (ValueError,TypeError):continue
                        if str(reply[0]) in [str(identifier) for identifier in ids] and delivery['status']!='sent':incomplete=True;break
                    if incomplete:previous=None
                    elif not previous:
                        # A short reply can contain several Telegram message IDs.
                        # Only a fully confirmed send bundle in this chat can supply its question.
                        bundles=db.execute("SELECT content,created,message_ids FROM sends WHERE chat=? AND status='sent' AND created>=? AND created<=? ORDER BY created DESC LIMIT 30",(chat,source['created']-300,source['created'])).fetchall()
                        for bundle in bundles:
                            try:ids=json.loads(bundle['message_ids'] or '[]')
                            except (ValueError,TypeError):continue
                            if str(reply[0]) in [str(identifier) for identifier in ids]:
                                previous={'kind':'assistant','content':bundle['content'],'created':bundle['created']}
                                break
                else:
                    previous=db.execute("SELECT * FROM events WHERE chat=? AND id<? AND kind IN ('user','assistant','forwarded') ORDER BY id DESC LIMIT 1",(chat,source['id'])).fetchone()
                if not previous or previous['kind']!='assistant' or time.time()-previous['created']>300:raise ValueError('这句许可没有对应的近期读取请求；请明确平台和要读的范围')
                question=previous['content']
                direct_question=bool(re.search(r'吗|要不要|是否|能否|允许|许可|可以',question) and READ.search(question) and re.search(r'(?:我|让我).*(?:看|读|刷|搜|打开|访问|获取)|(?:允许|许可).*(?:看|读|刷|搜|打开|访问|获取)',question))
                if not direct_question and not (inferred_platform and READ.search(instruction_words(question))):raise ValueError('这句许可没有对应的近期平台读取话题；请先明确想读什么')
                text=question
            if inferred_platform and not any(mentions(instruction_words(text),p) for p in LABELS):
                text+='\n平台：'+LABELS[inferred_platform]
            if APPROVE.fullmatch(source['content'].strip()) and not targets(text,platform) and mentions(instruction_words(text),platform):
                # Approval of an open-ended platform reading request delegates
                # its discovery steps too; it is not limited to the exact
                # verbs the assistant happened to use in the question.
                text+='\n允许浏览'+LABELS[platform]
            result=dict(source);result['approved_question']=bool(APPROVE.fullmatch(source['content'].strip()))
            return result,text

    def _target_allowed(self,platform,value,source,chat):
        requested=targets(str(value),platform);given=targets(source['permission_text'],platform)
        if requested and requested<=given:return True
        if str(value) and str(value) in source['permission_text']:return True
        # A delegated discovery task may inspect results obtained in that same task.
        if not re.search(r'挑|推荐|看看|阅读|浏览|自己|随便|刷|搜|找|查|相关',source['permission_text']):return False
        return self._from_reading(str(value),source,chat,platform)

    def _from_reading(self,value,source,chat,platform,required_tool=None):
        with self.runtime.db() as db:
            rows=db.execute("SELECT content FROM events WHERE chat=? AND kind='reading' AND created>=? AND content LIKE ? ORDER BY id DESC LIMIT 12",(chat,source['created'],'%'+value+'%')).fetchall()
        for row in rows:
            try:tool=json.loads(row[0]).get('tool')
            except (ValueError,AttributeError):continue
            if READ_TOOLS.get(tool)==platform and (required_tool is None or tool==required_tool):return True
        return False

    def ensure(self,name,args,context,chat,platform=None):
        """Tool selection is the model's decision in every runtime scene."""
        return


def check_web_permission(tool,args,context):
    return
