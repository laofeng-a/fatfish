"""One-shot bounded image normalization. No models, browser or resident cache."""
import argparse
import base64
import io
import json
import sys
import os
import urllib.parse
import urllib.request
from pathlib import Path
from PIL import Image,ImageOps,ImageDraw

Image.MAX_IMAGE_PIXELS=6_000_000

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args):return None

def main():
    lock_path=Path('/var/lib/picoclaw-readers/media.lock')
    lock=None
    if os.name=='posix' and lock_path.exists():
        import fcntl
        lock=lock_path.open('r+');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    parser=argparse.ArgumentParser();parser.add_argument('--url');parser.add_argument('--file');parser.add_argument('--output');parser.add_argument('--static-only',action='store_true')
    args=parser.parse_args()
    if args.static_only:Image.MAX_IMAGE_PIXELS=2_000_000
    if bool(args.url)==bool(args.file):raise ValueError('Choose one image source')
    if args.url:
        value=urllib.parse.urlsplit(args.url)
        host=(value.hostname or '').lower()
        if args.static_only and host!='pbs.twimg.com':raise ValueError('Static imports only use X photo hosts')
        if value.scheme!='https' or value.username or value.password or value.port not in (None,443) or not (host=='pbs.twimg.com' or host.endswith('.hdslb.com')):
            raise ValueError('Only configured public media hosts are allowed')
        if host=='pbs.twimg.com':
            query=urllib.parse.parse_qs(value.query);query['name']=['medium']
            args.url=urllib.parse.urlunsplit(value._replace(query=urllib.parse.urlencode(query,doseq=True)))
        request=urllib.request.Request(args.url,headers={'User-Agent':'Mozilla/5.0','Referer':'https://www.bilibili.com/' if host.endswith('.hdslb.com') else 'https://x.com/'})
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
        with opener.open(request,timeout=15) as response:raw=response.read(2*1024*1024+1)
    else:
        path=Path(args.file)
        if not path.is_file() or path.stat().st_size>2*1024*1024:raise ValueError('Image is too large')
        raw=path.read_bytes()
    if len(raw)>2*1024*1024:raise ValueError('Image is too large')
    with Image.open(io.BytesIO(raw)) as image:
        if image.width*image.height>(2_000_000 if args.static_only else 6_000_000):raise ValueError('Image pixel count is too large')
        if args.static_only and (image.format not in ('JPEG','PNG','WEBP') or getattr(image,'is_animated',False)):raise ValueError('Only still photos may be imported')
        count=1 if args.static_only else getattr(image,'n_frames',1)
        if args.static_only and count!=1:raise ValueError('Only still images may be imported')
        if count>300:raise ValueError('Animation has too many frames')
        if count>1 and image.width*image.height>2_000_000:raise ValueError('Animation frames are too large')
        if count>1:
            indexes=sorted(set((0,count//2,count-1)))
            frames=[]
            for index in indexes:
                image.seek(index);frame=image.convert('RGBA');frame.thumbnail((960,720))
                white=Image.new('RGB',frame.size,'white');white.paste(frame,mask=frame.getchannel('A'));frames.append((index,white))
            result=Image.new('RGB',(max(f.width for _,f in frames),sum(f.height+24 for _,f in frames)),'white')
            y=0;draw=ImageDraw.Draw(result)
            for index,frame in frames:
                draw.text((4,y+4),'sampled frame '+str(index+1)+' / '+str(count),fill='black');result.paste(frame,(0,y+24));y+=frame.height+24
            note='Only '+str(len(frames))+' sampled frames from '+str(count)+' frames; timing and other frames were not read.'
        else:
            side=1280 if args.static_only else 2000
            image.draft('RGB',(side,side));image.thumbnail((side,side))
            frame=ImageOps.exif_transpose(image).convert('RGBA')
            result=Image.new('RGB',frame.size,'white');result.paste(frame,mask=frame.getchannel('A'));note='One normalized still image.'
        output=io.BytesIO();result.save(output,'JPEG',quality=86,optimize=False)
        if output.tell()>800_000:
            result.thumbnail((1500,1500));output=io.BytesIO();result.save(output,'JPEG',quality=75,optimize=False)
        data=output.getvalue()
        if len(data)>1_000_000:raise ValueError('Normalized image is too large')
        if args.output:
            Path(args.output).write_bytes(data);payload={'path':args.output,'mime_type':'image/jpeg','note':note}
        else:payload={'mime_type':'image/jpeg','base64':base64.b64encode(data).decode(),'note':note}
        print(json.dumps(payload))

if __name__=='__main__':
    try:main()
    except Exception as error:
        print('Image normalization failed: '+type(error).__name__,file=sys.stderr);raise SystemExit(1)
