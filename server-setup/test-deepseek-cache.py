"""Preserve fresh time and conversation structure while reusing stable prefixes."""
import copy
from contextlib import closing
import datetime as dt
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('cache_gateway',Path(__file__).with_name('budget-gateway.py'))
g=importlib.util.module_from_spec(spec);spec.loader.exec_module(g)

class CacheTests(unittest.TestCase):
    def request(self):
        return {'model':'deepseek-flash','messages':[
            {'role':'system','content':'stable persona\n\n## Current Time\n2026-10-29 22:00 (Thursday)\n\n## Runtime\nlinux amd64\n\n## Current Session\nChat ID: 123'},
            {'role':'user','content':'hello'},
            {'role':'assistant','content':'hi'},
            {'role':'user','content':'and now?'}]}

    def test_only_clock_tail_changes_between_requests(self):
        data=self.request();original=copy.deepcopy(data)
        one=dt.datetime(2026,10,29,22,1,1,tzinfo=g.UTC8);two=dt.datetime(2026,10,29,22,2,2,tzinfo=g.UTC8)
        with patch.object(g.dt,'datetime') as clock:
            clock.now.return_value=one
            first=g.refresh_runtime_clock(data,tail_context=True)
            clock.now.return_value=two
            second=g.refresh_runtime_clock(data,tail_context=True)
        self.assertEqual(first['messages'][:-1],second['messages'][:-1])
        self.assertNotEqual(first['messages'][-1],second['messages'][-1])
        self.assertEqual(first['messages'][0]['role'],'system')
        self.assertNotIn('## Current Time',first['messages'][0]['content'])
        self.assertIn('## Current Session',first['messages'][0]['content'])
        self.assertIn('22:02:02',second['messages'][-1]['content'])
        self.assertEqual(data,original)

    def test_tool_pairs_and_user_images_remain_intact(self):
        data=self.request();data['messages'] += [
            {'role':'assistant','content':'','tool_calls':[{'id':'call1','type':'function','function':{'name':'lookup','arguments':'{}'}}]},
            {'role':'tool','tool_call_id':'call1','content':'lookup result'}]
        data['messages'][1]['content']=[{'type':'text','text':'hello'},{'type':'image_url','image_url':{'url':'data:image/png;base64,AA=='}}]
        result=g.refresh_runtime_clock(data,tail_context=True)
        self.assertEqual(result['messages'][1:-1],data['messages'][1:])
        self.assertEqual(result['messages'][-1]['role'],'system')

    def test_repeated_refresh_has_exactly_one_clock(self):
        once=g.refresh_runtime_clock(self.request(),tail_context=True)
        twice=g.refresh_runtime_clock(once,tail_context=True)
        self.assertEqual(len(once['messages']),len(twice['messages']))
        self.assertEqual(json.dumps(twice).count('[fatfish_runtime_clock]'),1)

    def test_system_text_blocks_and_no_system_request(self):
        data={'messages':[{'role':'system','content':[{'type':'text','text':'keep me'}]}, {'role':'user','content':'hello'}]}
        original=copy.deepcopy(data);result=g.refresh_runtime_clock(data,tail_context=True)
        self.assertEqual(result['messages'][0],data['messages'][0]);self.assertEqual(data,original)
        result=g.refresh_runtime_clock({'messages':[{'role':'user','content':'hi'}]},tail_context=True)
        self.assertEqual([x['role'] for x in result['messages']],['user','system'])

    def test_legacy_clock_behavior_is_preserved(self):
        data=g.refresh_runtime_clock(self.request())
        self.assertIn('[fatfish_runtime_clock]',data['messages'][0]['content'])
        self.assertEqual(len(data['messages']),4)

    def test_metrics_read_provider_hit_fields_without_inventing_missing_values(self):
        with tempfile.TemporaryDirectory() as folder:
            ledger=g.Ledger(Path(folder)/'test.sqlite')
            ledger.cache_usage(1,'deepseek-flash',{'prompt_tokens':100,'prompt_cache_hit_tokens':80},12)
            ledger.cache_usage(2,'deepseek-flash',{'prompt_tokens':100,'prompt_tokens_details':{'cached_tokens':60}},15)
            ledger.cache_usage(3,'deepseek-flash',{'prompt_tokens':100},20)
            ledger.cache_usage(4,'deepseek-flash',{'prompt_tokens':100,'prompt_cache_hit_tokens':200},20)
            with closing(sqlite3.connect(ledger.path)) as db:rows=db.execute('SELECT call_id,prompt_tokens,hit_tokens,miss_tokens FROM cache_usage ORDER BY call_id').fetchall()
            self.assertEqual(rows,[(1,100,80,20),(2,100,60,40)])

if __name__=='__main__':unittest.main(verbosity=2)
