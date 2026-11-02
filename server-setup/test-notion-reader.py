"""Notion reads, writes, receipts, and runtime dispatch."""
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
import urllib.error
import urllib.request
import urllib.parse
from notion_reader import NotionReader, NoRedirect, NotionAPIError, identifier, block, properties, MAX_RESPONSE

ID='401b6028-4bf2-81fb-87e3-c2683d5a0501'
PAGE={'object':'page','id':ID,'url':'https://www.notion.so/'+ID,'properties':{'Name':{'type':'title','title':[{'plain_text':'学习笔记'}]}}}

class ReaderTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        token=Path(self.tmp.name)/'token';token.write_text('ntn_'+'x'*46)
        self.r=NotionReader(token)
    def fake(self,value):
        response=io.BytesIO(json.dumps(value).encode());self.r.opener=Mock();self.r.opener.open.return_value=response
    def test_only_fixed_read_endpoints_and_no_secret_in_result(self):
        self.fake({'results':[PAGE],'has_more':False,'next_cursor':None})
        result=self.r.call({'action':'search','query':'学习'})
        request=self.r.opener.open.call_args.args[0]
        self.assertEqual(request.full_url,'https://api.notion.com/v1/search');self.assertEqual(request.method,'POST')
        self.assertEqual(request.get_header('Notion-version'),'2026-03-11')
        self.assertEqual(json.loads(request.data)['query'],'学习');self.assertEqual(result['search_scope'],'title_only')
        self.assertNotIn('ntn_',json.dumps(result))
    def test_invalid_actions_and_raw_arguments_rejected_before_network(self):
        self.r.opener=Mock()
        for action in ('delete','post_comment','request',None):
            with self.subTest(action=action),self.assertRaises(ValueError):self.r.call({'action':action})
        for extra in ({'url':'https://evil.test'},{'headers':{}},{'token':'x'},{'filter':{}}):
            with self.assertRaises(ValueError):self.r.call(dict(action='search',**extra))
        for method,path in [('DELETE','blocks/'+ID),('POST','users'),('GET','https://evil.test'),('GET','../search'),('GET','users/me')]:
            with self.assertRaises(ValueError):self.r.request(method,path)
        self.r.opener.open.assert_not_called()
    def test_create_page_payload_and_saved_receipt(self):
        self.fake(PAGE)
        result=self.r.call({'action':'create','parent_id':ID,'title':'验收','text':'第一段\n\n第二段'})
        req=self.r.opener.open.call_args.args[0];body=json.loads(req.data)
        self.assertEqual(req.method,'POST');self.assertTrue(req.full_url.endswith('/pages'))
        self.assertEqual(body['parent'],{'page_id':ID})
        self.assertEqual(body['properties']['title']['title'][0]['text']['content'],'验收')
        self.assertEqual(len(body['children']),2);self.assertEqual(result['status'],'saved')
        self.assertEqual(result['id'],ID)
    def test_create_data_source_uses_real_title_property(self):
        self.r.request=Mock(side_effect=[{'properties':{'任务':{'type':'title'}}},PAGE])
        self.r.call({'action':'create','parent_type':'data_source','parent_id':ID,'title':'导入日记'})
        body=self.r.request.call_args.args[2]
        self.assertEqual(body['parent'],{'data_source_id':ID})
        self.assertIn('任务',body['properties'])
    def test_append_native_todo_and_read_block(self):
        child={'object':'block','type':'to_do','to_do':{'rich_text':[{'type':'text','text':{'content':'导入日记'}}],'checked':False}}
        self.fake({'results':[dict(child,id=ID)],'has_more':False})
        result=self.r.call({'action':'append','id':ID,'children':[child]})
        req=self.r.opener.open.call_args.args[0]
        self.assertEqual(req.method,'PATCH');self.assertTrue(req.full_url.endswith('/blocks/'+ID+'/children'))
        self.assertEqual(json.loads(req.data)['children'],[child]);self.assertEqual(result['results'][0]['checked'],False)
        self.fake(dict(child,id=ID))
        self.assertEqual(self.r.call({'action':'block','id':ID})['text'],'导入日记')
    def test_update_page_title_and_checkbox(self):
        self.r.request=Mock(side_effect=[PAGE,PAGE])
        self.r.call({'action':'update','id':ID,'title':'修改标题'})
        self.assertIn('Name',self.r.request.call_args.args[2]['properties'])
        self.r.request=Mock(return_value={'id':ID,'type':'to_do','to_do':{'checked':True}})
        result=self.r.call({'action':'update','id':ID,'target_type':'block','block':{'to_do':{'checked':True}}})
        self.r.request.assert_called_once_with('PATCH','blocks/'+ID,{'to_do':{'checked':True}})
        self.assertTrue(result['checked']);self.assertEqual(result['status'],'saved')
    def test_empty_or_ambiguous_writes_do_not_touch_network(self):
        self.r.request=Mock()
        cases=[{'action':'create','parent_id':ID}, {'action':'append','id':ID,'text':' '},
               {'action':'append','id':ID,'text':'x','children':[]},
               {'action':'update','id':ID}, {'action':'update','id':ID,'target_type':'block','block':{}}]
        for args in cases:
            with self.subTest(args=args),self.assertRaises(ValueError):self.r.call(args)
        self.r.request.assert_not_called()
    def test_permission_failure_does_not_claim_saved_or_retry(self):
        self.r.opener=Mock();self.r.opener.open.side_effect=urllib.error.HTTPError('https://api.notion.com',403,'SECRET',{},io.BytesIO(b'SECRET'))
        with self.assertRaises(NotionAPIError) as error:
            self.r.call({'action':'create','parent_id':ID,'title':'验收'})
        self.assertEqual(error.exception.status,403);self.assertIn('Insert content',str(error.exception))
        self.r.opener.open.assert_called_once()
    def test_timeout_write_outcome_is_unknown_and_no_retry(self):
        self.r.opener=Mock();self.r.opener.open.side_effect=TimeoutError()
        with self.assertRaisesRegex(ValueError,'写入结果尚未确认'):
            self.r.call({'action':'append','id':ID,'text':'验收'})
        self.r.opener.open.assert_called_once()
    def test_large_successful_write_keeps_saved_receipt(self):
        self.fake(dict(PAGE,properties={'Name':{'type':'title','title':[{'plain_text':'x'*51000}]}}))
        result=self.r.call({'action':'create','parent_id':ID,'title':'验收'})
        self.assertEqual(result['status'],'saved');self.assertTrue(result['content_omitted'])
        self.assertEqual(result['id'],ID)
    def test_long_text_split_into_api_rich_text_chunks(self):
        self.fake(PAGE)
        self.r.call({'action':'create','parent_id':ID,'title':'验收','text':'鱼'*4500})
        children=json.loads(self.r.opener.open.call_args.args[0].data)['children']
        self.assertEqual([len(x['text']['content']) for x in children[0]['paragraph']['rich_text']],[2000,2000,500])
    def test_id_links_and_injection(self):
        self.assertEqual(identifier(ID.replace('-','')),ID)
        self.assertEqual(identifier('https://www.notion.so/notes-'+ID.replace('-','')+'?pvs=4'),ID)
        for value in (True,'../'+ID,'https://evil.test/'+ID,'https://notion.so.evil.test/'+ID,'https://u:p@notion.so/'+ID,'https://notion.so:444/'+ID,None):
            with self.subTest(value=value),self.assertRaises(ValueError):identifier(value)
    def test_pagination_is_explicit_no_automatic_crawl(self):
        self.fake({'results':[PAGE],'has_more':True,'next_cursor':ID})
        result=self.r.call({'action':'search','cursor':ID,'limit':1})
        self.assertEqual(result['next_cursor'],ID);self.assertTrue(result['has_more']);self.r.opener.open.assert_called_once()
        self.assertEqual(json.loads(self.r.opener.open.call_args.args[0].data)['start_cursor'],ID)
    def test_opaque_cursor_survives_query_search_and_children(self):
        cursor='s:73554ab1-ed02-4c82-a834-f4bf0bf92a12:'+ID+'+/='
        for action in ('query','search','children'):
            with self.subTest(action=action):
                self.fake({'results':[], 'has_more':True, 'next_cursor':cursor})
                args={'action':action,'cursor':cursor,'limit':2}
                if action!='search':args['id']=ID
                result=self.r.call(args)
                req=self.r.opener.open.call_args.args[0]
                sent=(json.loads(req.data)['start_cursor'] if req.data else
                      urllib.parse.parse_qs(urllib.parse.urlsplit(req.full_url).query)['start_cursor'][0])
                self.assertEqual(sent,cursor)
                self.assertEqual(result['next_cursor'],cursor)
    def test_cursor_must_be_response_string(self):
        self.r.request=Mock()
        with self.assertRaisesRegex(ValueError,'cursor'):
            self.r.call({'action':'query','id':ID,'cursor':{'next':'value'}})
        self.r.request.assert_not_called()
    def test_read_page_metadata_and_one_level_not_recursive(self):
        self.r.request=Mock(side_effect=[PAGE,{'results':[{'id':ID,'type':'toggle','has_children':True,'toggle':{'rich_text':[{'plain_text':'嵌套'}]}}],'has_more':True,'next_cursor':ID}])
        result=self.r.call({'action':'read','id':ID})
        self.assertEqual(result['title'],'学习笔记');self.assertEqual(len(self.r.request.call_args_list),2)
        self.assertTrue(result['content']['results'][0]['has_children']);self.assertEqual(result['content']['next_cursor'],ID)
    def test_table_cells_and_inline_links(self):
        self.assertEqual(block({'type':'table_row','table_row':{'cells':[[{'plain_text':'a'}],[{'plain_text':'b'}]]}})['cells'],['a','b'])
        p=block({'type':'paragraph','paragraph':{'rich_text':[{'plain_text':'x','href':'https://example.com'}]}})
        self.assertEqual(p['links'],['https://example.com'])
    def test_attachments_are_not_fetched_and_signed_urls_not_returned(self):
        for kind in ('image','audio','video','file','pdf'):
            result=block({'type':kind,kind:{'file':{'url':'SECRET_SIGNED_URL'},'caption':[{'plain_text':'图'}]}})
            self.assertTrue(result['attachment_not_read']);self.assertNotIn('SECRET',json.dumps(result))
    def test_data_source_and_database_reads(self):
        self.r.request=Mock(return_value={'object':'database','id':ID,'title':[],'data_sources':[{'id':ID,'name':'表'}]})
        result=self.r.call({'action':'database','id':ID});self.assertEqual(result['data_sources'][0]['id'],ID)
        self.assertTrue(result['untrusted_content']);self.r.request.assert_called_once_with('GET','databases/'+ID)
        self.r.request=Mock(return_value={'results':[PAGE],'has_more':False})
        self.r.call({'action':'query','id':ID,'limit':2});self.r.request.assert_called_once_with('POST','data_sources/'+ID+'/query',{'page_size':2})
    def test_query_filters_sorts_and_cursor_reach_api_unchanged(self):
        filters={'and':[{'property':'日期','date':{'on_or_after':'2025-01-01'}},
                        {'or':[{'property':'关键词','multi_select':{'contains':'实习'}},
                               {'property':'摘要（AI生成）','rich_text':{'contains':'面试'}}]}]}
        sorts=[{'property':'日期','direction':'ascending'},
               {'timestamp':'last_edited_time','direction':'descending'}]
        self.fake({'results':[PAGE],'has_more':True,'next_cursor':ID,
                   'request_status':{'type':'incomplete','incomplete_reason':'query_result_limit_reached'}})
        result=self.r.call({'action':'query','id':ID,'filter':filters,'sorts':sorts,'cursor':ID,'limit':3})
        request=self.r.opener.open.call_args.args[0]
        self.assertEqual(request.full_url,'https://api.notion.com/v1/data_sources/'+ID+'/query')
        self.assertEqual(json.loads(request.data),{'page_size':3,'start_cursor':ID,'filter':filters,'sorts':sorts})
        self.assertTrue(result['has_more']);self.assertEqual(result['next_cursor'],ID)
        self.assertEqual(result['request_status']['type'],'incomplete')
        self.r.opener.open.assert_called_once()
    def test_empty_query_options_remain_explicit(self):
        self.r.request=Mock(return_value={'results':[],'has_more':False})
        self.r.call({'action':'query','id':ID,'filter':{},'sorts':[]})
        self.r.request.assert_called_once_with('POST','data_sources/'+ID+'/query',
                                             {'page_size':20,'filter':{},'sorts':[]})
    def test_query_option_types_validate_before_network(self):
        self.r.request=Mock()
        for options in ({'filter':None},{'filter':[]},{'sorts':{}},{'sorts':[1]}):
            with self.subTest(options=options),self.assertRaises(ValueError):
                self.r.call(dict(action='query',id=ID,**options))
        self.r.request.assert_not_called()
    def test_limits_validate_before_network(self):
        self.r.request=Mock()
        for limit in (True,0,51,'20'):
            with self.assertRaises(ValueError):self.r.call({'action':'search','limit':limit})
        with self.assertRaises(ValueError):self.r.call({'action':'search','query':'x'*201})
        self.r.request.assert_not_called()
    def test_response_and_result_bounds_do_not_silently_truncate(self):
        self.r.opener=Mock();self.r.opener.open.return_value=io.BytesIO(b'x'*(MAX_RESPONSE+1))
        with self.assertRaisesRegex(ValueError,'响应过大'):self.r.call({'action':'search'})
        self.r.request=Mock(return_value={'results':[dict(PAGE,properties={'Name':{'type':'title','title':[{'plain_text':'x'*51000}]}})]})
        with self.assertRaisesRegex(ValueError,'内容过长'):self.r.call({'action':'search'})
    def test_http_errors_do_not_leak_body_or_token(self):
        for status,word in ((401,'令牌'),(403,'权限'),(404,'授权'),(429,'限流'),(500,'HTTP')):
            self.r.opener=Mock();self.r.opener.open.side_effect=urllib.error.HTTPError('https://api.notion.com',status,'SECRET',{},io.BytesIO(b'SECRET'))
            with self.assertRaises(ValueError) as err:self.r.call({'action':'search'})
            self.assertIn(word,str(err.exception));self.assertNotIn('SECRET',str(err.exception))
    def test_redirect_refused(self):
        self.assertIsNone(NoRedirect().redirect_request(None,None,302,'',{},'https://evil.test'))
    def test_missing_token(self):
        self.r.token_file.unlink()
        with self.assertRaisesRegex(ValueError,'尚未配置'):self.r.call({'action':'search'})

