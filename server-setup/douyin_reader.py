"""Bounded Douyin reading adapter; heavy extraction is explicitly a local worker.

The 512 MB server may import this module for request/result validation without
importing yt-dlp, Whisper, torch, browsers, or downloading media. No service or
MCP registration is performed by importing it.
"""
import contextlib,importlib.util,io,json,math,os,re,shutil,subprocess,tempfile,threading
from pathlib import Path
from urllib.parse import parse_qs,urljoin,urlsplit
import urllib.error,urllib.request

MAX_TEXT=16000
MAX_TRANSCRIPT=20000
MAX_METADATA_RESPONSE=1000000
MAX_DURATION=600
MAX_MEDIA_BYTES=64*1024*1024
MAX_RESULT_BYTES=50*1024
ALLOWED_HOSTS={'douyin.com','www.douyin.com','v.douyin.com','iesdouyin.com','www.iesdouyin.com'}
MODES={'metadata','transcript'}
_URL=re.compile(r'https?://[^\s<>"\u3000，。；！？、）】]+',re.I)
_ID=re.compile(r'^[0-9]{10,24}$')

class VideoReadError(ValueError):
    def __init__(self,message,code='video_read_failed'):
        super().__init__(message)
        self.code=code


def normalize_share(value):
    """Accept a single Douyin URL or the application's full share text."""
    if not isinstance(value,str) or not value.strip() or len(value)>MAX_TEXT:
        raise VideoReadError('需要抖音视频链接或分享文案，最多16000字')
    candidates=_URL.findall(value)
    if not candidates:raise VideoReadError('未找到抖音 HTTPS 分享链接')
    found=[]
    for candidate in candidates:
        candidate=candidate.rstrip('.,;!?，。；！？、)）]】}')
        parts=urlsplit(candidate)
        try:port=parts.port
        except ValueError:raise VideoReadError('无效链接端口') from None
        if parts.scheme!='https' or parts.hostname not in ALLOWED_HOSTS:continue
        if parts.username or parts.password or port not in (None,443):raise VideoReadError('分享链接不能带账号密码或自定义端口')
        if '\\' in candidate or any(ord(c)<32 for c in candidate):raise VideoReadError('无效分享链接')
        path=parts.path
        match=re.fullmatch(r'/(?:video|share/video)/([0-9]{10,24})/?',path)
        query=parse_qs(parts.query)
        identifier=match.group(1) if match else next((v[0] for k,v in query.items() if k in ('modal_id','aweme_id') and len(v)==1 and _ID.fullmatch(v[0])),None)
        if identifier:
            item={'url':'https://www.douyin.com/video/'+identifier,'video_id':identifier}
        elif parts.hostname=='v.douyin.com' and re.fullmatch(r'/[a-zA-Z0-9_-]{4,80}/?',path):
            item={'url':'https://v.douyin.com/'+path.strip('/')+'/','video_id':None}
        else:continue
        if item not in found:found.append(item)
    if len(found)!=1:raise VideoReadError('请提供一条明确的抖音视频分享链接；不支持主页、搜索、直播或图文')
    return found[0]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args):return None


def resolve_video_url(value,opener=None):
    """Resolve only trusted share redirects; external/private destinations are refused."""
    normalized=normalize_share(value)
    if normalized['video_id']:return normalized
    opener=opener or urllib.request.build_opener(_NoRedirect())
    url=normalized['url']
    for _ in range(5):
        request=urllib.request.Request(url,method='HEAD',headers={'User-Agent':'Mozilla/5.0'})
        try:
            with opener.open(request,timeout=12) as response:
                destination=response.geturl()
                if destination!=url:raise VideoReadError('分享链接跳转未经过验证')
                raise VideoReadError('分享链接未提供可读取的视频地址，请发视频长链接')
        except urllib.error.HTTPError as error:
            if error.code not in (301,302,303,307,308):raise VideoReadError('分享链接暂时无法读取') from None
            destination=urljoin(url,error.headers.get('Location',''))
        except (urllib.error.URLError,TimeoutError,OSError):raise VideoReadError('分享链接暂时不可达') from None
        normalized=normalize_share(destination)
        if normalized['video_id']:return normalized
        url=normalized['url']
    raise VideoReadError('分享链接跳转次数过多')


def request(value):
    if not isinstance(value,dict) or set(value)-{'url','mode'}:raise VideoReadError('仅接受url与mode')
    mode=value.get('mode','metadata')
    if mode not in MODES:raise VideoReadError('mode使用metadata或transcript')
    return {**normalize_share(value.get('url')),'mode':mode}


