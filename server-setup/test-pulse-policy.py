import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock
from runtime_policy import Runtime


class PulseTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        root=Path(self.tmp.name);(root/'catalog.json').write_text('[]')
        config=root/'settings.json';config.write_text(json.dumps({'owner_id':'123','state_dir':str(root),'catalog_file':str(root/'catalog.json'),
            'pulse_policy':{'enabled':True,'existing_maintenance_ids':[]},'exploration_opportunities':{'enabled':True}}))
        self.r=Runtime(config);self.r.diary.claim=Mock(return_value=None);self.r.exploration.consider=Mock()
        self.ctx={'channel':'telegram','chat_id':'123','message_id':'test','session_key':'fixture'}

    def alarm(self,purpose='chat',**extra):
        args={'action':'create','task':'本次约定','purpose':purpose,'delay_seconds':1,'interval_seconds':600,'_context':self.ctx}
        args.update(extra);return self.r.call('silent_alarm',args)

    def due(self,identifier):
        with self.r.db() as db:db.execute('UPDATE silent_alarms SET due=? WHERE id=?',(time.time()-1,identifier))

    def get(self,identifier):
        with self.r.db() as db:return dict(db.execute('SELECT * FROM silent_alarms WHERE id=?',(identifier,)).fetchone())

    def finish(self,wake,contacted=0,outcome='completed'):
        with self.r.db() as db:db.execute('UPDATE wakes SET contacted=? WHERE id=?',(contacted,wake['wake_id']))
        self.r.action('wake_done',{'wake_id':wake['wake_id'],'outcome':outcome,'summary':'内部结论'})

    def test_three_exact_cadences(self):
        for purpose,interval in (('chat',120),('quota_watch',300),('quota_reset',0)):
            with self.subTest(purpose=purpose):
                value=self.alarm(purpose);self.assertEqual(value['interval_seconds'],interval);self.assertEqual(value['purpose'],purpose)

    def test_other_purposes_not_scheduled(self):
        for purpose in ('reading','maintenance','progress'):
            with self.subTest(purpose=purpose),self.assertRaises(ValueError):self.alarm(purpose)

    def test_reset_needs_actual_time(self):
        with self.assertRaises(ValueError):self.r.call('silent_alarm',{'action':'create','task':'等重置','purpose':'quota_reset','_context':self.ctx})

    def test_repeat_creation_reuses_same_active_pulse(self):
        a=self.alarm();b=self.alarm(task='换个话题',_context=dict(self.ctx,message_id='new'))
        self.assertEqual(a['id'],b['id']);self.assertEqual(self.get(a['id'])['task'],'换个话题')

    def test_no_random_or_todo_model_wakeup(self):
        self.r.todos.due_wake_needed=Mock(return_value=True)
        result=self.r.wake({});self.assertTrue(result['skip'])
        self.r.exploration.consider.assert_not_called();self.r.todos.due_wake_needed.assert_not_called()
        with self.r.db() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM wakes').fetchone()[0],0)

    def test_no_share_ends_chain_after_one_model_round(self):
        alarm=self.alarm();self.due(alarm['id']);wake=self.r.wake({});self.finish(wake)
        self.assertEqual(self.get(alarm['id'])['status'],'cancelled')
        self.assertTrue(self.r.wake({})['skip'])

    def test_no_share_still_ends_when_model_updated_its_own_alarm(self):
        alarm=self.alarm();self.due(alarm['id']);wake=self.r.wake({})
        self.r.call('silent_alarm',{'action':'update','id':alarm['id'],'delay_seconds':120,'_context':self.ctx})
        self.finish(wake);self.assertEqual(self.get(alarm['id'])['status'],'cancelled')

    def test_actual_share_keeps_the_120_second_pulse(self):
        alarm=self.alarm();self.due(alarm['id']);wake=self.r.wake({});self.finish(wake,contacted=1)
        self.assertEqual(self.get(alarm['id'])['status'],'pending');self.assertEqual(self.get(alarm['id'])['interval_seconds'],120)

    def test_two_unanswered_rounds_stop_before_third_model_call(self):
        alarm=self.alarm();self.due(alarm['id'])
        for _ in range(2):
            wake=self.r.wake({});self.assertFalse(wake.get('skip'));self.finish(wake,contacted=1);self.due(alarm['id'])
        self.assertTrue(self.r.wake({})['skip']);self.assertEqual(self.get(alarm['id'])['status'],'cancelled')

    def test_new_human_reply_resets_unanswered_rounds(self):
        alarm=self.alarm()
        for i in range(3):
            self.r.event('123','user','接着聊',str(i));self.due(alarm['id']);wake=self.r.wake({})
            self.assertFalse(wake.get('skip'));self.finish(wake,contacted=1)

    def test_recent_messages_supplied_without_history_tool_or_external_call(self):
        self.r.event('123','user','这里是刚说的话','7');self.r.telegram=Mock()
        alarm=self.alarm();self.due(alarm['id']);wake=self.r.wake({})
        self.assertIn('这里是刚说的话',wake['context']);self.r.telegram.assert_not_called()

    def test_quota_watch_without_running_task_stops_before_model(self):
        alarm=self.alarm('quota_watch');self.due(alarm['id'])
        self.assertTrue(self.r.wake({})['skip']);self.assertEqual(self.get(alarm['id'])['status'],'cancelled')

    def test_running_quota_watch_has_cached_usage_and_task_without_external_query(self):
        with self.r.db() as db:
            db.execute("INSERT INTO codex_chains(id,thread_id,initial_job,state,created,updated) VALUES('c','thread','job','running',?,?)",(time.time(),time.time()))
            db.execute("INSERT INTO codex_usage_cache VALUES(1,'now',?,?)",(json.dumps({'ordinary_usage_allowed':True}),time.time()))
        alarm=self.alarm('quota_watch');self.due(alarm['id']);wake=self.r.wake({})
        self.assertIn('last_usage',wake['context']);self.assertIn('thread',wake['context'])
        with self.r.db() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM codex_jobs').fetchone()[0],0)

    def test_reset_is_one_shot_even_on_error(self):
        alarm=self.alarm('quota_reset');self.due(alarm['id']);wake=self.r.wake({});self.finish(wake,outcome='error')
        self.assertEqual(self.get(alarm['id'])['status'],'error');self.assertTrue(self.r.wake({})['skip'])

    def test_fixed_maintenance_preserved_and_not_newly_creatable(self):
        self.r.settings['pulse_policy']['enabled']=False
        maintenance=self.alarm('');self.r.settings['pulse_policy']['enabled']=True
        self.r.settings['pulse_policy']['existing_maintenance_ids']=[maintenance['id']]
        self.due(maintenance['id']);wake=self.r.wake({});self.finish(wake)
        self.assertEqual(self.get(maintenance['id'])['status'],'pending')

    def test_latest_cancel_stays_cancelled_after_inflight_finish(self):
        alarm=self.alarm();self.due(alarm['id']);wake=self.r.wake({})
        self.r.call('silent_alarm',{'action':'cancel','id':alarm['id'],'_context':self.ctx});self.finish(wake,contacted=1)
        self.assertEqual(self.get(alarm['id'])['status'],'cancelled')


if __name__=='__main__':unittest.main(verbosity=2)
