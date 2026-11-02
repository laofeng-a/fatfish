import concurrent.futures
import importlib.util
import json
import os
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import Mock
from tool_efficiency import ToolEfficiency

spec=importlib.util.spec_from_file_location('efficiency_base',Path(__file__).with_name('test-fatfish.py'))
base=importlib.util.module_from_spec(spec);spec.loader.exec_module(base)


class EfficiencyTests(unittest.TestCase):
    setUp=base.PolicyTests.setUp

    def engine(self,**kwargs):
        value=ToolEfficiency(self.r,**kwargs);self.addCleanup(value.close);return value

    def test_batch_really_overlaps_and_preserves_result_order(self):
        engine=self.engine();active=0;peak=0;lock=threading.Lock()
        def execute(name,args):
            nonlocal active,peak
            with lock:active+=1;peak=max(peak,active)
            time.sleep(.06)
            with lock:active-=1
            return {'name':name,'context':args['_context']}
        names=['emote_find','telegram_chats','history_find','chat_status']
        value=engine.batch({'calls':[{'name':name} for name in names],'_context':self.context},execute)
        self.assertEqual(peak,4)
        self.assertEqual([x['name'] for x in value['results']],names)
        self.assertEqual(value['results'][0]['result']['context'],self.context)

    def test_mutations_execute_in_input_order(self):
        engine=self.engine();events=[]
        def execute(name,args):
            events.append(('start',args['position']))
            time.sleep(.04 if args['position']==1 else .001)
            events.append(('end',args['position']))
            return {'position':args['position']}
        calls=[{'name':name,'arguments':{'position':i}} for i,name in
               enumerate(('send_message','emote_send','memory_note_write'),1)]
        result=engine.batch({'calls':calls},execute)
        self.assertEqual(events,[('start',1),('end',1),('start',2),('end',2),('start',3),('end',3)])
        self.assertFalse(result['parallel'])

    def test_write_is_a_barrier_between_read_groups(self):
        engine=self.engine();events=[];guard=threading.Lock()
        def execute(name,args):
            with guard:events.append(('start',args['position']))
            time.sleep(.03)
            with guard:events.append(('end',args['position']))
            return {}
        calls=[{'name':name,'arguments':dict(action=action,position=i)} for i,(name,action) in
               enumerate((('notion','read'),('codex','status'),('notion','append'),('notion','read')))]
        value=engine.batch({'calls':calls},execute)
        self.assertLess(events.index(('end',0)),events.index(('start',2)))
        self.assertLess(events.index(('end',1)),events.index(('start',2)))
        self.assertLess(events.index(('end',2)),events.index(('start',3)))
        self.assertTrue(value['parallel'])

    def test_unknown_operations_still_execute_in_sequence(self):
        engine=self.engine();seen=[]
        def execute(name,args):seen.append(name);return {'ok':True}
        result=engine.batch({'calls':[{'name':'future_extension'},{'name':'mcp_reader_send_message'}]},execute)
        self.assertEqual(seen,['future_extension','send_message'])
        self.assertFalse(any(x['is_error'] for x in result['results']))

    def test_invalid_action_is_only_one_failed_item(self):
        engine=self.engine()
        def execute(name,args):
            if name=='notion':raise ValueError('action needs a string')
            return {'ok':True}
        result=engine.batch({'calls':[{'name':'notion','arguments':{'action':[]}},
                                      {'name':'emote_find'}]},execute)
        self.assertTrue(result['results'][0]['is_error'])
        self.assertFalse(result['results'][1]['is_error'])

    def test_memory_atomic_replacement_invalidates_even_with_same_mtime(self):
        engine=self.engine()
        path=self.r.notes.path('USER.md');path.write_text('old',encoding='utf-8')
        backend=lambda args:{'content':path.read_text(encoding='utf-8')}
        args={'path':'USER.md','_context':self.context}
        self.assertEqual(engine.invoke('memory_note_read',args,backend)['content'],'old')
        stamp=path.stat().st_mtime_ns
        replacement=path.with_suffix('.tmp');replacement.write_text('new',encoding='utf-8')
        os.utime(replacement,ns=(stamp,stamp));os.replace(replacement,path)
        result=engine.invoke('memory_note_read',args,backend)
        self.assertEqual(result['content'],'new')
        self.assertFalse(result['read_cache']['hit'])

    def test_two_minute_alarm_and_description_agree(self):
        from runtime_policy import TOOLS
        from silent_alarms import MIN_INTERVAL_SECONDS
        self.assertEqual(MIN_INTERVAL_SECONDS,120)
        result=self.r.silent_alarms.call({'action':'create','task':'continue when relevant',
                                        'interval_seconds':120},self.context,self.r.owner)
        self.assertEqual(result['interval_seconds'],120)
        with self.assertRaises(ValueError):
            self.r.silent_alarms.call({'action':'create','task':'bad interval','interval_seconds':119},
                                     self.context,self.r.owner)
        description=next(x['description'] for x in TOOLS if x['name']=='silent_alarm')
        self.assertIn('120',description)

    def test_repeat_reads_use_cache_and_refresh_reexecutes(self):
        engine=self.engine();execute=Mock(return_value={'total':0,'emotes':[]})
        args={'query':'笑','_context':self.context}
        first=engine.invoke('emote_find',args,execute);second=engine.invoke('emote_find',args,execute)
        self.assertFalse(first['read_cache']['hit']);self.assertTrue(second['read_cache']['hit'])
        self.assertEqual(execute.call_count,1)
        third=engine.invoke('emote_find',dict(args,_context=dict(self.context,message_id='next-message')),execute)
        self.assertTrue(third['read_cache']['hit']);self.assertEqual(execute.call_count,1)
        engine.invoke('emote_find',dict(args,refresh=True),execute)
        self.assertEqual(execute.call_count,2);self.assertNotIn('refresh',execute.call_args.args[0])

    def test_duplicate_parallel_reads_share_one_backend_call(self):
        engine=self.engine();count=0;lock=threading.Lock()
        def backend(args):
            nonlocal count
            with lock:count+=1
            time.sleep(.08);return {'total':0,'emotes':[]}
        def invoke(name,args):return engine.invoke(name,args,backend)
        result=engine.batch({'calls':[{'name':'emote_find'} for _ in range(4)],'_context':self.context},invoke)
        self.assertEqual(count,1);self.assertFalse(any(x['is_error'] for x in result['results']))

    def test_chat_change_invalidates_history_but_results_are_not_mutable(self):
        engine=self.engine();execute=Mock(return_value={'messages':[{'text':'original'}]})
        args={'query':'x','_context':self.context}
        first=engine.invoke('history_find',args,execute);first['messages'][0]['text']='changed'
        second=engine.invoke('history_find',args,execute)
        self.assertEqual(second['messages'][0]['text'],'original')
        self.r.event('123','user','new message','2')
        engine.invoke('history_find',args,execute);self.assertEqual(execute.call_count,2)

    def test_failures_are_not_cached_and_other_batch_results_survive(self):
        engine=self.engine();bad=Mock(side_effect=ValueError('test failure'))
        for _ in range(2):
            with self.assertRaises(ValueError):engine.invoke('emote_find',{'_context':self.context},bad)
        self.assertEqual(bad.call_count,2)
        def execute(name,args):
            if name=='bad':raise ValueError('bad')
            return {'ok':True}
        result=engine.batch({'calls':[{'name':'bad'},{'name':'good'},{'name':'batch'}]},execute)
        self.assertTrue(result['results'][0]['is_error']);self.assertFalse(result['results'][1]['is_error'])
        self.assertTrue(result['results'][2]['is_error'])

    def test_scope_and_byte_limits(self):
        engine=self.engine(max_bytes=150,max_entries=2);execute=Mock(return_value={'text':'x'*300})
        engine.invoke('emote_find',{'_context':self.context},execute)
        engine.invoke('emote_find',{'_context':self.context},execute)
        self.assertEqual(execute.call_count,2);self.assertLessEqual(engine.bytes,150)
        small=Mock(return_value={'ok':True})
        engine.invoke('telegram_chats',{'_context':self.context},small)
        engine.invoke('telegram_chats',{'_context':dict(self.context,chat_id='456')},small)
        self.assertEqual(small.call_count,2)

    def test_notion_writes_are_never_cached(self):
        engine=self.engine();backend=Mock(return_value={'status':'saved'})
        for _ in range(2):engine.invoke('notion',{'action':'create','title':'x','_context':self.context},backend)
        self.assertEqual(backend.call_count,2)

    def test_notion_write_invalidates_previous_read(self):
        engine=self.engine();value={'text':'old'};reads=[]
        def backend(args):
            if args['action']=='append':value['text']='new'
            else:reads.append(value['text'])
            return dict(value)
        args={'action':'read','page_id':'page','_context':self.context}
        self.assertEqual(engine.invoke('notion',args,backend)['text'],'old')
        engine.invoke('notion',{'action':'append','_context':self.context},backend)
        self.assertEqual(engine.invoke('notion',args,backend)['text'],'new')
        self.assertEqual(reads,['old','new'])

    def test_recent_media_keeps_exact_id_and_message_receipt(self):
        self.r.catalog=[{'id':'whale-235','name':'举手耶','kind':'sticker','path':'/tmp/test.webm','mime':'video/webm','tags':[]}]
        self.r.telegram=Mock(return_value={'message_id':1146,'sticker':{'file_id':'cached'}})
        self.r.call('emote_send',{'id':'whale-235','_context':self.context})
        value=self.r.call('emote_history',{'_context':self.context})
        row=value['media'][0]
        self.assertEqual(row['emote_id'],'whale-235');self.assertEqual(row['message_id'],'1146')
        self.assertEqual(row['id_source'],'delivery_record')
        with self.r.db() as db:db.execute("UPDATE send_actions SET status='unknown'")
        self.assertEqual(self.r.call('emote_history',{'_context':self.context})['media'],[])

    def test_legacy_media_resolves_only_unambiguous_names(self):
        self.r.catalog=[{'id':'one','name':'old','kind':'sticker','file_id':'cached','tags':[]}]
        self.r.telegram=Mock(return_value={'message_id':22})
        self.r.call('emote_send',{'id':'one','_context':self.context})
        with self.r.db() as db:
            row=db.execute('SELECT rowid,content FROM send_actions').fetchone()
            content=json.loads(row['content']);content.pop('emote_id')
            db.execute('UPDATE send_actions SET content=? WHERE rowid=?',(json.dumps(content),row['rowid']))
        self.assertEqual(self.r.call('emote_history',{'_context':self.context})['media'][0]['emote_id'],'one')
        self.r.catalog.append({'id':'two','name':'old','kind':'sticker','tags':[]})
        self.assertIsNone(self.r.call('emote_history',{'_context':self.context})['media'][0]['emote_id'])

    def test_pending_codex_query_reused_across_messages(self):
        with self.r.db() as db:
            self.r._set(db,'codex_worker_seen',time.time());self.r._set(db,'codex_worker_protocol','5')
        self.r.settings['codex_default_thread_id']='00000000-0000-4000-8000-000000000001'
        first=self.r.call('codex',{'action':'read','_context':self.context})
        second=self.r.call('codex',{'action':'read','_context':dict(self.context,message_id='2')})
        self.assertEqual(first['job_id'],second['job_id']);self.assertTrue(second['reused_pending_query'])
        with self.r.db() as db:self.assertEqual(db.execute("SELECT COUNT(*) FROM codex_jobs WHERE action='read'").fetchone()[0],1)


if __name__=='__main__':unittest.main()