def project_result(value):
    """Never forward raw downloader JSON, signed media URLs, cookies or local paths."""
    if not isinstance(value,dict) or value.get('state')!='ok':raise VideoReadError('视频读取未取得可验证结果')
    source=normalize_share(value.get('source_url'))
    identifier=source['video_id']
    if not identifier:raise VideoReadError('结果缺少真实视频ID')
    text=value.get('transcript')
    text=text if isinstance(text,str) else ''
    duration=value.get('duration_seconds')
    if type(duration) not in (int,float) or not math.isfinite(duration) or duration<0:duration=None
    result={'state':'ok','source_url':source['url'],'video_id':identifier,
        'title':str(value.get('title') or '')[:500],
        'author':str(value.get('author') or '')[:200],
        'description':str(value.get('description') or '')[:4000],
        'duration_seconds':duration,'transcript':text[:MAX_TRANSCRIPT],
        'transcript_source':'local_whisper_asr' if text else None,
        'transcript_truncated':len(text)>MAX_TRANSCRIPT or bool(value.get('transcript_truncated')),
        'transcript_saved_locally':bool(text and value.get('transcript_saved_locally')),
        'capabilities':{'metadata':True,'platform_captions':False,'audio_transcript':bool(text),'video_frames':False},
        'note':'音轨转写不能覆盖画面信息、字幕贴纸、无声动作或全部笑点。' if text else '只取得视频标题、作者和简介，尚未读取音轨或画面。'}
    # The existing SSH transport limits a UTF-8 line to 64 KiB. Keep headroom
    # for its result envelope, including for Chinese text and JSON escaping.
    while len(json.dumps(result,ensure_ascii=False,separators=(',',':')).encode('utf-8'))>MAX_RESULT_BYTES:
        if result['transcript']:
            result['transcript']=result['transcript'][:-256]
            result['transcript_truncated']=True
        elif result['description']:
            result['description']=result['description'][:-256]
        else:raise VideoReadError('视频结果超过传输上限','result_too_large')
    return result


class DouyinReader:
    """Small server facade; worker is an injected, separately authorized transport."""
    def __init__(self,worker=None):self.worker=worker
    def call(self,args):
        clean=request(args)
        if self.worker is None:
            return {'state':'unavailable','reason':'local_video_worker_not_connected','url':clean['url'],
                'note':'抖音本机读取端尚未连接，当前没有读取到视频内容。'}
        try:return project_result(self.worker(clean))
        except VideoReadError:raise
        except Exception:raise VideoReadError('抖音读取失败；未取得视频内容，也未自动重复下载') from None


