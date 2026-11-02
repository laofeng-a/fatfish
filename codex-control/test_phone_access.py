import json
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import Mock,patch
from phone_access import PhoneAccess


class PhoneTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory();self.control=Mock();self.control.call.return_value={'engine_ready':True}
        self.manager=PhoneAccess(self.control,Path(self.folder.name));self.manager.origin='https://test-owner.trycloudflare.com'
        self.server=self.manager.make_server(0);self.manager.server=self.server
        threading.Thread(target=self.server.serve_forever,daemon=True).start()
        self.url='http://127.0.0.1:'+str(self.server.server_port)
        self.opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    def tearDown(self):self.manager.close();self.folder.cleanup()
    def request(self,path='/bootstrap',body=None,headers=None):
        base={'Host':self.manager.host}
        if body is not None:base.update({'Origin':self.manager.origin,'Content-Type':'application/json'})
        base.update(headers or {})
        request=urllib.request.Request(self.url+path,data=json.dumps(body).encode() if body is not None else None,headers=base)
        return self.opener.open(request,timeout=4)
    def assert_code(self,code,**kwargs):
        with self.assertRaises(urllib.error.HTTPError) as caught:self.request(**kwargs)
        self.assertEqual(caught.exception.code,code)
    def test_no_local_bypass_without_the_private_hop_capability(self):
        self.manager.armed=True
        for host in ('127.0.0.1:'+str(self.server.server_port),'test-owner.trycloudflare.com','forged.localhost'):
            self.assert_code(403,headers={'Host':host})
        self.control.call.assert_not_called()
    def test_unverified_email_gate_never_exposes_the_app(self):
        self.assert_code(503)
        self.assert_code(503,path='/api',body={'action':'list'})
        self.control.call.assert_not_called()
    def test_same_controller_after_email_gate_no_local_token_in_browser(self):
        self.manager.armed=True
        with self.request() as response:self.assertEqual(json.load(response),{'mode':'phone'})
        with self.request('/api',{'action':'status'}) as response:self.assertTrue(json.load(response)['result']['engine_ready'])
        self.control.call.assert_called_once_with({'action':'status'})
        self.assertNotIn(self.manager.host,json.dumps(self.manager.public_status()))
        self.assertEqual(self.server.server_address[0],'127.0.0.1')
    def test_cross_site_requests_and_private_endpoints_are_rejected(self):
        self.manager.armed=True
        self.assert_code(403,path='/api',body={'action':'send'},headers={'Origin':'https://evil.example'})
        self.assert_code(403,path='/api',body={'action':'status'},headers={'Origin':''})
        self.assert_code(400,path='/api',body={'action':'phone_status'})
        self.assert_code(404,path='/control-ui.json')
        self.assert_code(404,path='/../connector.json')
        self.control.call.assert_not_called()
    def test_gate_probe_requires_an_access_redirect_not_a_success_page(self):
        opener=Mock()
        with patch('phone_access.urllib.request.build_opener',return_value=opener):
            opener.open.return_value=Mock();self.assertFalse(self.manager.verify_login_gate())
            for location,expected in [('https://owner.cloudflareaccess.com/cdn-cgi/access/login/123',True),('https://evil.example/cdn-cgi/access/login/123',False),('http://owner.cloudflareaccess.com/cdn-cgi/access/login/123',False),('https://login.trycloudflare.com/authorize?hostname=test-owner.trycloudflare.com&state=nonce',True),('https://login.trycloudflare.com/authorize?hostname=another.trycloudflare.com&state=nonce',False)]:
                opener.open.side_effect=urllib.error.HTTPError(self.manager.origin,302,'Redirect',{'Location':location},None)
                self.assertEqual(self.manager.verify_login_gate(),expected)


if __name__=='__main__':unittest.main()
