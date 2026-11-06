"""Disk-full diagnostics must return a structured response rather than EOF."""
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import Mock,patch

spec=importlib.util.spec_from_file_location('gateway_storage',Path(__file__).with_name('budget-gateway.py'))
g=importlib.util.module_from_spec(spec);spec.loader.exec_module(g)

class StorageRecoveryTests(unittest.TestCase):
    def test_disk_full_even_in_diagnostic_ledger_does_not_drop_response(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'upstream').write_text('test-upstream');(root/'local').write_text('test-local')
            cfg={'upstream_base_url':'https://api.deepseek.com','upstream_key_file':str(root/'upstream'),
                'local_token_file':str(root/'local'),'ledger_file':str(root/'ledger')}
            path=root/'settings.json';path.write_text(json.dumps(cfg))
            ledger=Mock();ledger.reserve.side_effect=sqlite3.OperationalError('database or disk is full')
            ledger.event.side_effect=sqlite3.OperationalError('database or disk is full')
            ready=threading.Event();servers=[];real=g.BoundedModelServer
            def factory(address,handler):
                server=real(('127.0.0.1',0),handler);servers.append(server);ready.set();return server
            with patch.object(g,'Ledger',return_value=ledger),patch.object(g,'ActivityLedger'),patch.object(g,'BoundedModelServer',side_effect=factory),patch.object(g,'limits_enabled',return_value=False),patch.object(g,'fetch_upstream') as upstream:
                worker=threading.Thread(target=g.serve,args=(path,),daemon=True);worker.start();self.assertTrue(ready.wait(3))
                try:
                    body=json.dumps({'model':g.MODEL,'messages':[{'role':'user','content':'test'}],'max_tokens':128}).encode()
                    req=urllib.request.Request('http://127.0.0.1:'+str(servers[0].server_port)+'/v1/chat/completions',data=body,headers={'Authorization':'Bearer test-local'})
                    with self.assertRaises(urllib.error.HTTPError) as failure:urllib.request.urlopen(req,timeout=3)
                    self.assertEqual(failure.exception.code,503)
                    result=json.load(failure.exception);self.assertEqual(result['error']['code'],'storage_unavailable')
                    self.assertFalse(result['error']['upstream_attempted']);upstream.assert_not_called()
                finally:servers[0].shutdown();servers[0].server_close();worker.join(3)

if __name__=='__main__':unittest.main(verbosity=2)
