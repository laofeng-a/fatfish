"""Metadata and existing subtitle tracks, using only the Python standard library."""
import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
from bilibili_client import BiliClient,NoRedirect

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--url',required=True);parser.add_argument('--offset',type=int,default=0)
    args=parser.parse_args();client=BiliClient(os.environ['BILI_COOKIE_FILE'])
    info=client.info(args.url);part=int(urllib.parse.parse_qs(urllib.parse.urlsplit(args.url).query).get('p',['1'])[0])
    selected=next((p for p in info['parts'] if p['page']==part),None)
    if not selected:raise ValueError('The requested part does not exist')
    transcript='';language=None;failure=None
    try:
        player=client._request('/x/player/wbi/v2',{'aid':info['aid'],'cid':selected['cid']},signed=True)
        tracks=player.get('subtitle',{}).get('subtitles') or []
        tracks=sorted(tracks,key=lambda row:(not row.get('lan','').startswith('zh'),row.get('lan','')))
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
        for track in tracks[:6]:
            address=track.get('subtitle_url','')
            if address.startswith('//'):address='https:'+address
            parsed=urllib.parse.urlsplit(address);host=parsed.hostname or ''
            if parsed.scheme!='https' or parsed.username or parsed.password or not (host.endswith('.hdslb.com') or host.endswith('.bilibili.com')):continue
            with opener.open(urllib.request.Request(address,headers={'User-Agent':'Mozilla/5.0'}),timeout=12) as response:raw=response.read(2_000_001)
            if len(raw)>2_000_000:continue
            body=json.loads(raw).get('body',[])
            transcript='\n'.join('['+str(r.get('from','?'))+'s–'+str(r.get('to','?'))+'s] '+str(r.get('content','')) for r in body if isinstance(r,dict))
            if transcript:language=track.get('lan');break
    except Exception as error:failure=type(error).__name__
    offset=max(0,args.offset);chunk=12000
    info.update(url=args.url,requested_time_seconds=urllib.parse.parse_qs(urllib.parse.urlsplit(args.url).query).get('t',[None])[0],part=part,selected_part=selected,language=language,transcript=transcript[offset:offset+chunk],
        transcript_source='Bilibili subtitle track' if transcript else None,transcript_length=len(transcript),offset=offset,
        next_offset=offset+chunk if len(transcript)>offset+chunk else None,content_available=bool(transcript),
        note=None if transcript else '没有取得可用字幕；只读到了视频信息，没有观看画面或转写音频',subtitle_read_error=failure)
    print(json.dumps(info,ensure_ascii=False))

if __name__=='__main__':
    try:main()
    except Exception as error:
        print('Bilibili read failed: '+type(error).__name__,file=sys.stderr);raise SystemExit(1)
