#!/usr/bin/env python3
"""Read-only selection; record only confirmed sends. No repeats before half usage."""
import argparse
import json
import os
from pathlib import Path
import random
import sys
import tempfile
import time

CATALOG = Path(os.environ.get('FATFISH_EMOTE_CATALOG_FILE', '/opt/picoclaw/assets/emotes/catalog.json'))
STATE = Path(os.environ.get('FATFISH_EMOTE_STATE_FILE', '/var/lib/picoclaw/.picoclaw/workspace/state/emote-used.json'))


def load_catalog():
    data = json.loads(CATALOG.read_text(encoding='utf-8'))
    if isinstance(data, dict):
        data = data.get('items', data.get('emotes', data))
    if isinstance(data, dict):
        data = [dict(item, id=key) for key, item in data.items()]
    if not isinstance(data, list) or not data:
        raise ValueError('表情库为空或格式不正确')
    return data


def load_state():
    state = json.loads(STATE.read_text(encoding='utf-8')) if STATE.exists() else {'cycle': 1}
    if not isinstance(state, dict):
        raise ValueError('表情账本格式不正确')
    for name in ('used', 'sent_count', 'receipts', 'last_sent'):
        state.setdefault(name, {})
        if not isinstance(state[name], dict):
            raise ValueError('表情账本字段不正确：' + name)
    return state


def save_state(state):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.emote-used-', dir=STATE.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(state, stream, ensure_ascii=False, indent=1, sort_keys=True)
            stream.write('\n')
        os.replace(temporary, STATE)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def used_ids(catalog, state):
    return {str(item['id']) for item in catalog} & set(state['used'])


def matches(item, query=None, tag=None, kind=None):
    if kind and item.get('kind') != kind:
        return False
    if tag and tag.lower() not in [str(t).lower() for t in item.get('tags', [])]:
        return False
    haystack = ' '.join([str(item['id']), item.get('name', '')] + list(item.get('tags', []))).lower()
    return not query or query.lower() in haystack


def check_allowed(item, catalog, state):
    used = used_ids(catalog, state)
    if str(item['id']) in used and len(used) * 2 < len(catalog):
        raise ValueError('EMOTE-RULE：库未过半，不能重复发送 ' + str(item['id']))


def choose(catalog, state, query=None, tag=None, kind=None):
    pool = [item for item in catalog if matches(item, query, tag, kind)]
    if not pool:
        raise ValueError('没有匹配的表情')
    used = used_ids(catalog, state)
    fresh = [item for item in pool if str(item['id']) not in used]
    if fresh:
        return random.choice(fresh), '优先未发过的表情'
    if len(used) * 2 < len(catalog):
        raise ValueError('EMOTE-RULE：该条件下已无未发过的表情，且库未过半；请换标签或关键词')
    return min(pool, key=lambda item: state['last_sent'].get(str(item['id']), state['used'].get(str(item['id']), ''))), '库已过半，选择最久未发的'


def record_sent(identifier, chat=None, message_id=None):
    state = load_state()
    key = f'{chat}:{message_id}' if message_id is not None else None
    if key and state['receipts'].get(key) == identifier:
        return False
    now = time.strftime('%Y-%m-%dT%H:%M:%S%z')
    state['used'].setdefault(identifier, now)
    state['sent_count'][identifier] = state['sent_count'].get(identifier, 0) + 1
    state['last_sent'][identifier] = now
    if key:
        state['receipts'][key] = identifier
    save_state(state)
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    pick = sub.add_parser('pick')
    pick.add_argument('--query')
    pick.add_argument('--tag')
    pick.add_argument('--kind')
    pick.add_argument('--dry', action='store_true', help='兼容旧调用；挑选始终不记账')
    for name in ('mark', 'sent', 'check'):
        sub.add_parser(name).add_argument('ids', nargs='+')
    sub.add_parser('status')
    args = parser.parse_args()
    catalog, state = load_catalog(), load_state()
    if args.action == 'pick':
        item, reason = choose(catalog, state, args.query, args.tag, args.kind)
        print(json.dumps(dict(item, reason=reason, marked=False,
                              usage_after=len(used_ids(catalog, state))/len(catalog), total=len(catalog)), ensure_ascii=False))
    elif args.action in ('mark', 'sent'):
        known = {str(item['id']) for item in catalog}
        if not set(args.ids) <= known:
            raise ValueError('表情编号不在库中')
        for identifier in args.ids:
            record_sent(identifier)
        print(json.dumps({'marked': args.ids}, ensure_ascii=False))
    elif args.action == 'check':
        for identifier in args.ids:
            count = state['sent_count'].get(identifier, 0)
            print(f'{identifier}\t{"已发" if count else "没发"}\t{count}')
    else:
        used = len(used_ids(catalog, state))
        print(f'库 {len(catalog)} 个，已发过 {used} 个（{used/len(catalog):.1%}）；'
              + ('已过半，允许重样' if used*2 >= len(catalog) else '未过半，不许重样'))
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (ValueError, OSError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(2)
