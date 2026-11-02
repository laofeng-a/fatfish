"""Small, bounded Bilibili web client; no browser, downloads or account discovery."""
import hashlib
import http.cookiejar
import html
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

MIXIN=(46,47,18,2,53,8,23,32,15,50,10,31,58,3,45,35,27,43,5,49,33,9,42,19,29,28,14,39,12,38,41,13,37,48,7,16,24,55,40,61,26,17,0,1,60,51,30,4,22,25,54,21,56,59,6,63,57,62,11,36,20,34,44,52)

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args): return None

class BiliClient:
    def __init__(self,cookie_file):
        self.cookies=http.cookiejar.MozillaCookieJar(str(cookie_file))
        try:self.cookies.load(ignore_discard=True,ignore_expires=False)
        except (OSError,http.cookiejar.LoadError):pass
        self.opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect(),urllib.request.HTTPCookieProcessor(self.cookies))
        self.key=None;self.key_time=0

    def _request(self,path,params=None,post=False,signed=False):
        params=dict(params or {})
        if signed:
            if not self.key or time.time()-self.key_time>1800:
                data=self._request('/x/web-interface/nav')
                wbi=data.get('wbi_img',{})
                raw=''.join(urllib.parse.urlsplit(wbi[k]).path.rsplit('/',1)[-1].split('.')[0] for k in ('img_url','sub_url'))
                self.key=''.join(raw[i] for i in MIXIN)[:32];self.key_time=time.time()
            params['wts']=int(time.time())
            params={k:re.sub(r"[!'()*]",'',str(v)) for k,v in params.items()}
            encoded=urllib.parse.urlencode(sorted(params.items()))
            params['w_rid']=hashlib.md5((encoded+self.key).encode()).hexdigest()
        data=urllib.parse.urlencode(params).encode()
        address='https://api.bilibili.com'+path
        headers={'User-Agent':'Mozilla/5.0','Referer':'https://www.bilibili.com/','Accept-Encoding':'identity'}
        if post:headers['Content-Type']='application/x-www-form-urlencoded'
        request=urllib.request.Request(address if post else address+'?'+data.decode(),data=data if post else None,headers=headers)
        try:
            with self.opener.open(request,timeout=18) as response:raw=response.read(2_000_001)
        except urllib.error.HTTPError as error:
            raise ValueError('B站返回 HTTP '+str(error.code)+'；没有完成本次操作') from None
        if len(raw)>2_000_000:raise ValueError('B站返回内容过大，请缩小范围')
        result=json.loads(raw)
        if result.get('code')!=0:raise ValueError('B站拒绝本次请求，代码 '+str(result.get('code')))
        data=result.get('data')
        if data is None:raise ValueError('B站没有返回有效数据，不能当成空结果')
        return data

    def resolve(self,value):
        if not isinstance(value,str) or len(value)>4096:raise ValueError('视频链接或编号不合法')
        match=re.search(r'BV[0-9A-Za-z]{10}',value)
        if match:return {'bvid':match.group(0)}
        match=re.search(r'(?:\bav|/av)([0-9]{1,20})',value,re.I)
        if match:return {'aid':int(match.group(1))}
        match=re.search(r'https://b23\.tv/[0-9A-Za-z]+',value)
        if not match:raise ValueError('需要 B站视频链接、b23.tv 短链或 BV/AV 编号')
        address=match.group(0)
        try:self.opener.open(urllib.request.Request(address,headers={'User-Agent':'Mozilla/5.0'}),timeout=12).close()
        except urllib.error.HTTPError as error:
            target=error.headers.get('Location','')
            host=urllib.parse.urlsplit(target).hostname
            if error.code not in (301,302,303,307,308) or host not in ('www.bilibili.com','m.bilibili.com','bilibili.com'):
                raise ValueError('短链接没有解析到允许的 B站视频地址') from None
            return self.resolve(target)
        raise ValueError('短链接没有返回可用的视频地址')

    def info(self,value):
        data=self._request('/x/web-interface/view',self.resolve(value))
        return {'bvid':data['bvid'],'aid':data['aid'],'title':data.get('title'),
                'uploader':data.get('owner',{}).get('name'),'uploader_id':data.get('owner',{}).get('mid'),
                'description':data.get('desc',''),'duration':data.get('duration'),'published_at':data.get('pubdate'),
                'url':'https://www.bilibili.com/video/'+data['bvid'],
                'parts':[{'part':p.get('part'),'page':p.get('page'),'cid':p.get('cid'),'duration':p.get('duration')} for p in data.get('pages',[])[:100]]}

    def normalize_url(self,value):
        match=re.search(r'https://(?:www\.|m\.)?(?:bilibili\.com|b23\.tv)/[^\s<>，。]+',value or '')
        address=match.group(0).rstrip(')）') if match else ''
        if address and urllib.parse.urlsplit(address).hostname=='b23.tv':
            try:self.opener.open(urllib.request.Request(address,headers={'User-Agent':'Mozilla/5.0'}),timeout=12).close()
            except urllib.error.HTTPError as error:
                destination=error.headers.get('Location','')
                if error.code not in (301,302,303,307,308) or urllib.parse.urlsplit(destination).hostname not in ('www.bilibili.com','m.bilibili.com','bilibili.com'):raise ValueError('短链目标不是 B站视频') from None
                address=destination
        target=self.resolve(address or value)
        query=urllib.parse.parse_qs(urllib.parse.urlsplit(address).query)
        part=query.get('p',['1'])[0]
        if not part.isdigit() or not 1<=int(part)<=100:raise ValueError('分 P 编号不合法')
        keep={'p':part}
        stamp=query.get('t',[''])[0]
        if stamp:
            try:valid=0<=float(stamp)<=604800
            except ValueError:valid=False
            if valid:keep['t']=stamp
        return 'https://www.bilibili.com/video/'+(target.get('bvid') or 'av'+str(target['aid']))+'?'+urllib.parse.urlencode(keep)

    @staticmethod
    def _comment(row,depth=0):
        return {'id':str(row.get('rpid')),'root':str(row.get('root',0)),'parent':str(row.get('parent',0)),
                'author':row.get('member',{}).get('uname'),'author_id':row.get('mid'),
                'text':row.get('content',{}).get('message','')[:600],'text_truncated':len(row.get('content',{}).get('message',''))>600,'time':row.get('ctime'),
                'likes':row.get('like',0),'reply_count':row.get('rcount',0),
                'replies':[BiliClient._comment(r,depth+1) for r in (row.get('replies') or [])[:2]] if depth==0 else []}

    def comments(self,value,order='hot',cursor='',root=None,page=1):
        if order not in ('hot','new') or not isinstance(cursor,str) or len(cursor)>2048:raise ValueError('评论排序或游标不合法')
        info=self.info(value)
        if root:
            if not str(root).isdigit() or not 1<=int(page)<=100:raise ValueError('楼中楼参数不合法')
            data=self._request('/x/v2/reply/reply',{'oid':info['aid'],'type':1,'root':root,'pn':page,'ps':20})
            return {'url':info['url'],'comments':[self._comment(r) for r in data.get('replies') or []],
                    'page':page,'root':root,'next_page':page+1 if len(data.get('replies') or [])>=20 else None}
        data=self._request('/x/v2/reply/wbi/main',{'oid':info['aid'],'type':1,'mode':3 if order=='hot' else 2,
                   'pagination_str':json.dumps({'offset':cursor},separators=(',',':')),'plat':1,'web_location':1315875},signed=True)
        result=[self._comment(r) for r in (data.get('replies') or [])[:20]]
        pinned=data.get('upper',{}).get('top') if isinstance(data.get('upper'),dict) else None
        return {'url':info['url'],'order':order,'comments':result,'pinned':self._comment(pinned) if pinned else None,
                'next_cursor':data.get('cursor',{}).get('pagination_reply',{}).get('next_offset'),
                'is_end':data.get('cursor',{}).get('is_end')}

    @staticmethod
    def _video(row):
        bvid=row.get('bvid')
        return {'bvid':bvid,'title':html.unescape(re.sub('<[^>]+>','',row.get('title',''))),
                'uploader':row.get('author') or row.get('owner',{}).get('name'),
                'url':'https://www.bilibili.com/video/'+bvid if bvid else row.get('arcurl'),
                'description':row.get('description') or row.get('desc'),'published_at':row.get('pubdate') or row.get('created')}

    def discover(self,mode,query,limit=10):
        if type(limit) is not int or not 1<=limit<=20 or not isinstance(query,str) or len(query)>200:raise ValueError('发现参数不合法')
        if mode=='search':
            data=self._request('/x/web-interface/wbi/search/type',{'search_type':'video','keyword':query,'page':1,'page_size':limit,'order':'totalrank'},signed=True)
            rows=data.get('result') or []
        elif mode=='related':rows=self._request('/x/web-interface/archive/related',self.resolve(query))
        elif mode=='uploader':
            if not query.isdigit():raise ValueError('UP 主查询需要数字 UID')
            data=self._request('/x/space/wbi/arc/search',{'mid':query,'pn':1,'ps':min(20,limit),'order':'pubdate'},signed=True)
            rows=data.get('list',{}).get('vlist') or []
        else:raise ValueError('发现模式应为 search、related 或 uploader')
        return {'mode':mode,'query':query,'videos':[self._video(r) for r in rows[:limit]]}

    def post(self,value,text,root=None,parent=None):
        csrf=next((c.value for c in self.cookies if c.name=='bili_jct'),None)
        if not csrf:raise ValueError('现有登录态缺少发布校验字段，需要更新登录态')
        if not isinstance(text,str) or not 1<=len(text)<=500:raise ValueError('评论需要 1 到 500 字')
        info=self.info(value)
        params={'oid':info['aid'],'type':1,'message':text,'csrf':csrf,'plat':1}
        for key,val in (('root',root),('parent',parent)):
            if val:
                if not str(val).isdigit():raise ValueError('回复目标无效')
                params[key]=str(val)
        data=self._request('/x/v2/reply/add',params,post=True)
        return {'url':info['url'],'comment_id':str(data.get('rpid') or data.get('reply',{}).get('rpid')),'status':'submitted'}
