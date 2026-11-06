"""Fixed SSH protocol and actual HTTP handler authority, without opening sockets."""
import importlib.util,io,json,unittest
from pathlib import Path
from unittest.mock import Mock,MagicMock,patch

def load(name,filename):
    spec=importlib.util.spec_from_file_location(name,Path(__file__).with_name(filename))
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module

entry=load('ssh_entry','codex-ssh-entry.py')
base=load('ssh_entry_base','test-fatfish.py')
facade=load('ssh_entry_facade','memory-reader.py')

class EntryTests(unittest.TestCase):
    def test_only_fixed_bridge_destination_and_worker_operations(self):
        opener=MagicMock();opener.open.return_value.__enter__.return_value.read.return_value=b'{"job":null}'
        value=entry.dispatch({'id':1,'token':'a'*40,'data':{'action':'poll','url':'https://invalid.example'}},opener)
        request=opener.open.call_args.args[0]
        self.assertEqual(request.full_url,'http://127.0.0.1:18792/bridge')
        self.assertEqual(value,{'id':1,'result':{'job':None}})
        for action in ('exec','mcp','inbound','send'):
            with self.assertRaises(ValueError):entry.dispatch({'id':1,'token':'a'*40,'data':{'action':action}},opener)
    def test_malformed_envelopes_never_reach_http(self):
        opener=Mock()
        for data in ([],{}, {'id':True,'token':'a'*40,'data':{'action':'poll'}},{'id':1,'token':'','data':{'action':'poll'}},{'id':1,'token':'a'*40,'data':[]}):
            with self.assertRaises(ValueError):entry.dispatch(data,opener)
        opener.open.assert_not_called()
    def test_oversized_lines_close_without_forwarding(self):
        with patch.object(entry,'dispatch') as dispatch:
            output=io.StringIO();entry.serve(io.StringIO('a'*(entry.MAX_LINE+1)+'\n'),output)
        dispatch.assert_not_called();self.assertEqual(output.getvalue(),'')
    def test_internal_errors_do_not_echo_secrets(self):
        output=io.StringIO()
        with patch.object(entry,'dispatch',side_effect=RuntimeError('credential-must-not-escape')):
            entry.serve(io.StringIO(json.dumps({'id':2,'token':'private-secret'})+'\n'),output)
        self.assertEqual(json.loads(output.getvalue()),{'id':2,'error':'Bridge request failed'})
    def test_oversized_http_result_rejected(self):
        opener=MagicMock();opener.open.return_value.__enter__.return_value.read.return_value=b'x'*65537
        with self.assertRaises(ValueError):entry.dispatch({'id':1,'token':'a'*40,'data':{'action':'poll'}},opener)

class AuthorityTests(unittest.TestCase):
    setUp=base.PolicyTests.setUp
    def handler(self,bridge=True):
        directory=Path(self.tmp.name)
        (directory/'upstream').write_text('not-used');(directory/'main').write_text('main-key')
        (directory/'worker').write_text('worker-key')
        if bridge:self.r.settings['codex_bridge_token_file']=str(directory/'worker')
        (directory/'facade.json').write_text(json.dumps({'origin':facade.ORIGIN,'local_token_file':str(directory/'main'),'upstream_key_file':str(directory/'upstream')}))
        self.r.reminders.start=Mock();servers=[]
        class FakeServer:
            def __init__(self,address,handler):self.handler=handler
            def serve_forever(self):pass
        with patch.object(facade.http.server,'ThreadingHTTPServer',FakeServer):
            facade.serve(runtime=self.r,settings_path=directory/'facade.json',on_ready=servers.append)
        return servers[0].handler
    def post(self,handler,path,credential,message):
        request=handler.__new__(handler);request.path=path;request.send=Mock()
        raw=json.dumps(message).encode();request.rfile=io.BytesIO(raw)
        request.headers={'Authorization':'Bearer '+credential,'Content-Length':str(len(raw))}
        request.do_POST();return request.send.call_args.args
    def test_worker_key_cannot_use_mcp_or_runtime_and_main_key_cannot_use_worker(self):
        handler=self.handler()
        for path,key in [('/mcp','worker-key'),('/runtime/inbound','worker-key'),('/bridge','main-key'),('/bridge','')]:
            self.assertEqual(self.post(handler,path,key,{'action':'poll'}),(401,))
        self.assertEqual(self.post(handler,'/bridge','worker-key',{'action':'poll'}),(200,{'job':None,'watches':[],'native_watches':[]}))
        result=self.post(handler,'/mcp','main-key',{'jsonrpc':'2.0','id':1,'method':'tools/list'})
        self.assertEqual(result[0],200);self.assertIn('codex',[t['name'] for t in result[1]['result']['tools']])
    def test_unconfigured_bridge_fails_closed(self):
        handler=self.handler(bridge=False)
        self.assertEqual(self.post(handler,'/bridge','worker-key',{'action':'poll'}),(401,))
    def test_worker_has_no_runtime_action_even_with_valid_worker_key(self):
        handler=self.handler();self.r.action=Mock()
        self.assertEqual(self.post(handler,'/bridge','worker-key',{'action':'inbound','sender_id':'123'}),(400,{'error':'Invalid MCP request'}))
        self.r.action.assert_not_called()

if __name__=='__main__':unittest.main()
