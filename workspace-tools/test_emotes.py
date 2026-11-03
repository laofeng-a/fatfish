import argparse
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import emote_pick
import emote_send


class EmoteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.catalog, self.state = root/'catalog.json', root/'used.json'
        items = []
        for n in range(6):
            path = root/f'{n}.webm'
            path.write_bytes(b'fixture')
            items.append({'id': str(n), 'path': str(path), 'kind': 'sticker', 'tags': ['special' if n == 0 else 'other']})
        self.catalog.write_text(json.dumps(items))
        self.state.write_text(json.dumps({'cycle': 1, 'used': {'0': 'old'}, 'sent_count': {'0': 1}}))
        for name, value in [('CATALOG', self.catalog), ('STATE', self.state)]:
            setting = patch.object(emote_pick, name, value)
            setting.start()
            self.addCleanup(setting.stop)

    def args(self, **extra):
        return argparse.Namespace(target=None, pick=True, tag=None, query=None, kind=None,
                                  chat='owner', dry_run=False, **extra)

    def test_exhausted_tag_cannot_repeat_before_half(self):
        with self.assertRaisesRegex(ValueError, 'EMOTE-RULE'):
            emote_pick.choose(emote_pick.load_catalog(), emote_pick.load_state(), tag='special')

    def test_pick_is_read_only(self):
        before = self.state.read_bytes()
        item, _ = emote_pick.choose(emote_pick.load_catalog(), emote_pick.load_state())
        self.assertNotEqual(item['id'], '0')
        self.assertEqual(before, self.state.read_bytes())

    def test_send_failure_does_not_mark(self):
        before = self.state.read_bytes()
        with patch.object(emote_send, 'send', side_effect=ValueError('rejected')):
            with self.assertRaises(ValueError):
                emote_send.execute(self.args())
        self.assertEqual(before, self.state.read_bytes())

    def test_pick_send_records_exactly_once(self):
        with patch.object(emote_send, 'send', return_value={'ok': True, 'message_id': 42}) as send:
            result = emote_send.execute(self.args())
        send.assert_called_once()
        state = emote_pick.load_state()
        self.assertTrue(result['recorded'])
        self.assertEqual(state['sent_count'][result['id']], 1)
        emote_pick.record_sent(result['id'], 'owner', 42)
        self.assertEqual(emote_pick.load_state()['sent_count'][result['id']], 1)

    def test_explicit_repeat_is_blocked_before_api(self):
        args = self.args()
        args.pick, args.target = False, '0'
        with patch.object(emote_send, 'send') as send:
            with self.assertRaisesRegex(ValueError, 'EMOTE-RULE'):
                emote_send.execute(args)
        send.assert_not_called()

    def test_half_boundary_and_orphan_ids(self):
        state = emote_pick.load_state()
        state['used']['removed'] = 'old'
        state['used']['1'] = 'old'
        emote_pick.save_state(state)
        with self.assertRaises(ValueError):
            emote_pick.choose(emote_pick.load_catalog(), emote_pick.load_state(), tag='special')
        state['used']['2'] = 'old'
        emote_pick.save_state(state)
        item, _ = emote_pick.choose(emote_pick.load_catalog(), emote_pick.load_state(), tag='special')
        self.assertEqual(item['id'], '0')

    def test_invalid_ledger_is_not_reset(self):
        self.state.write_text('{broken')
        with self.assertRaises(ValueError):
            emote_pick.load_state()
        self.assertEqual(self.state.read_text(), '{broken')


if __name__ == '__main__':
    unittest.main()
