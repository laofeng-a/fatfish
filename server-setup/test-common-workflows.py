"""Behavior checks for source-preserving, read-only workflow bundles."""
import datetime as dt
import importlib.util
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock

from common_workflows import NAMES, UTC8
from notion_todos import TodoSnapshot

spec = importlib.util.spec_from_file_location('workflow_base', Path(__file__).with_name('test-fatfish.py'))
base = importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)

PAGE = '11111111-1111-1111-1111-111111111111'
PARENT = '22222222-2222-2222-2222-222222222222'
CHILD = '33333333-3333-3333-3333-333333333333'
OTHER = '44444444-4444-4444-4444-444444444444'


def checkbox(identifier, text, checked=False, children=False):
    return {'id': identifier, 'type': 'to_do', 'has_children': children,
            'to_do': {'checked': checked, 'rich_text': [{'plain_text': text}]}}


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = self.root / 'config.json'
        self.path = self.root / 'snapshot.json'
        self.config.write_text(json.dumps({'page_id': PAGE, 'snapshot_file': str(self.path)}))
        self.reader = Mock()
        self.snapshot = TodoSnapshot(self.config, self.reader)

    def seed(self):
        self.reader.request.side_effect = [
            {'last_edited_time': '2026-11-04T01:00:00Z'},
            {'results': [checkbox(PARENT, '已完成的父任务', True, True)], 'has_more': True, 'next_cursor': 'next'},
            {'results': [checkbox(CHILD, '仍未完成的子任务')], 'has_more': False},
            {'results': [checkbox(OTHER, '下一页待办')], 'has_more': False},
        ]

    def test_pagination_and_unchecked_child_of_checked_parent(self):
        self.seed()
        value = self.snapshot.refresh()
        self.assertEqual([item['id'] for item in value['items']], [CHILD, OTHER])
        self.assertEqual(value['items'][0]['parents'], ['已完成的父任务'])
        self.assertEqual(value['pending_count'], 2)
        self.assertFalse(value['stale'])
        self.assertEqual(self.reader.request.call_count, 4)
        self.assertTrue(all(call.args[0] == 'GET' for call in self.reader.request.call_args_list))

    def test_failed_refresh_preserves_last_complete_list_and_time(self):
        self.seed()
        before = self.snapshot.refresh()
        self.reader.request.side_effect = ValueError('Notion暂时不可用')
        with self.assertRaises(ValueError):
            self.snapshot.refresh()
        after = self.snapshot.read()
        self.assertEqual(after['items'], before['items'])
        self.assertEqual(after['synced_at'], before['synced_at'])
        self.assertEqual(after['status'], 'sync_failed')
        self.assertTrue(after['stale'])

    def test_missing_snapshot_is_unknown_and_successful_empty_is_known(self):
        self.assertEqual(self.snapshot.read()['status'], 'unavailable')
        self.reader.request.side_effect = [{}, {'results': [], 'has_more': False}]
        value = self.snapshot.refresh()
        self.assertEqual(value['status'], 'ok')
        self.assertEqual(value['pending_count'], 0)

    def test_incomplete_page_does_not_publish_partial_list(self):
        self.seed()
        before = self.snapshot.refresh()
        self.reader.request.side_effect = [{}, {'results': [checkbox(OTHER, 'partial')], 'has_more': True}]
        with self.assertRaises(ValueError):
            self.snapshot.refresh()
        self.assertEqual(self.snapshot.read()['items'], before['items'])


