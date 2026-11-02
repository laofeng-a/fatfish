#!/usr/bin/env python3
"""One request per process; local media and GPU work, no network listener.

Only url/mode are model inputs. Runtime paths are operator-controlled arguments.
"""
import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import sys

from douyin_reader import LocalVideoWorker, MAX_TEXT, VideoReadError


def parser():
    home = Path.home()
    runtime = home/'.cache'/'codex-runtimes'/'codex-primary-runtime'/'dependencies'/'node'
    args = argparse.ArgumentParser(description=__doc__)
    args.add_argument('--video2text-root', default=str(home/'video2text'))
    args.add_argument('--cookie-file')
    args.add_argument('--output-directory')
    args.add_argument('--browser-node', default=str(runtime/'bin'/'node.exe'))
    args.add_argument('--playwright-module', default=str(runtime/'node_modules'/'playwright'))
    args.add_argument('--check', action='store_true')
    return args


def main(argv=None):
    args = parser().parse_args(argv)
    worker = LocalVideoWorker(args.video2text_root, args.cookie_file,
        browser_node=args.browser_node, playwright_module=args.playwright_module,
        output_directory=args.output_directory)
    if args.check:
        return {'state': 'ok', 'dependencies': worker.dependencies()}
    # A share text can contain 16k Chinese characters; bound bytes separately.
    payload = sys.stdin.buffer.read(MAX_TEXT*4+1)
    if len(payload)>MAX_TEXT*4:
        raise VideoReadError('视频请求超过输入上限', 'invalid_request')
    try:
        value = json.loads(payload.decode('utf-8-sig'))
    except (ValueError, UnicodeError):
        raise VideoReadError('视频请求需要单条 UTF-8 JSON', 'invalid_request') from None
    # Third-party progress cannot contaminate the transport or leak raw errors.
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return worker.read(value)


if __name__=='__main__':
    try:
        result = main()
    except VideoReadError as error:
        result = {'state': 'failed', 'reason': error.code, 'note': str(error)}
    except Exception:
        result = {'state': 'failed', 'reason': 'video_worker_failed',
            'note': '本机视频读取器未完成，尚未取得可验证的视频内容'}
    # UTF-8 is explicit even when PowerShell or an SSH process redirects stdout.
    sys.stdout.buffer.write((json.dumps(result, ensure_ascii=False, separators=(',',':'))+'\n').encode('utf-8'))
