import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest.mock import Mock
from shared_control import SharedControl,start_control

SOURCE='source-thread-12345';TARGET='shared-thread-12345'

class SharedTests(unittest.TestCase):
    def controller(self):
        app=Mock();app.routes={SOURCE:{'thread_id':TARGET,'shared':True}}
        app.execute.return_value={'thread_id':TARGET,'state':'submitted'}
        return SharedControl(app)
    def test_human_and_bot_use_one_app_and_one_thread(self):
        control=self.controller()
        human=control.call({'action':'send','thread_id':SOURCE,'text':'人类补充','request_id':'human-unique-123'})
        bot=control.execute({'action':'send','thread_id':TARGET,'text':'机器人转达'},lambda:None)
        calls=control.app.execute.call_args_list
        self.assertEqual(human['thread_id'],bot['thread_id'])
        self.assertEqual(calls[0].args[0]['thread_id'],TARGET)
        self.assertIn('[jg · 共用控制页]',calls[0].args[0]['text'])
        self.assertIn('[大肥鱼转达 jg]',calls[1].args[0]['text'])
        self.assertIsNone(control.app.wait_callback)
    def test_retry_dedup_and_uncertainty_never_resubmit(self):
        control=self.controller();request={'action':'send','thread_id':TARGET,'text':'一次','request_id':'same-request-id'}
        control.call(request);control.call(request);self.assertEqual(control.app.execute.call_count,1)
        with self.assertRaises(ValueError):control.call(dict(request,text='另一个任务'))
        control.app.execute=Mock(side_effect=TimeoutError())
        request['request_id']='timeout-request-id'
        with self.assertRaises(TimeoutError):control.call(request)
        with self.assertRaises(ValueError):control.call(request)
        self.assertEqual(control.app.execute.call_count,1)
    def test_no_unattached_thread_control(self):
        control=self.controller()
        with self.assertRaises(ValueError):control.call({'action':'stop','thread_id':'unknown-thread-12345','request_id':'stop-request-id'})
        control.app.execute.assert_not_called()
    def test_operations_serialize_across_both_frontends(self):
        control=self.controller();active=0;peak=0
        def work(job):
            nonlocal active,peak
            active+=1;peak=max(peak,active);time.sleep(.03);active-=1
            return {'thread_id':TARGET}
        control.app.execute.side_effect=work
        threads=[threading.Thread(target=control.execute,args=({'action':'read','thread_id':TARGET},)) for _ in range(4)]
        for thread in threads:thread.start()
        for thread in threads:thread.join()
        self.assertEqual(peak,1)
    def test_history_uses_shared_id_and_preserves_cursor(self):
        control=self.controller()
        control.app.rpc.side_effect=[{'data':[{'id':'turn','status':'completed','items':[{'type':'userMessage','content':[{'text':'你好'}]},{'type':'agentMessage','text':'嗨'}]}],'nextCursor':'older'},{'thread':{'name':'共用','status':{'type':'idle'}}}]
        result=control.call({'action':'history','thread_id':SOURCE,'cursor':'cursor1'})
        self.assertEqual(result['thread_id'],TARGET);self.assertEqual(result['next_cursor'],'older')
        self.assertEqual(control.app.rpc.call_args_list[0].args[1]['cursor'],'cursor1')
        self.assertEqual(result['turns'][0]['messages'][0]['text'],'你好')
    def test_large_tool_results_do_not_hide_final_chat_reply(self):
        control=self.controller();items=[{'type':'commandExecution','output':'x'*90000} for _ in range(50)]+[{'type':'agentMessage','text':'最后的有效回复'}]
        control.app.rpc.side_effect=[{'data':[{'items':items}]},{'thread':{'status':{'type':'idle'}}}]
        result=control.call({'action':'history','thread_id':TARGET})
        self.assertEqual(result['turns'][0]['messages'][-1]['text'],'最后的有效回复')
        self.assertGreater(result['turns'][0]['omitted_tool_items'],0)
    def test_local_http_auth_and_cross_origin_rejection(self):
        control=self.controller()
        with tempfile.TemporaryDirectory() as folder:
            state=Path(folder)/'state.json';server=start_control(control,state)
            try:
                info=json.loads(state.read_text());url=info['url'];op=urllib.request.build_opener(urllib.request.ProxyHandler({}))
                def post(headers):
                    req=urllib.request.Request(url+'api',data=b'{"action":"list"}',headers=dict({'Content-Type':'application/json'},**headers))
                    return op.open(req,timeout=5)
                for headers,expected in [({},401),({'Authorization':'Bearer '+info['token'],'Origin':'https://untrusted.example'},403),({'Authorization':'Bearer '+info['token'],'Host':'untrusted.example'},403)]:
                    with self.assertRaises(urllib.error.HTTPError) as error:post(headers)
                    self.assertEqual(error.exception.code,expected)
                control.app.execute.return_value={'threads':[]}
                with post({'Authorization':'Bearer '+info['token']}) as response:
                    result=json.load(response)['result']
                    self.assertEqual(result['threads'],[]);self.assertEqual(result['default_thread_id'],TARGET)
                self.assertEqual(server.server_address[0],'127.0.0.1')
            finally:server.shutdown();server.server_close()

    def test_page_stays_available_while_engine_is_unavailable(self):
        control=SharedControl()
        with tempfile.TemporaryDirectory() as folder:
            state=Path(folder)/'state.json';server=start_control(control,state)
            try:
                info=json.loads(state.read_text());op=urllib.request.build_opener(urllib.request.ProxyHandler({}))
                with op.open(info['url']+'healthz',timeout=5) as response:
                    health=json.load(response)
                    self.assertTrue(health['page_online']);self.assertNotIn('token',health)
                with op.open(info['url'],timeout=5) as response:self.assertEqual(response.status,200)
                def post(action):
                    request=urllib.request.Request(info['url']+'api',data=json.dumps({'action':action}).encode(),headers={'Content-Type':'application/json','Authorization':'Bearer '+info['token']})
                    return op.open(request,timeout=5)
                with post('status') as response:self.assertFalse(json.load(response)['result']['engine_ready'])
                with self.assertRaises(urllib.error.HTTPError) as error:post('list')
                self.assertEqual(error.exception.code,503)
                app=Mock();app.routes={};app.execute.return_value={'threads':[]};control.set_app(app)
                with post('list') as response:self.assertEqual(json.load(response)['result']['threads'],[])
                control.set_app(None)
                with op.open(info['url']+'healthz',timeout=5) as response:self.assertEqual(response.status,200)
            finally:server.shutdown();server.server_close()

    def test_open_tab_credential_and_address_survive_page_restart(self):
        control=self.controller()
        with tempfile.TemporaryDirectory() as folder:
            state=Path(folder)/'state.json';server=start_control(control,state)
            old=json.loads(state.read_text());port=server.server_port;server.shutdown();server.server_close()
            server=start_control(control,state,port)
            try:
                new=json.loads(state.read_text());self.assertEqual(old['token'],new['token']);self.assertEqual(old['url'],new['url'])
                control.app.execute.return_value={'threads':[]}
                request=urllib.request.Request(old['url']+'api',data=b'{"action":"list"}',headers={'Content-Type':'application/json','Authorization':'Bearer '+old['token']})
                with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request,timeout=5) as response:self.assertEqual(response.status,200)
            finally:server.shutdown();server.server_close()

if __name__=='__main__':unittest.main()