class LocalVideoWorker:
    """Windows-only manual adapter reusing video2text.transcribe; no background service.

    Trusted operator supplies helper/cookie paths, never model tool arguments.
    Metadata mode does not load a speech model. Worker processes one job at a time.
    """
    def __init__(self,video2text_root,cookie_file=None,*,extractor_factory=None,transcriber=None,
                 browser_node=None,playwright_module=None,output_directory=None):
        self.root=Path(video2text_root)
        self.cookie=Path(cookie_file) if cookie_file else None
        self.extractor_factory=extractor_factory
        self.transcriber=transcriber
        self.browser_node=Path(browser_node) if browser_node else None
        self.playwright_module=Path(playwright_module) if playwright_module else None
        self.output_directory=Path(output_directory) if output_directory else self.root/'output'/'douyin'
        self.busy=threading.Lock()
    def dependencies(self):
        return {'yt_dlp':bool(importlib.util.find_spec('yt_dlp')),
            'faster_whisper':bool(importlib.util.find_spec('faster_whisper')),
            'helper_script':(self.root/'video2text.py').is_file(),
            'ffmpeg':bool(shutil.which('ffmpeg')),
            'public_browser_fallback':bool(self.browser_node and self.browser_node.is_file() and self.playwright_module and self.playwright_module.exists()),
            'cookie_file_present':bool(self.cookie and self.cookie.is_file() and self.cookie.stat().st_size)}
    @contextlib.contextmanager
    def cookie_copy(self,directory):
        if self.cookie is None:yield None;return
        if not self.cookie.is_file() or not self.cookie.stat().st_size:raise VideoReadError('本机抖音Cookie文件尚未配置')
        if self.cookie.stat().st_size>1024*1024:raise VideoReadError('本机Cookie文件异常')
        path=Path(directory)/'cookies.private.txt'
        path.write_bytes(self.cookie.read_bytes().removeprefix(b'\xef\xbb\xbf'))
        try:yield str(path)
        finally:path.unlink(missing_ok=True)
    def _extractor(self,options):
        if self.extractor_factory:return self.extractor_factory(options)
        try:
            from yt_dlp import YoutubeDL
        except ImportError:raise VideoReadError('本机读取端缺少yt-dlp依赖；服务器不应安装GPU转写模型') from None
        return YoutubeDL(options)
    def _transcribe(self,audio):
        if self.transcriber:return self.transcriber(str(audio))
        helper=self.root/'video2text.py'
        if not helper.is_file():raise VideoReadError('未找到既有video2text脚本')
        spec=importlib.util.spec_from_file_location('_fatfish_video2text',helper)
        module=importlib.util.module_from_spec(spec)
        # The existing helper prints progress. The worker's stdout is JSON only.
        with contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
            spec.loader.exec_module(module)
            return module.plain_transcript(module.transcribe(str(audio),'large-v3','cuda','zh'))
    def _browser_metadata(self,identifier):
        if not (self.browser_node and self.playwright_module):
            raise VideoReadError('抖音直接接口读取受阻，且未连接本机公开网页读取器','douyin_extraction_blocked')
        script=Path(__file__).with_name('douyin-browser.cjs')
        try:
            child=subprocess.run([str(self.browser_node),str(script),str(self.playwright_module),identifier],
                capture_output=True,encoding='utf-8',errors='replace',timeout=45,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
            if len(child.stdout.encode('utf-8'))>MAX_METADATA_RESPONSE:raise ValueError()
            response=json.loads(child.stdout)
            info=response.get('info')
            if child.returncode or response.get('state')!='ok' or not isinstance(info,dict):raise ValueError()
            return info
        except (OSError,subprocess.TimeoutExpired,ValueError):
            raise VideoReadError('抖音公开网页未返回该视频的可读取内容，尚未取得音轨','douyin_public_page_unavailable') from None
    def _save_transcript(self,canonical,raw):
        self.output_directory.mkdir(parents=True,exist_ok=True)
        target=self.output_directory/(canonical['video_id']+'.fatfish.md')
        text='# '+str(raw.get('title') or canonical['video_id'])+'\n\n'
        text+='- 链接: '+canonical['url']+'\n- 来源: 本机 Whisper 音轨转写\n\n## 逐字稿\n\n'+raw['transcript']+'\n'
        temporary=target.with_suffix('.tmp')
        try:
            temporary.write_text(text,encoding='utf-8')
            os.replace(temporary,target)
        finally:temporary.unlink(missing_ok=True)
        raw['transcript_saved_locally']=True
    def read(self,args):
        clean=request(args)
        if not self.busy.acquire(blocking=False):raise VideoReadError('本机正在读取另一个视频，请稍后再试')
        try:
            canonical=resolve_video_url(clean['url'])
            with tempfile.TemporaryDirectory(prefix='fatfish-douyin-') as directory,self.cookie_copy(directory) as cookie:
                class QuietLogger:
                    def debug(self,*args):pass
                    def warning(self,*args):pass
                    def error(self,*args):pass
                def bounds(info,*,incomplete=False):
                    if info.get('is_live'):return 'live streams are not supported'
                    duration=info.get('duration')
                    if duration is None or type(duration) not in (int,float) or not math.isfinite(duration):return 'duration is unknown'
                    if duration>MAX_DURATION:return 'video is longer than the configured limit'
                def progress(data):
                    if data.get('downloaded_bytes',0)>MAX_MEDIA_BYTES:raise VideoReadError('视频超过64MiB读取上限')
                opts={'quiet':True,'no_warnings':True,'logger':QuietLogger(),'noplaylist':True,
                    'socket_timeout':15,'retries':1,'extractor_retries':1,'fragment_retries':1,
                    'cookiefile':cookie,'cachedir':False,'skip_download':True,
                    'max_filesize':MAX_MEDIA_BYTES,'progress_hooks':[progress],
                    'outtmpl':str(Path(directory)/'%(id)s.%(ext)s')}
                try:
                    browser_used=False
                    try:
                        with self._extractor(opts) as downloader:info=downloader.extract_info(canonical['url'],download=False)
                    except Exception:
                        info=self._browser_metadata(canonical['video_id'])
                        browser_used=True
                    if not isinstance(info,dict) or str(info.get('id'))!=canonical['video_id'] or info.get('_type') in ('playlist','multi_video'):
                        raise VideoReadError('未取得对应的单条抖音视频')
                    raw={'state':'ok','source_url':canonical['url'],'title':info.get('title'),
                        'author':info.get('uploader') or info.get('channel'),'description':info.get('description'),
                        'duration_seconds':info.get('duration')}
                    if clean['mode']=='transcript':
                        reason=bounds(info)
                        if reason:raise VideoReadError('视频时长未知、超过10分钟或为直播，暂不下载音轨')
                        media_opts=dict(opts,skip_download=False,format='bestaudio/best',match_filter=bounds)
                        with self._extractor(media_opts) as downloader:
                            if browser_used:downloader.process_ie_result(info,download=True)
                            else:downloader.extract_info(canonical['url'],download=True)
                        audios=[p for p in Path(directory).iterdir() if p.is_file() and p.suffix.lower() in ('.mp4','.m4a','.mp3','.webm','.opus','.wav')]
                        if len(audios)!=1 or audios[0].stat().st_size>MAX_MEDIA_BYTES:raise VideoReadError('没有取得大小受控的单条视频音轨')
                        raw['transcript']=self._transcribe(audios[0])
                        if not isinstance(raw['transcript'],str) or not raw['transcript'].strip():raise VideoReadError('音轨没有得到可用转写')
                        self._save_transcript(canonical,raw)
                    return project_result(raw)
                except VideoReadError:raise
                except BaseException as error:
                    # video2text may use SystemExit for missing Whisper; do not leak raw logs/cookies.
                    if isinstance(error,(KeyboardInterrupt,GeneratorExit)):raise
                    raise VideoReadError('抖音提取或转写未成功；可能需要更新本机依赖或Cookie，请先检查后再试') from None
        finally:self.busy.release()
