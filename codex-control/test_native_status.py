import json,tempfile,unittest
from pathlib import Path
from unittest.mock import Mock,patch
import test_worker
worker=test_worker.worker


class NativeStatusTests(unittest.TestCase):
    def app(self):
        return test_worker.WorkerTests().app()

    def turn(self,status='interrupted',phase='commentary',text='still working'):
        return {'id':'turn-one','status':status,'items':[
            {'type':'userMessage','clientId':'fatfish-job-one','content':[{'type':'text','text':'please inspect'}]},
            {'type':'agentMessage','phase':phase,'text':text}]}

    def inspect(self,turn,evidence):
        app=self.app();app.rpc.side_effect=[{'data':[]},{'data':[turn]}]
        app.rollout_lifecycle=Mock(return_value={'turn-one':evidence} if evidence else {})
        return app,app.inspect_submission('thread','fatfish-job-one')

    def test_persisted_interrupted_with_open_durable_turn_is_running(self):
        _,got=self.inspect(self.turn(),{'event':'task_started','at':'start-time'})
        self.assertEqual(got['state'],'running');self.assertEqual(got['status'],'inProgress')
        self.assertEqual(got['reported_status'],'interrupted');self.assertFalse(got['terminal_confirmed'])
        self.assertEqual(got['text'],'');self.assertEqual(got['status_source'],'rollout_task_started')

    def test_interrupted_without_terminal_evidence_stays_pending_even_after_five_minutes(self):
        app,got=self.inspect(self.turn(),None)
        self.assertEqual(got['state'],'pending');self.assertFalse(got['terminal_confirmed'])
        app.inspect_submission=Mock(return_value=got)
        events=app.poll_watchers([{'job_id':'job-one','thread_id':'thread','created':0}])
        self.assertEqual([e['action'] for e in events],['observation'])
        self.assertFalse(events[0]['terminal_confirmed'])

    def test_nonterminal_observation_only_published_when_state_changes(self):
        app,got=self.inspect(self.turn(),{'event':'task_started','at':'start-time'})
        app.inspect_submission=Mock(return_value=got)
        watch={'job_id':'job-one','thread_id':'thread','created':0}
        self.assertEqual(app.poll_watchers([watch])[0]['action'],'observation')
        app.watcher_seen={}
        self.assertEqual(app.poll_watchers([watch]),[])
        app.inspect_submission.return_value=dict(got,status_source='persisted_snapshot_unconfirmed',execution_state='pending',state='pending')
        app.watcher_seen={}
        self.assertEqual(app.poll_watchers([watch])[0]['execution_state'],'pending')

    def test_confirmed_completion_overrides_stale_interrupted_and_reaches_old_event_path(self):
        app,got=self.inspect(self.turn(),{'event':'task_complete','at':'finish-time','final_text':'actual final'})
        self.assertEqual(got['state'],'completed');self.assertEqual(got['status'],'completed')
        self.assertTrue(got['terminal_confirmed']);self.assertEqual(got['text'],'actual final')
        app.inspect_submission=Mock(return_value=got)
        event=app.poll_watchers([{'job_id':'job-one','thread_id':'thread','created':0}])[0]
        self.assertEqual(event['action'],'completed');self.assertEqual(event['status_source'],'rollout_task_complete')
        self.assertEqual(event['task_goal_status'],'not_evaluated')

    def test_persisted_completed_with_only_commentary_is_not_completion(self):
        _,got=self.inspect(self.turn('completed'),None)
        self.assertEqual(got['state'],'pending');self.assertEqual(got['text'],'')

    def test_real_abort_is_reported_only_with_exact_durable_terminal(self):
        _,got=self.inspect(self.turn(),{'event':'turn_aborted','at':'abort-time'})
        self.assertEqual(got['state'],'completed');self.assertEqual(got['status'],'interrupted')
        self.assertTrue(got['terminal_confirmed']);self.assertEqual(got['text'],'')

    def test_usage_limit_complete_without_final_is_a_failed_turn(self):
        # Real diary turn 01a16cf7-a5fb-7743-9453-1d7419d801aa
        # ended after 1061 ms with only the triggering user message.
        turn=self.turn('failed');turn['items']=turn['items'][:1]
        turn['error']={'message':'Usage limit reached','codexErrorInfo':'usageLimitExceeded'}
        app,got=self.inspect(turn,{'event':'task_complete','at':'2026-10-24T08:12:12.801Z',
            'error':{'code':'usage_limit_exceeded','message':'Usage limit reached'}})
        self.assertEqual(got['state'],'failed');self.assertEqual(got['status'],'failed')
        self.assertEqual(got['reported_status'],'failed');self.assertTrue(got['terminal_confirmed'])
        self.assertEqual(got['completion_outcome'],'failed');self.assertFalse(got['reply_present'])
        self.assertEqual(got['text'],'');self.assertIsNone(got['phase'])
        self.assertEqual(got['error']['code'],'usage_limit_exceeded')
        app.inspect_submission=Mock(return_value=got)
        event=app.poll_watchers([{'job_id':'job-one','thread_id':'thread','created':0}])[0]
        self.assertEqual(event['action'],'completed');self.assertEqual(event['status'],'failed')
        self.assertEqual(event['execution_state'],'failed');self.assertFalse(event['reply_present'])

    def test_durable_error_overrides_a_stale_completed_projection(self):
        _,got=self.inspect(self.turn('completed','final_answer','partial answer'),
            {'event':'task_complete','error':{'message':'Model failed','code':'provider_error'}})
        self.assertEqual(got['status'],'failed');self.assertEqual(got['completion_outcome'],'failed')
        self.assertTrue(got['reply_present']);self.assertEqual(got['text'],'partial answer')

    def test_complete_with_no_final_or_error_is_terminal_with_unknown_outcome(self):
        app,got=self.inspect(self.turn('completed'),{'event':'task_complete'})
        self.assertTrue(got['terminal_confirmed']);self.assertEqual(got['status'],'unknown')
        self.assertEqual(got['completion_outcome'],'unknown');self.assertFalse(got['reply_present'])
        self.assertEqual(got['state'],'completed');self.assertEqual(got['text'],'')
        app.inspect_submission=Mock(return_value=got)
        event=app.poll_watchers([{'job_id':'job-one','thread_id':'thread','created':0}])[0]
        self.assertEqual(event['status'],'unknown');self.assertTrue(event['terminal_confirmed'])

    def test_persisted_failed_without_lifecycle_does_not_become_pending_success(self):
        _,got=self.inspect(self.turn('failed'),None)
        self.assertEqual(got['state'],'failed');self.assertEqual(got['status'],'failed')
        self.assertTrue(got['terminal_confirmed']);self.assertEqual(got['completion_outcome'],'failed')

    def test_blank_final_does_not_establish_success(self):
        _,got=self.inspect(self.turn('completed','final_answer','  '),{'event':'task_complete','final_text':' '})
        self.assertFalse(got['reply_present']);self.assertEqual(got['completion_outcome'],'unknown')

    def test_read_keeps_full_final_before_progress_and_labels_truncation(self):
        app=self.app();turn=self.turn('completed','final_answer','F'*3254)
        turn['items'].extend({'type':'agentMessage','phase':'commentary','text':'C'*1800} for _ in range(6))
        app.rpc.side_effect=[{'thread':{'status':{'type':'notLoaded'}}},{'data':[turn]},{'data':[]}]
        got=app.execute({'action':'read','thread_id':'thread'})
        final=next(m for m in got['turns'][0]['messages'] if m['is_final'])
        commentary=next(m for m in got['turns'][0]['messages'] if m['phase']=='commentary')
        self.assertEqual(final['text'],'F'*3254);self.assertFalse(final['truncated'])
        self.assertTrue(commentary['truncated']);self.assertFalse(commentary['is_final'])
        self.assertEqual(got['request_state'],'done');self.assertEqual(got['task_goal_status'],'not_evaluated')
        self.assertEqual(got['turns'][0]['messages_omitted'],5)

    def test_non_native_thread_never_reads_rollout_or_extra_metadata(self):
        app=self.app();self.assertEqual(app.rollout_lifecycle('thread',['turn-one']),{})
        app.rpc.assert_not_called()

    def test_rollout_scope_identity_exact_turn_and_latest_lifecycle(self):
        with tempfile.TemporaryDirectory() as folder,patch.dict(worker.os.environ,{'CODEX_HOME':folder}):
            home=Path(folder);sessions=home/'sessions';sessions.mkdir();path=sessions/'own.jsonl'
            app=self.app();app.routes={'thread':{'thread_id':'thread','native':True}}
            def write(identity='thread'):
                records=[{'type':'session_meta','payload':{'id':identity}},
                    {'type':'event_msg','timestamp':'t1','payload':{'type':'task_started','turn_id':'turn-one'}},
                    {'type':'event_msg','timestamp':'t2','payload':{'type':'task_complete','turn_id':'different-turn','last_agent_message':'wrong'}},
                    {'type':'event_msg','timestamp':'t3','payload':{'type':'task_complete','turn_id':'turn-one','last_agent_message':'correct'}}]
                path.write_text('\n'.join(json.dumps(r) for r in records)+'\n',encoding='utf-8')
            write();got=app.rollout_lifecycle('thread',['turn-one'],{'path':str(path)})
            self.assertEqual(got,{'turn-one':{'event':'task_complete','at':'t3','final_text':'correct'}})
            write('someone-else');self.assertEqual(app.rollout_lifecycle('thread',['turn-one'],{'path':str(path)}),{})
            outside=home/'outside.jsonl';outside.write_text(path.read_text(),encoding='utf-8')
            self.assertEqual(app.rollout_lifecycle('thread',['turn-one'],{'path':str(outside)}),{})

    def test_rollout_preserves_bounded_error_on_terminal_event(self):
        with tempfile.TemporaryDirectory() as folder,patch.dict(worker.os.environ,{'CODEX_HOME':folder}):
            path=Path(folder)/'sessions'/'own.jsonl';path.parent.mkdir()
            records=[{'type':'session_meta','payload':{'id':'thread'}},
                {'type':'event_msg','payload':{'type':'task_complete','turn_id':'turn-one',
                    'last_agent_message':None,'error':{'message':'x'*1000,'codex_error_info':'usage_limit_exceeded'}}}]
            path.write_text('\n'.join(json.dumps(r) for r in records)+'\n',encoding='utf-8')
            app=self.app();app.routes={'thread':{'thread_id':'thread','native':True}}
            got=app.rollout_lifecycle('thread',['turn-one'],{'path':str(path)})['turn-one']
            self.assertNotIn('final_text',got);self.assertEqual(got['error']['code'],'usage_limit_exceeded')
            self.assertEqual(len(got['error']['message']),500)

    def test_lifecycle_older_than_bounded_tail_is_unknown(self):
        with tempfile.TemporaryDirectory() as folder,patch.dict(worker.os.environ,{'CODEX_HOME':folder}):
            path=Path(folder)/'sessions'/'own.jsonl';path.parent.mkdir()
            records=[{'type':'session_meta','payload':{'id':'thread'}},
                {'type':'event_msg','payload':{'type':'task_started','turn_id':'turn-one'}}]
            with path.open('w',encoding='utf-8') as stream:
                for row in records:stream.write(json.dumps(row)+'\n')
                line=json.dumps({'type':'padding','payload':'x'*1000})+'\n'
                for _ in range(4500):stream.write(line)
            app=self.app();app.routes={'thread':{'thread_id':'thread','native':True}}
            self.assertEqual(app.rollout_lifecycle('thread',['turn-one'],{'path':str(path)}),{})


if __name__=='__main__':unittest.main()
