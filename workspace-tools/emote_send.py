#!/usr/bin/env python3
"""Pick, send and record once: emote_send.py --pick --tag TAG [--kind sticker]."""
import argparse
import json
import mimetypes
import os
from pathlib import Path
import sys
import urllib.error
import urllib.request
import uuid

import emote_pick

CONFIG = Path('/var/lib/picoclaw/.picoclaw/config.json')


def default_chat():
    if os.environ.get('FATFISH_OWNER_CHAT'):
        return os.environ['FATFISH_OWNER_CHAT']
    return str(json.loads(Path('/etc/picoclaw-memory/runtime.json').read_text())['owner_id'])


def send(path, chat):
    formats = {'.webm': ('sendSticker', 'sticker'), '.jpg': ('sendPhoto', 'photo'),
               '.jpeg': ('sendPhoto', 'photo'), '.png': ('sendPhoto', 'photo'),
               '.gif': ('sendAnimation', 'animation'), '.mp4': ('sendAnimation', 'animation')}
    if path.suffix.lower() not in formats:
        raise ValueError('不支持的表情格式 ' + path.suffix)
    method, field = formats[path.suffix.lower()]
    config = json.loads(CONFIG.read_text())
    token = config['channel_list']['telegram']['settings']['token']
    boundary = 'fatfish-' + uuid.uuid4().hex
    mime = mimetypes.guess_type(path.name)[0] or 'application/octet-stream'
    body = (f'--{boundary}\r\nContent-Disposition: form-data; name="chat_id"\r\n\r\n{chat}\r\n'
            f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; filename="{path.name}"\r\n'
            f'Content-Type: {mime}\r\n\r\n').encode() + path.read_bytes() + f'\r\n--{boundary}--\r\n'.encode()
    request = urllib.request.Request('https://api.telegram.org/bot' + token + '/' + method,
                                     data=body, headers={'Content-Type': 'multipart/form-data; boundary=' + boundary})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.load(response)
    except urllib.error.HTTPError as error:
        try:
            detail = json.loads(error.read()).get('description', 'Bot API error')
        except (ValueError, OSError):
            detail = 'Bot API error'
        raise ValueError(f'发送失败（HTTP {error.code}）：{detail}') from None
    except (urllib.error.URLError, TimeoutError):
        raise ValueError('投递未确认；先核对记录，避免盲目重发') from None
    if not result.get('ok') or not result.get('result', {}).get('message_id'):
        raise ValueError('发送未取得成功回执')
    message = result['result']
    media = message.get(field)
    if isinstance(media, list):
        media = media[-1] if media else {}
    return {'ok': True, 'message_id': message['message_id'], 'chat_id': str(chat),
            'method': method, 'as': method.removeprefix('send'), 'file': str(path),
            'media': {k: media[k] for k in ('is_video', 'is_animated', 'mime_type', 'file_unique_id') if isinstance(media, dict) and k in media}}


def execute(args):
    catalog, state = emote_pick.load_catalog(), emote_pick.load_state()
    reason = None
    if args.pick:
        item, reason = emote_pick.choose(catalog, state, args.query, args.tag, args.kind)
        path = Path(item['path'])
    elif args.target:
        item = next((i for i in catalog if str(i['id']) == args.target), None)
        if item is None:
            path = Path(args.target).resolve()
            item = next((i for i in catalog if Path(i['path']).resolve() == path), None)
        else:
            path = Path(item['path'])
    else:
        raise ValueError('提供编号或路径，或者使用 --pick')
    if not path.is_file():
        raise ValueError('表情文件不存在')
    if item:
        emote_pick.check_allowed(item, catalog, state)
    chat = args.chat or default_chat()
    identifier = str(item['id']) if item else None
    if args.dry_run:
        return {'ok': True, 'dry_run': True, 'id': identifier, 'file': str(path), 'reason': reason, 'recorded': False}
    receipt = send(path, chat)
    receipt.update(id=identifier, reason=reason, recorded=False)
    if identifier:
        try:
            emote_pick.record_sent(identifier, chat, receipt['message_id'])
            receipt['recorded'] = True
        except (ValueError, OSError) as error:
            receipt['record_error'] = str(error)
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('target', nargs='?')
    parser.add_argument('--chat')
    parser.add_argument('--pick', action='store_true')
    parser.add_argument('--tag')
    parser.add_argument('--query')
    parser.add_argument('--kind')
    parser.add_argument('--dry-run', action='store_true')
    result = execute(parser.parse_args())
    print(json.dumps(result, ensure_ascii=False))
    return 1 if result.get('record_error') else 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (ValueError, OSError) as error:
        print(json.dumps({'ok': False, 'error': str(error)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(2)
