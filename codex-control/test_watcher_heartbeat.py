import threading,unittest
from types import SimpleNamespace
from unittest.mock import Mock,patch
from worker import poll_background
class HeartbeatTests(unittest.TestCase):
    def test_slow_watch_rpc_keeps_heartbeat_without_consuming_queued_jobs(self):
        app=SimpleNamespace(wait_callback=None);post=Mock(return_value={'job':None})
        def slow_read(_):
            app.wait_callback();return [{'observed':True}]
        app.poll_watchers=slow_read;app.poll_native_threads=lambda _:[]
        with patch('worker.time.monotonic',side_effect=[10,16]):result=poll_background(app,SimpleNamespace(lock=threading.RLock()),post,[{}],[])
        self.assertEqual(result,[{'observed':True}]);self.assertEqual(post.call_count,2)
        self.assertTrue(all(c.args[0]['active_id']=='watcher-heartbeat' for c in post.call_args_list));self.assertIsNone(app.wait_callback)
    def test_callback_restored_after_read_failure(self):
        previous=Mock();app=SimpleNamespace(wait_callback=previous,poll_watchers=Mock(side_effect=RuntimeError('read failed')))
        with self.assertRaises(RuntimeError):poll_background(app,SimpleNamespace(lock=threading.RLock()),Mock(),[],[])
        self.assertIs(app.wait_callback,previous)
if __name__=='__main__':unittest.main(verbosity=2)
