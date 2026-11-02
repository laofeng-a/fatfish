"""Extract Bilibili metadata and subtitles only; never download audio or video."""
import argparse
import html
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import urllib.parse
import urllib.request
from yt_dlp import YoutubeDL

class Quiet:
    def debug(self,*args):pass
    def warning(self,*args):pass
    def error(self,*args):pass

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--url',required=True)
    parser.add_argument('--offset',type=int,default=0)
    args=parser.parse_args()
    parsed=urllib.parse.urlsplit(args.url)
    if parsed.scheme!='https' or parsed.username or parsed.password or parsed.hostname not in ('www.bilibili.com','m.bilibili.com','bilibili.com','b23.tv'):
        raise ValueError('Expected a Bilibili URL')
    url=args.url
    if parsed.hostname=='b23.tv':
        with urllib.request.urlopen(url,timeout=12) as response:url=response.geturl()
    parsed=urllib.parse.urlsplit(url)
    if parsed.hostname not in ('www.bilibili.com','m.bilibili.com','bilibili.com') or not re.match(r'^/video/(BV[0-9A-Za-z]+|av[0-9]+)',parsed.path):
        raise ValueError('Expected one Bilibili video, not a profile or playlist')
    with tempfile.TemporaryDirectory(prefix='bili-read-') as temporary:
        opts={'skip_download':True,'noplaylist':True,'quiet':True,'no_warnings':True,'logger':Quiet(),
              'cachedir':False,'socket_timeout':12,'retries':0,'extractor_retries':0}
        cookie=os.environ.get('BILI_COOKIE_FILE')
        if cookie and Path(cookie).is_file():
            # yt-dlp can update cookie jars; use a private copy and preserve the installed source.
            copy=Path(temporary)/'cookies.txt'
            shutil.copyfile(cookie,copy);os.chmod(copy,0o600)
            opts['cookiefile']=str(copy)
        with YoutubeDL(opts) as ydl:
            info=ydl.extract_info(url,download=False)
            if not isinstance(info,dict):raise ValueError('No video metadata')
            result={k:info.get(k) for k in ('id','title','uploader','duration','description','upload_date','webpage_url')}
            tracks={**(info.get('automatic_captions') or {}),**(info.get('subtitles') or {})}
            language=None;transcript=''
            for lang in sorted(tracks,key=lambda value:(not ('zh' in value or 'cn' in value),'en' not in value,value)):
                variants=tracks[lang]
                if not isinstance(variants,list):continue
                for variant in sorted(variants,key=lambda item:item.get('ext')!='json'):
                    address=variant.get('url')
                    if not address:continue
                    track_url=urllib.parse.urlsplit(address)
                    host=(track_url.hostname or '').lower()
                    if track_url.scheme not in ('https','http') or not (host.endswith('.hdslb.com') or host.endswith('.bilibili.com')):continue
                    with ydl.urlopen(address) as response:raw=response.read(2_000_001)
                    if len(raw)>2_000_000:continue
                    text=raw.decode('utf-8-sig',errors='replace')
                    try:
                        body=json.loads(text).get('body',[])
                        transcript='\n'.join('['+str(row.get('from','?'))+'s–'+str(row.get('to','?'))+'s] '+str(row.get('content','')) for row in body if isinstance(row,dict))
                    except (ValueError,AttributeError):
                        lines=[html.unescape(re.sub(r'<[^>]+>','',line)).strip() for line in text.splitlines()]
                        transcript='\n'.join(line for line in lines if line and '-->' not in line and not line.isdigit() and not line.startswith(('WEBVTT','Kind:','Language:')))
                    if transcript:language=lang;break
                if transcript:break
            offset=max(0,args.offset)
            result.update(transcript_source='Bilibili subtitles' if transcript else None,
                          language=language,transcript=transcript[offset:offset+16000],
                          transcript_length=len(transcript),offset=offset,
                          next_offset=offset+16000 if len(transcript)>offset+16000 else None,
                          content_available=bool(transcript),
                          note=None if transcript else '没有取得可用字幕；这里只读到了视频信息，没有看过视频或转写音频')
            print(json.dumps(result,ensure_ascii=False))

if __name__=='__main__':
    try:main()
    except Exception as error:
        print('Bilibili read failed: '+type(error).__name__,file=sys.stderr)
        raise SystemExit(1)