class WorkflowTests(unittest.TestCase):
    setUp = base.PolicyTests.setUp

    def call(self, name, **args):
        return self.r.call(name, dict(args, _context=self.context))

    def test_tools_registered_and_personal_tools_private(self):
        from runtime_policy import TOOLS
        self.assertTrue(NAMES.issubset({tool['name'] for tool in TOOLS}))
        for name in NAMES - {'chat_recall'}:
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.r.workflows.call(name, {}, dict(self.context, chat_id='-99'), '-99')

    def test_chat_recall_expands_originals_with_neighbors_once(self):
        first = self.r.event('123', 'user', '前一句话')
        match = self.r.event('123', 'assistant', '关键词' + '原文' * 350)
        last = self.r.event('123', 'user', '后一句话')
        self.r.event('-99', 'user', '关键词别的群')
        value = self.call('chat_recall', query='关键词')
        self.assertEqual(value['matched_ids'], [match])
        self.assertEqual([row['source_id'] for row in value['records']], [first, match, last])
        self.assertEqual(value['records'][1]['content'], '关键词' + '原文' * 350)
        self.assertFalse(value['records'][1]['truncated'])

    def test_chat_recall_large_matches_kept_and_truncation_explicit(self):
        ids = [self.r.event('123', 'user', '关键词' + '长' * 8000) for _ in range(8)]
        value = self.call('chat_recall', query='关键词', limit=8, around=0)
        self.assertEqual([row['id'] for row in value['records']], ids)
        self.assertTrue(all(row['truncated'] for row in value['records']))

    def test_diary_prepare_day_scope_revision_and_no_write(self):
        day = dt.datetime.now(UTC8).date()
        start = dt.datetime.combine(day, dt.time(), UTC8).timestamp()
        wanted = self.r.event('123', 'user', '当天真实经历')
        outside = self.r.event('123', 'user', '昨天的记录')
        self.r.event('-99', 'user', '别的群记录')
        with self.r.db() as db:
            db.execute('UPDATE events SET created=? WHERE id=?', (start + 60, wanted))
            db.execute('UPDATE events SET created=? WHERE id=?', (start - 1, outside))
        value = self.call('diary_prepare', day=day.isoformat())
        self.assertEqual(value['source_ids'], [wanted])
        self.assertIn('revision', value['existing_note'])
        self.assertFalse(value['existing_note']['exists'])
        self.assertFalse(self.r.notes.path(value['path']).exists())

    def test_diary_prepare_pagination_has_no_gaps_or_duplicates(self):
        for i in range(85):
            self.r.event('123', 'user', str(i))
        first = self.call('diary_prepare')
        second = self.call('diary_prepare', cursor=first['next_cursor'])
        self.assertEqual(len(first['records']) + len(second['records']), 85)
        self.assertFalse(set(first['source_ids']) & set(second['source_ids']))
        self.assertFalse(second['has_more'])

    def test_codex_overview_reads_existing_receipts_without_enqueue(self):
        thread = 'thread_workflow_test'
        self.r.settings['codex_default_thread_id'] = thread
        now = time.time()
        with self.r.db() as db:
            db.execute("INSERT INTO codex_jobs(id,source,fingerprint,action,args,state,created,updated,output) VALUES('read-job','source','fp','read',?,'done',?,?,?)",
                       (json.dumps({'thread_id': thread}), now, now, json.dumps({'thread_id': thread, 'execution_state': 'running', 'text': '正在验证'})))
            db.execute("INSERT INTO codex_usage_cache VALUES(1,'2026-11-04T00:00:00Z',?,?)", (json.dumps({'remaining_percent': 80}), now))
        self.r.codex.call = Mock(side_effect=AssertionError('read-only overview enqueued work'))
        value = self.call('codex_overview')
        self.assertEqual(value['latest_progress']['state'], 'done')
        self.assertEqual(value['latest_progress']['result']['execution_state'], 'running')
        self.assertEqual(value['quota']['snapshot']['remaining_percent'], 80)
        self.r.codex.call.assert_not_called()

    def test_codex_refresh_uses_one_existing_read(self):
        self.r.settings['codex_default_thread_id'] = 'thread_workflow_test'
        self.r.codex.call = Mock(return_value={'state': 'queued', 'job_id': 'existing-read'})
        result = self.call('codex_overview', refresh=True)
        self.assertEqual(result['refresh_request']['job_id'], 'existing-read')
        self.r.codex.call.assert_called_once()
        self.assertEqual(self.r.codex.call.call_args.args[0]['action'], 'read')


if __name__ == '__main__':
    unittest.main()
