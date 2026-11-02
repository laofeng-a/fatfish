"""Official DeepSeek compatibility without changing Bailian behavior."""
import base64
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('deepseek_gateway',Path(__file__).with_name('budget-gateway.py'))
g=importlib.util.module_from_spec(spec);spec.loader.exec_module(g)

class DeepSeekGatewayTests(unittest.TestCase):
 def setUp(self):
  self.original=(g.MODEL,g.INPUT_PRICE,g.OUTPUT_PRICE)
  self.addCleanup(self.restore)
 def restore(self):
  g.MODEL,g.INPUT_PRICE,g.OUTPUT_PRICE=self.original
 def request(self,**extra):
  return dict(model=g.MODEL,messages=[{'role':'user','content':'hello'}],max_tokens=128,**extra)
 def test_official_base_urls_and_existing_bailian_still_work(self):
  for base in ('https://api.deepseek.com','https://api.deepseek.com/v1','https://api.deepseek.com/v1/'):
   self.assertEqual(g.validated_upstream(base),base.rstrip('/')+'/chat/completions')
  base='https://ws-demo.cn-beijing.maas.aliyuncs.com/compatible-mode/v1'
  self.assertEqual(g.validated_upstream(base),base+'/chat/completions')
 def test_configured_official_provider_uses_real_model_and_peak_estimate(self):
  url,provider=g.configure_upstream({'upstream_base_url':'https://api.deepseek.com/v1'})
  self.assertEqual((url,provider,g.MODEL),('https://api.deepseek.com/v1/chat/completions','deepseek','deepseek-flash'))
  self.assertEqual((g.INPUT_PRICE,g.OUTPUT_PRICE),(2.0,8.0))
 def test_bailian_retains_previous_model_and_dialect(self):
  _,provider=g.configure_upstream({'upstream_base_url':'https://dashscope.aliyuncs.com/compatible-mode/v1'})
  data=json.loads(g.prepare_request(self.request(enable_thinking=True),provider)[0])
  self.assertEqual(g.MODEL,'deepseek-v4.1-flash')
  self.assertTrue(data['enable_thinking']);self.assertFalse(data['enable_search'])
 def test_thinking_flag_maps_to_official_shape(self):
  g.configure_upstream({'upstream_base_url':'https://api.deepseek.com'})
  for enabled in (False,True):
   data=json.loads(g.prepare_request(self.request(enable_thinking=enabled),'deepseek')[0])
   self.assertEqual(data['thinking'],{'type':'enabled' if enabled else 'disabled'})
   self.assertNotIn('enable_thinking',data);self.assertNotIn('enable_search',data)
 def test_explicit_official_thinking_control_is_preserved(self):
  g.configure_upstream({'upstream_base_url':'https://api.deepseek.com'})
  data=json.loads(g.prepare_request(self.request(thinking={'type':'disabled'},reasoning_effort='high'),'deepseek')[0])
  self.assertEqual(data['thinking'],{'type':'disabled'})
 def test_tool_history_preserves_original_reasoning_and_result(self):
  g.configure_upstream({'upstream_base_url':'https://api.deepseek.com'})
  req=self.request();req['tools']=[{'type':'function','function':{'name':'probe','parameters':{'type':'object','properties':{}}}}]
  req['messages'] += [{'role':'assistant','content':None,'reasoning_content':'original reasoning',
   'tool_calls':[{'id':'c1','type':'function','function':{'name':'probe','arguments':'{}'}}]},
   {'role':'tool','tool_call_id':'c1','content':'confirmed result'}]
  data=json.loads(g.prepare_request(req,'deepseek')[0])
  self.assertEqual(data['messages'],req['messages']);self.assertEqual(data['tools'],req['tools'])
 def test_inline_images_are_preserved(self):
  g.configure_upstream({'upstream_base_url':'https://api.deepseek.com'})
  image={'type':'image_url','image_url':{'url':'data:image/png;base64,'+base64.b64encode(b'\x89PNG\r\n\x1a\nfixture').decode()}}
  req=self.request();req['messages'][0]['content']=[{'type':'text','text':'image'},image]
  data=json.loads(g.prepare_request(req,'deepseek')[0])
  self.assertEqual(data['messages'],req['messages'])
 def test_large_text_remains_allowed(self):
  g.configure_upstream({'upstream_base_url':'https://api.deepseek.com'})
  req=self.request();req['messages'][0]['content']='context '*40000
  self.assertEqual(json.loads(g.prepare_request(req,'deepseek')[0])['messages'],req['messages'])

if __name__=='__main__':unittest.main(verbosity=2)
