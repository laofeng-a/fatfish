"""A slow summary cannot occupy the gateway's only request handler."""
import http.server
import importlib.util
from pathlib import Path
import threading
import unittest
import urllib.request

spec=importlib.util.spec_from_file_location('gateway',Path(__file__).with_name('budget-gateway.py'))
gateway=importlib.util.module_from_spec(spec);spec.loader.exec_module(gateway)


class GatewayConcurrencyTests(unittest.TestCase):
    def test_chat_is_served_while_summary_is_waiting(self):
        entered=threading.Event();release=threading.Event()
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                if self.path=='/summary':entered.set();release.wait(4)
                self.send_response(200);self.send_header('Content-Length','2');self.end_headers();self.wfile.write(b'OK')
        server=gateway.BoundedModelServer(('127.0.0.1',0),Handler)
        loop=threading.Thread(target=server.serve_forever,daemon=True);loop.start()
        url='http://127.0.0.1:'+str(server.server_port)
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        result=[]
        def summary():
            with opener.open(url+'/summary',timeout=5) as response:result.append(response.status)
        worker=threading.Thread(target=summary);worker.start()
        try:
            self.assertTrue(entered.wait(2))
            with opener.open(url+'/chat',timeout=2) as response:self.assertEqual(response.status,200)
            self.assertEqual(result,[])
        finally:
            release.set();worker.join(5);server.shutdown();server.server_close();loop.join(2)
        self.assertEqual(result,[200])


if __name__=='__main__':unittest.main(verbosity=2)
