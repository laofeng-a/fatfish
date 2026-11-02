"""403 contract: keep upstream evidence, no retry multiplication in the relay."""
import importlib.util,io,json,unittest,urllib.error
from pathlib import Path
from unittest.mock import Mock
spec=importlib.util.spec_from_file_location('retry_gateway',Path(__file__).with_name('budget-gateway.py'))
g=importlib.util.module_from_spec(spec);spec.loader.exec_module(g)
class Upstream403Tests(unittest.TestCase):
 def test_unknown_auth_risk_403_retries_but_explicit_policy_and_parameters_do_not(self):
  for code,retry in [('AccessDenied.Unpurchased',True),('invalid_api_key',True),('risk_control',True),('',True),
                     ('content_filter',False),('data_inspection_failed',False),('InvalidParameter.Range',False)]:
   with self.subTest(code=code):self.assertEqual(g.upstream_retryable(403,code,''),retry)
  self.assertFalse(g.upstream_retryable(400,'invalid_request','bad parameters'))
  self.assertFalse(g.upstream_retryable(401))
  self.assertTrue(g.upstream_retryable(429));self.assertTrue(g.upstream_retryable(503))
  self.assertTrue(g.upstream_retryable(403,'AccessDenied.Unpurchased',
   json.dumps({'error':{'code':'AccessDenied.Unpurchased','type':'invalid_request_error','message':'model access denied'}})))
 def test_403_summary_and_original_code_preserved_with_one_upstream_attempt(self):
  for code in ('AccessDenied.Unpurchased','content_filter'):
   body=json.dumps({'error':{'code':code,'message':'model denied'}}).encode()
   opener=Mock();opener.open.side_effect=urllib.error.HTTPError('https://provider',403,'Forbidden',{},io.BytesIO(body))
   with self.assertRaises(g.UpstreamFailure) as got:g.fetch_upstream(opener,object())
   self.assertEqual(got.exception.upstream_status,403);self.assertEqual(got.exception.upstream_code,code)
   self.assertIn('model denied',got.exception.body_summary);opener.open.assert_called_once()
 def test_html_gateway_response_is_diagnostic_and_retryable(self):
  opener=Mock();opener.open.side_effect=urllib.error.HTTPError('https://provider',403,'Forbidden',{},io.BytesIO(b'<html>edge auth failure</html>'))
  with self.assertRaises(g.UpstreamFailure) as got:g.fetch_upstream(opener,object())
  self.assertIn('edge auth failure',got.exception.body_summary);self.assertTrue(g.upstream_retryable(403,'',got.exception.body_summary))
if __name__=='__main__':unittest.main(verbosity=2)
