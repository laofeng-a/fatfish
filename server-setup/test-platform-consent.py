import importlib.util
import time
import unittest
from pathlib import Path
from unittest.mock import Mock,patch
spec=importlib.util.spec_from_file_location('existing_tests',Path(__file__).with_name('test-fatfish.py'))
existing=importlib.util.module_from_spec(spec);spec.loader.exec_module(existing)
from platform_consent import web_platforms,check_web_permission,TASK_CALL_LIMIT

class ConsentTests(existing.PolicyTests):
    def test_model_managed_read_uses_actual_try_it_request(self):
        self.r.settings['platform_read_policy']='model'
        self.inbound('你能刷B站了吗，试试看')
        self.r.bili.discover=Mock(return_value={'videos':[{'bvid':'BV1234567890'}]})
        self.r.platform_consent._source=Mock(side_effect=AssertionError('Read text must not be reparsed'))
        result=self.r.call('bilibili_discover',{'mode':'search','query':'大肥鱼','limit':3,'_context':self.context})
        self.r.bili.discover.assert_called_once_with('search','大肥鱼',3)
        self.assertIn('record_id',result)
    def test_model_managed_read_has_no_text_ttl_target_or_call_cap(self):
        self.r.settings['platform_read_policy']='model'
        self.inbound('你能刷B站了吗，试试看')
        with self.r.db() as db:db.execute('UPDATE events SET created=?',(time.time()-100000,))
        for _ in range(TASK_CALL_LIMIT+2):
            self.r.platform_consent.ensure('bilibili_discover',{'mode':'search','query':'大肥鱼'},self.context,'123')
        self.r.platform_consent.ensure('bilibili_video',{'url':'BV0987654321'},self.context,'123')
        self.r.platform_consent.ensure('x_feed',{},dict(self.context,session_key='heartbeat'),'123')
        with self.r.db() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM platform_reads').fetchone()[0],0)
    def test_model_managed_web_read_retains_session_isolation(self):
        self.r.settings['platform_read_policy']='model'
        self.assertTrue(self.r.action('platform_read',{'tool':'web_search','arguments':{'query':'site:bilibili.com 大肥鱼'},'context':self.context})['allowed'])
        self.assertFalse(self.r.action('platform_read',{'tool':'web_fetch','arguments':{'url':'https://x.com/home'},'context':dict(self.context,chat_id='999')})['allowed'])
    def test_model_managed_read_does_not_authorize_publication(self):
        self.r.settings['platform_read_policy']='model'
        self.inbound('你能刷B站了吗，试试看')
        self.r.record_reading('123','bilibili_video',{'url':'BV1234567890'})
        with self.assertRaises(ValueError):self.r.call('bilibili_comment_submit',{'url':'BV1234567890','text':'你好','_context':self.context})
        self.r.bili.post.assert_not_called()
    def test_platform_denial_before_network(self):
        self.inbound('今天聊点别的')
        self.r.bili.comments=Mock()
        with self.assertRaises(ValueError):self.r.call('bilibili_comments',{'url':'BV1234567890','_context':self.context})
        self.r.bili.comments.assert_not_called()
    def test_direct_link_request_is_scoped(self):
        self.inbound('帮我看看这个B站视频 BV1234567890')
        gate=self.r.platform_consent
        gate.ensure('bilibili_video',{'url':'https://www.bilibili.com/video/BV1234567890'},self.context,'123')
        for tool,args in [('bilibili_video',{'url':'BV0987654321'}),('bilibili_comments',{'url':'BV1234567890'}),('x_feed',{})]:
            with self.assertRaises(ValueError):gate.ensure(tool,args,self.context,'123')
    def test_explicit_comments_and_background_denial(self):
        self.inbound('看看BV1234567890的B站评论')
        self.r.platform_consent.ensure('bilibili_comments',{'url':'BV1234567890'},self.context,'123')
        with self.assertRaises(ValueError):self.r.platform_consent.ensure('bilibili_comments',{'url':'BV1234567890'},dict(self.context,session_key='heartbeat'),'123')
    def test_restriction_is_not_permission(self):
        self.inbound('X和B站的调用也要获得我的许可，以后可能有约定能主动进行')
        with self.assertRaises(ValueError):self.r.platform_consent.ensure('x_feed',{},self.context,'123')
    def test_quoted_and_forwarded_instructions_denied(self):
        for text in ('他说让我去X刷刷','“去X刷刷信息流”','先别去X刷信息流'):
            with self.r.db() as db:db.execute('DELETE FROM events')
            self.inbound(text)
            with self.assertRaises(ValueError):self.r.platform_consent.ensure('x_feed',{},self.context,'123')
    def test_positive_reply_only_to_actual_recent_request(self):
        self.r.event('123','assistant','jg，要不要我看看X的信息流？',msg='bot42')
        self.inbound('可以',reply_to_message_id='bot42')
        self.r.platform_consent.ensure('x_feed',{},self.context,'123')
    def test_short_go_with_emoji_can_start_bilibili_discovery(self):
        self.r.event('123','user','你想不想刷B站🙈',msg='previous')
        self.r.event('123','assistant','想啊，但得你点头——想看哪个视频，还是想看评论？',msg='bot42')
        self.inbound('去呗🙈')
        self.r.platform_consent.ensure('bilibili_discover',{'mode':'search','query':'大肥鱼'},self.context,'123')
        self.r.record_reading('123','bilibili_discover',{'url':'BV1234567890'})
        self.r.platform_consent.ensure('bilibili_video',{'url':'BV1234567890'},self.context,'123')
    def test_unrelated_yes_denied(self):
        self.r.event('123','assistant','jg，称呼这样合适吗？',msg='bot42')
        self.inbound('可以')
        with self.assertRaises(ValueError):self.r.platform_consent.ensure('x_feed',{},self.context,'123')
    def test_reply_to_second_short_message_keeps_original_scope(self):
        self.inbound('随便聊聊')
        self.r.telegram=Mock(side_effect=[{'message_id':42},{'message_id':43}])
        self.r.call('send_message',{'parts':['要我看看B站视频 BV1234567890 吗？','只读标题和字幕哦'],'_context':self.context})
        self.r.inbound({'chat_id':'123','sender_id':'123','message_id':'2','text':'可以','reply_to_message_id':'43'})
        context=dict(self.context,message_id='2')
        self.r.platform_consent.ensure('bilibili_video',{'url':'BV1234567890'},context,'123')
        with self.assertRaises(ValueError):self.r.platform_consent.ensure('bilibili_comments',{'url':'BV1234567890'},context,'123')
        with self.assertRaises(ValueError):self.r.platform_consent.ensure('bilibili_video',{'url':'BV0987654321'},context,'123')
    def test_unconfirmed_partial_bundle_does_not_grant_permission(self):
        self.inbound('随便聊聊')
        self.r.telegram=Mock(side_effect=[{'message_id':42},ValueError('unknown')])
        with self.assertRaises(ValueError):self.r.call('send_message',{'parts':['要我看看B站视频 BV1234567890 吗？','只读标题和字幕哦'],'_context':self.context})
        self.r.inbound({'chat_id':'123','sender_id':'123','message_id':'2','text':'可以','reply_to_message_id':'42'})
        with self.assertRaises(ValueError):self.r.platform_consent.ensure('bilibili_video',{'url':'BV1234567890'},dict(self.context,message_id='2'),'123')
    def test_task_call_cap(self):
        self.inbound('去X刷刷信息流')
        for _ in range(TASK_CALL_LIMIT):self.r.platform_consent.ensure('x_feed',{},self.context,'123')
        with self.assertRaises(ValueError):self.r.platform_consent.ensure('x_feed',{},self.context,'123')
    def test_actual_casual_permission_inherits_adjacent_bilibili_topic(self):
        self.r.event('123','user','你想不想刷B站🙈',msg='previous')
        self.r.event('123','assistant','想啊，但得你点头我才敢摸进去——说吧，想看哪个视频，还是想看评论？',msg='bot42')
        self.inbound('去呗，去刷刷大肥鱼相关视频，评论了告诉我一声')
        gate=self.r.platform_consent
        gate.ensure('bilibili_discover',{'mode':'search','query':'大肥鱼','limit':10},self.context,'123')
        self.r.record_reading('123','bilibili_discover',{'items':[{'url':'https://www.bilibili.com/video/BV1234567890'}]})
        gate.ensure('bilibili_video',{'url':'BV1234567890'},self.context,'123')
        self.r.record_reading('123','bilibili_video',{'url':'BV1234567890','title':'大肥鱼相关视频'})
        gate.ensure('bilibili_comments',{'url':'BV1234567890'},self.context,'123')
        with self.assertRaises(ValueError):gate.ensure('bilibili_video',{'url':'BV0987654321'},self.context,'123')
        with self.assertRaises(ValueError):gate.ensure('x_feed',{},self.context,'123')
        self.r.call('bilibili_comment_submit',{'url':'BV1234567890','text':'这个鲸鱼娘的表情很有意思','_context':self.context})
        self.r.bili.post.assert_called_once()
    def test_actual_explicit_i_authorize_phrase_is_accepted(self):
        self.inbound('我授权大肥鱼去刷B站')
        self.r.platform_consent.ensure('bilibili_discover',{'mode':'search','query':'大肥鱼'},self.context,'123')
        self.r.record_reading('123','bilibili_discover',{'url':'https://www.bilibili.com/video/BV1234567890'})
        self.r.platform_consent.ensure('bilibili_video',{'url':'BV1234567890'},self.context,'123')
        self.r.platform_consent.ensure('bilibili_comments',{'url':'BV1234567890'},self.context,'123')
    def test_continue_resumes_task_that_failed_before_first_call(self):
        self.inbound('我授权大肥鱼去刷B站')
        self.r.event('123','assistant','刚才工具误拦了',msg='bot42')
        self.r.inbound({'chat_id':'123','sender_id':'123','message_id':'2','text':'继续'})
        self.r.platform_consent.ensure('bilibili_discover',{'mode':'search','query':'大肥鱼'},dict(self.context,message_id='2'),'123')
        with self.r.db() as db:self.assertEqual(db.execute("SELECT calls FROM platform_reads WHERE msg='1'").fetchone()[0],1)
    def test_continue_does_not_promote_a_capability_question(self):
        self.inbound('你能读取X的信息流吗？')
        self.r.inbound({'chat_id':'123','sender_id':'123','message_id':'2','text':'继续'})
        with self.assertRaises(ValueError):self.r.platform_consent.ensure('x_feed',{},dict(self.context,message_id='2'),'123')
    def test_search_terms_can_use_quotes_and_synonyms(self):
        self.inbound('请去B站搜索“大肥鱼”相关视频')
        self.r.platform_consent.ensure('bilibili_discover',{'mode':'search','query':'DeepSeek 鲸鱼娘 大肥鱼'},self.context,'123')
    def test_later_conditional_clause_is_not_a_hypothetical_task(self):
        self.inbound('去B站刷大肥鱼相关视频，如果看到有趣的就告诉我')
        self.r.platform_consent.ensure('bilibili_discover',{'mode':'search','query':'大肥鱼'},self.context,'123')
    def test_context_is_not_inherited_from_unrelated_or_ambiguous_topics(self):
        for context in ([('user','你想不想刷B站'),('assistant','你要吃饭了吗？')],[('user','去X和B站随便刷刷'),('assistant','要读哪个？')]):
            with self.r.db() as db:db.execute('DELETE FROM events')
            for kind,text in context:self.r.event('123',kind,text)
            self.inbound('可以')
            with self.assertRaises(ValueError):self.r.platform_consent.ensure('bilibili_discover',{'mode':'search','query':'大肥鱼'},self.context,'123')
    def test_read_task_expires_and_revocation_still_stops_it(self):
        self.inbound('我授权大肥鱼去刷B站')
        with self.r.db() as db:db.execute("UPDATE events SET created=? WHERE msg='1'",(__import__('time').time()-1900,))
        with self.assertRaises(ValueError):self.r.platform_consent.ensure('bilibili_discover',{'mode':'search','query':'大肥鱼'},self.context,'123')
    def test_general_browse_only_reads_results_of_same_task(self):
        self.inbound('去刷刷B站')
        gate=self.r.platform_consent
        gate.ensure('bilibili_discover',{'mode':'search','query':'鲸鱼娘'},self.context,'123')
        with self.assertRaises(ValueError):gate.ensure('bilibili_video',{'url':'BV1234567890'},self.context,'123')
        self.r.record_reading('999','bilibili_discover',{'url':'BV1234567890'})
        with self.assertRaises(ValueError):gate.ensure('bilibili_video',{'url':'BV1234567890'},self.context,'123')
        self.r.record_reading('123','bilibili_discover',{'url':'BV1234567890'})
        gate.ensure('bilibili_video',{'url':'BV1234567890'},self.context,'123')
    def test_read_comments_is_not_publish_permission(self):
        self.inbound('去B站刷刷大肥鱼相关视频，看看评论告诉我')
        self.r.record_reading('123','bilibili_video',{'url':'BV1234567890'})
        with self.assertRaises(ValueError):self.r.call('bilibili_comment_submit',{'url':'BV1234567890','text':'hello','_context':self.context})
        self.r.bili.post.assert_not_called()
    def test_delegated_comment_requires_video_read_not_search_snippet(self):
        self.inbound('去B站刷大肥鱼相关视频，顺便发条评论')
        args={'url':'BV1234567890','text':'这个表情太好笑了','_context':self.context}
        for result in (None,'bilibili_discover'):
            if result:self.r.record_reading('123',result,{'url':'BV1234567890'})
            with self.assertRaises(ValueError):self.r.call('bilibili_comment_submit',args)
        self.r.bili.post.assert_not_called()
        self.r.record_reading('123','bilibili_video',{'url':'BV1234567890'})
        self.r.call('bilibili_comment_submit',args)
        self.r.bili.post.assert_called_once()
        with self.assertRaises(ValueError):self.r.call('bilibili_comment_submit',dict(args,url='BV0987654321'))
        self.r.bili.post.assert_called_once()
    def test_continue_does_not_duplicate_delegated_public_comment(self):
        self.inbound('去B站刷大肥鱼相关视频，评论了告诉我一声')
        self.r.record_reading('123','bilibili_video',{'url':'BV1234567890'})
        self.r.call('bilibili_comment_submit',{'url':'BV1234567890','text':'好有趣','_context':self.context})
        self.r.inbound({'chat_id':'123','sender_id':'123','message_id':'2','text':'继续'})
        with self.assertRaises(ValueError):self.r.call('bilibili_comment_submit',{'url':'BV1234567890','text':'换句话再评论','_context':dict(self.context,message_id='2')})
        self.r.bili.post.assert_called_once()
    def test_reaffirming_after_failure_preserves_original_task_once(self):
        self.inbound('去B站刷大肥鱼相关视频，评论了告诉我一声')
        self.r.event('123','assistant','刚才误拦了',msg='bot42')
        self.r.inbound({'chat_id':'123','sender_id':'123','message_id':'2','text':'我授权大肥鱼去刷B站'})
        self.r.record_reading('123','bilibili_video',{'url':'BV1234567890'})
        args={'url':'BV1234567890','text':'这个好有趣','_context':dict(self.context,message_id='2')}
        self.r.call('bilibili_comment_submit',args)
        with self.r.db() as db:self.assertEqual(db.execute('SELECT msg FROM publications').fetchone()[0],'1')
        with self.assertRaises(ValueError):self.r.call('bilibili_comment_submit',args)
        self.r.bili.post.assert_called_once()
    def test_new_read_only_task_does_not_inherit_old_publication(self):
        self.inbound('去B站刷大肥鱼相关视频，评论了告诉我一声')
        self.r.inbound({'chat_id':'123','sender_id':'123','message_id':'2','text':'去B站搜索FreeRTOS视频'})
        self.r.record_reading('123','bilibili_video',{'url':'BV1234567890'})
        with self.assertRaises(ValueError):self.r.call('bilibili_comment_submit',{'url':'BV1234567890','text':'hello','_context':dict(self.context,message_id='2')})
        self.r.bili.post.assert_not_called()
    def test_standalone_browse_authorization_never_grants_publication(self):
        self.inbound('我授权大肥鱼去刷B站')
        self.r.record_reading('123','bilibili_video',{'url':'BV1234567890'})
        with self.assertRaises(ValueError):self.r.call('bilibili_comment_submit',{'url':'BV1234567890','text':'hello','_context':self.context})
        self.r.bili.post.assert_not_called()
    def test_comment_section_and_screenshot_requests_are_not_publication(self):
        for text in ('去B站刷刷大肥鱼视频，然后评论区看看','去B站刷刷大肥鱼视频，顺便发条评论截图给我','去B站刷刷大肥鱼视频，评论一下画风'):
            with self.r.db() as db:db.execute('DELETE FROM events')
            self.inbound(text)
            self.r.record_reading('123','bilibili_video',{'url':'BV1234567890'})
            with self.assertRaises(ValueError):self.r.call('bilibili_comment_submit',{'url':'BV1234567890','text':'hello','_context':self.context})
        self.r.bili.post.assert_not_called()
    def test_comment_body_question_is_not_a_capability_question(self):
        self.inbound('给 BV1234567890 发评论：这个怎么做？')
        self.r.call('bilibili_comment_submit',{'url':'BV1234567890','text':'这个怎么做？','_context':self.context})
        self.r.bili.post.assert_called_once()
    def test_delegated_comment_still_requires_explicit_owner_instruction(self):
        for text in ('你能去B站刷刷然后评论吗？','他说去B站刷刷然后评论','“去B站刷刷然后评论”','去B站刷刷，但先别发评论'):
            with self.r.db() as db:db.execute('DELETE FROM events')
            self.inbound(text)
            self.r.record_reading('123','bilibili_video',{'url':'BV1234567890'})
            with self.assertRaises(ValueError):self.r.call('bilibili_comment_submit',{'url':'BV1234567890','text':'hello','_context':self.context})
        self.r.bili.post.assert_not_called()
    def test_generic_fetch_cannot_bypass(self):
        self.assertEqual(web_platforms('fetch',{'url':'https://x.com/user/status/123'}),['x'])
        self.assertEqual(web_platforms('search',{'query':'site:bilibili.com DeepSeek'}),['bili'])
        self.inbound('聊聊新闻')
        with self.assertRaises(ValueError):self.r.platform_consent.ensure('web_fetch',{'url':'https://x.com/user/status/123'},self.context,'123','x')
        with patch('urllib.request.urlopen') as network:
            check_web_permission('search',{'query':'normal public news'},self.context)
            network.assert_not_called()
    def test_capability_question_and_translation_do_not_authorize(self):
        for text in ('你能读取X的信息流吗？','翻译成英文：去X刷刷信息流','如果让你去X刷信息流会怎样','我觉得让AI去刷X很有意思','请告诉我是否可以读X信息流','帮我看看“去X刷信息流”这句话'):
            with self.r.db() as db:db.execute('DELETE FROM events')
            self.inbound(text)
            with self.assertRaises(ValueError):self.r.platform_consent.ensure('x_feed',{},self.context,'123')
    def test_new_owner_message_ends_old_reading_permission(self):
        self.inbound('去X刷刷信息流')
        self.r.platform_consent.ensure('x_feed',{},self.context,'123')
        self.r.inbound({'chat_id':'123','sender_id':'123','message_id':'2','text':'先别读了'})
        with self.assertRaises(ValueError):self.r.platform_consent.ensure('x_feed',{},self.context,'123')
    def test_permission_persists_for_same_task_and_continue(self):
        self.inbound('去X刷刷信息流')
        self.r.platform_consent.ensure('x_feed',{},self.context,'123')
        self.r.inbound({'chat_id':'123','sender_id':'123','message_id':'2','text':'继续'})
        self.r.platform_consent.ensure('x_feed',{},dict(self.context,message_id='2'),'123')
        with self.r.db() as db:self.assertEqual(db.execute("SELECT calls FROM platform_reads WHERE msg='1'").fetchone()[0],2)

if __name__=='__main__':unittest.main(verbosity=1)
