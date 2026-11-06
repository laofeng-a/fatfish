import base64
import hashlib
import tempfile
from pathlib import Path
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch
import test_worker

worker=test_worker.worker

class RuntimeRecoveryTests(unittest.TestCase):
    def app(self):return test_worker.WorkerTests().app()

    def test_light_status_does_not_expand_turn_history(self):
        app=self.app();app.rpc.side_effect=[{'thread':{'id':'native-thread','status':{'type':'idle'}}},{'data':[]}]
        result=app.execute({'action':'read','detail':'status','thread_id':'native-thread'})
        self.assertEqual(result['detail'],'status');self.assertEqual(result['pending_messages'],[])
        self.assertEqual([c.args[0]for c in app.rpc.call_args_list],['thread/read','thread/queue/list'])

    def test_file_read_transfers_exact_bytes_without_starting_turn(self):
        with tempfile.TemporaryDirectory() as directory:
            raw='原始完整报告\n'.encode();path=Path(directory)/'report.md';path.write_bytes(raw)
            app=self.app();app.rpc.return_value={'thread':{'id':'native-thread','cwd':directory}}
            result=app.execute({'action':'artifact','thread_id':'native-thread','path':'report.md'})
            self.assertEqual(base64.b64decode(result['content_base64']),raw);self.assertEqual(result['sha256'],hashlib.sha256(raw).hexdigest())
            self.assertEqual([c.args[0]for c in app.rpc.call_args_list],['thread/read'])

    def test_watcher_yields_without_claiming_foreground_request(self):
        post=Mock(return_value={'job':None,'work_waiting':True});previous=Mock()
        app=SimpleNamespace(wait_callback=previous,poll_watchers=Mock(),poll_native_threads=Mock())
        self.assertEqual(worker.poll_background(app,SimpleNamespace(lock=threading.RLock()),post,[],[]),[])
        app.poll_watchers.assert_not_called();self.assertIs(app.wait_callback,previous)

if __name__=='__main__':unittest.main(verbosity=2)