spec=importlib.util.spec_from_file_location('notion_base',Path(__file__).with_name('test-fatfish.py'))
base=importlib.util.module_from_spec(spec);spec.loader.exec_module(base)

class RuntimeTests(unittest.TestCase):
    setUp=base.PolicyTests.setUp
    bind_group=base.PolicyTests.bind_group
    def test_owner_private_can_read_and_record_source(self):
        self.r.notion.call=Mock(return_value={'source':'notion','results':[]})
        result=self.r.call('notion',{'action':'search','_context':self.context})
        self.assertGreater(result['record_id'],0);self.r.notion.call.assert_called_once_with({'action':'search'})
    def test_existing_context_access_stays_unchanged_for_writes(self):
        self.bind_group();self.r.notion.call=Mock(side_effect=lambda args: {'source':'notion','status':'saved','id':ID})
        for context in (dict(self.context,chat_id='-99'),dict(self.context,chat_id='456'),dict(self.context,session_key='heartbeat'),dict(self.context,channel='web')):
            with self.subTest(context=context):
                self.assertEqual(self.r.call('notion',{'action':'append','id':ID,'text':'验收','_context':context})['status'],'saved')
        self.assertEqual(self.r.notion.call.call_count,4)
    def test_schema_exposes_write_actions(self):
        item=next(x for x in base.runtime_policy.TOOLS if x['name']=='notion')
        self.assertFalse(item['annotations']['readOnlyHint'])
        for name in ('create','append','update'):
            self.assertIn(name,item['inputSchema']['properties']['action']['enum'])
    def test_schema_exposes_native_query_options(self):
        item=next(x for x in base.runtime_policy.TOOLS if x['name']=='notion')
        args=item['inputSchema']['properties']
        self.assertEqual(args['filter']['type'],'object')
        self.assertTrue(args['filter']['additionalProperties'])
        self.assertEqual(args['sorts']['type'],'array')
        self.assertEqual(args['sorts']['items']['type'],'object')
    def test_writes_record_as_operations_not_readings_or_replies(self):
        self.r.notion.call=Mock(return_value={'source':'notion','status':'saved','id':ID})
        result=self.r.call('notion',{'action':'create','parent_id':ID,'title':'验收','_context':self.context})
        with self.r.db() as db:
            row=db.execute('SELECT kind FROM events WHERE id=?',(result['record_id'],)).fetchone()
        self.assertEqual(row['kind'],'notion_write')

if __name__=='__main__':unittest.main()
