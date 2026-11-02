"""Bounded, fixed readers. No shell, posting, follows, likes or account changes."""
import datetime as dt
import json
import os
from pathlib import Path
import subprocess
import threading
import urllib.parse
import re
from bilibili_client import BiliClient

TOOLS=[
    {'name':'x_post_read','description':'读取X帖子、部分回复、引用与配图链接。',
     'inputSchema':{'type':'object','properties':{'url':{'type':'string'},'limit':{'type':'integer','minimum':1,'maximum':20}},'required':['url'],'additionalProperties':False}},
    {'name':'x_feed','description':'浏览专用X账号的推荐或关注信息流。',
     'inputSchema':{'type':'object','properties':{'kind':{'type':'string','enum':['for-you','following'],'default':'for-you'},
         'limit':{'type':'integer','minimum':1,'maximum':20,'default':10}},'additionalProperties':False}},
    {'name':'bilibili_video','description':'读取B站视频标题、简介与可访问字幕；content_available说明字幕获取情况，next_offset继续读长字幕。',
     'inputSchema':{'type':'object','properties':{'url':{'type':'string','description':'B 站单个视频或 b23.tv 短链接'},
         'offset':{'type':'integer','minimum':0,'default':0}},'required':['url'],'additionalProperties':False}},
    {'name':'yesterday_health','description':'读取UTC+8昨日已同步的睡眠、步数和运动记录，并返回数据完整情况。',
     'inputSchema':{'type':'object','properties':{},'additionalProperties':False}},
]
for tool in TOOLS:
    tool['annotations']={'readOnlyHint':True,'destructiveHint':False,'openWorldHint':True}
BUSY=threading.Lock()
PYTHON='/opt/picoclaw/readers-venv/bin/python'
DIRECTORY=Path('/etc/picoclaw-memory')

def call(name,args):
    if not isinstance(args,dict):raise ValueError('读取参数必须为对象')
    if name=='yesterday_health':
        today=dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).date()
        day=(today-dt.timedelta(days=1)).isoformat()
        path=Path('/var/lib/picoclaw-readers/health')/(day+'.json')
        if not path.exists():
            return {'date':day,'timezone':'UTC+8','status':'awaiting_mobile_sync',
                    'sleep':None,'steps':None,'workouts':None,'missing':['sleep','steps','workouts'],
                    'note':'手机端尚未提供这一天的数据；缺失不代表零'}
        if path.stat().st_size>1_048_576:raise ValueError('健康摘要文件过大')
        data=json.loads(path.read_text())
        if not isinstance(data,dict) or data.get('date')!=day:raise ValueError('健康摘要日期与昨日不一致')
        result={k:data.get(k) for k in ('date','timezone','source','synced_at','sleep','steps','workouts')}
        result['missing']=[k for k in ('sleep','steps','workouts') if result[k] is None]
        result['status']='partial' if result['missing'] else 'available'
        return result
    if name in ('x_feed','x_post_read'):
        kind=args.get('kind','for-you');limit=args.get('limit',10)
        if kind not in ('for-you','following') or type(limit) is not int or not 1<=limit<=20:
            raise ValueError('信息流类型或条数不合法')
        session_path=DIRECTORY/'x-session.json'
        if not session_path.exists():raise ValueError('专用 X 登录态尚未配置')
        session=json.loads(session_path.read_text())
        env=dict(os.environ,TWITTER_AUTH_TOKEN=session['auth_token'],TWITTER_CT0=session['ct0'],
                 PYTHONIOENCODING='utf-8',NO_COLOR='1')
        command=[PYTHON,'/opt/picoclaw/fatfish-v1/fetch_feed.py','--kind',kind,'--limit',str(limit)]
        if name=='x_post_read':
            address=urllib.parse.urlsplit(args.get('url',''))
            match=re.fullmatch(r'/[^/]+/status/(\d+)(?:/.*)?',address.path)
            if address.scheme!='https' or address.hostname not in ('x.com','www.x.com','twitter.com','www.twitter.com') or not match:raise ValueError('需要一个 X 帖子链接')
            command=[PYTHON,'/opt/picoclaw/fatfish-v1/fetch_feed.py','--post-id',match.group(1),'--limit',str(limit)]
        secrets=[session['auth_token'],session['ct0']]
    elif name=='bilibili_video':
        url=args.get('url');offset=args.get('offset',0)
        if not isinstance(url,str) or len(url)>2048 or type(offset) is not int or offset<0:
            raise ValueError('视频地址或字幕偏移不合法')
        url=BiliClient(DIRECTORY/'bili-cookies.txt').normalize_url(url)
        env=dict(os.environ,PYTHONIOENCODING='utf-8',BILI_COOKIE_FILE=str(DIRECTORY/'bili-cookies.txt'))
        command=['/usr/bin/python3','/opt/picoclaw/fatfish-v1/bili-reader.py','--url',url,'--offset',str(offset)]
        secrets=[]
    else:raise ValueError('未知读取工具')
    BUSY.acquire()
    try:
        check_memory()
        try:process=subprocess.run(command,env=env,capture_output=True,timeout=75)
        except subprocess.TimeoutExpired:raise ValueError('读取超时，没有完成本次内容获取')
        if process.returncode:
            raise ValueError('读取未成功；登录态可能过期，或平台暂时拒绝访问，没有取得本次内容')
        raw=process.stdout.decode('utf-8-sig')
        for secret in secrets:
            if secret:raw=raw.replace(secret,'<REDACTED>')
        if len(raw)>1_000_000:raise ValueError('内容过多，请缩小读取范围')
        result=json.loads(raw)
        if name in ('x_feed','x_post_read'):return {'fetched_at':dt.datetime.now(dt.timezone.utc).isoformat(),'feed':kind if name=='x_feed' else 'post','data':result}
        return result
    finally:BUSY.release()

def check_memory():
    path=Path('/proc/meminfo')
    if path.exists():
        available=next(int(line.split()[1]) for line in path.read_text().splitlines() if line.startswith('MemAvailable:'))
        if available<80*1024:raise ValueError('当前可用内存偏低，暂缓这项读取；普通文字聊天仍可使用')
