import unittest
from unittest.mock import Mock
import test_worker

class NativeThreadWatchTests(unittest.TestCase):
    def app(self,turns,cursor=None):
        app=test_worker.WorkerTests().app();app.routes={'thread-native':{'thread_id':'thread-native','native':True,'shared':True}}
        app.rpc=Mock(return_value={'data':turns})
        app.rollout_lifecycle=Mock(return_value={t['id']:{'event':'task_started' if t['status']=='inProgress' else 'task_complete','final_text':'final '+t['id']} for t in turns})
        return app,[{'thread_id':'thread-native','cursor_turn_id':cursor}]
    def turn(self,name,status='completed'):
        return {'id':name,'status':status,'items':[{'type':'userMessage','content':[{'type':'text','text':'jg continued in the desktop'}]},
            {'type':'agentMessage','phase':'commentary' if status=='inProgress' else 'final_answer','text':'answer '+name}]}
    def test_bootstrap_backfills_latest_final_even_while_new_turn_runs(self):
        app,watches=self.app([self.turn('new-running','inProgress'),self.turn('last-final'),self.turn('older-final')])
        events=app.poll_native_threads(watches)
        self.assertEqual([e['turn_id'] for e in events],['last-final']);self.assertEqual(events[0]['text'],'answer last-final')
        self.assertEqual(events[0]['action'],'native_turn');self.assertTrue(events[0]['terminal_confirmed'])
    def test_follows_desktop_turns_after_original_submission_was_interrupted(self):
        app,watches=self.app([self.turn('newer-final'),self.turn('original-final'),self.turn('old-final')],'original-final')
        events=app.poll_native_threads(watches)
        self.assertEqual([e['turn_id'] for e in events],['newer-final'])
        self.assertIn('desktop',events[0]['user_text'])
    def test_cursor_survives_worker_restart_and_suppresses_replay(self):
        app,watches=self.app([self.turn('already-received')],'already-received')
        self.assertEqual(app.poll_native_threads(watches),[])
    def test_two_new_finals_are_published_in_time_order(self):
        app,watches=self.app([self.turn('third'),self.turn('second'),self.turn('first')],'first')
        self.assertEqual([e['turn_id'] for e in app.poll_native_threads(watches)],['second','third'])
    def test_running_snapshot_is_not_a_completion(self):
        app,watches=self.app([self.turn('running','inProgress')])
        self.assertEqual(app.poll_native_threads(watches),[])
    def test_only_registered_shared_native_routes_are_watched(self):
        app,watches=self.app([self.turn('done')]);app.routes['thread-native']['native']=False
        self.assertEqual(app.poll_native_threads(watches),[]);app.rpc.assert_not_called()
    def test_failed_without_answer_preserves_failure(self):
        turn={'id':'failed-turn','status':'failed','items':[],'error':{'code':'usage_limit_exceeded','message':'quota'}}
        app,watches=self.app([turn]);app.rollout_lifecycle.return_value={'failed-turn':{'event':'task_complete','error':turn['error']}}
        result=app.poll_native_threads(watches)[0]
        self.assertEqual(result['status'],'failed');self.assertFalse(result['reply_present']);self.assertEqual(result['text'],'')

if __name__=='__main__':unittest.main()
