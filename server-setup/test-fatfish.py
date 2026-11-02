"""Offline authorization, quota, isolation and image-boundary regression tests."""
import base64
import datetime
import time
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch
import runtime_policy
from activity_budget import ActivityLedger

spec=importlib.util.spec_from_file_location('gateway',Path(__file__).with_name('budget-gateway.py'))
gateway=importlib.util.module_from_spec(spec);spec.loader.exec_module(gateway)

class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        directory=Path(self.tmp.name);(directory/'catalog.json').write_text('[]')
        spending_patch=patch.dict('os.environ',{'FATFISH_SPENDING_CONTROL':str(directory/'spending-control.json')})
        spending_patch.start();self.addCleanup(spending_patch.stop)
        config={'owner_id':'123','platform_read_policy':'strict','publication_policy':'strict','state_dir':str(directory),'catalog_file':str(directory/'catalog.json'),'bili_cookie_file':str(directory/'missing')}
        (directory/'settings.json').write_text(json.dumps(config));self.r=runtime_policy.Runtime(directory/'settings.json')
        self.context={'channel':'telegram','chat_id':'123','message_id':'1','session_key':'telegram:123'}
        self.r.bili.post=Mock(return_value={'status':'submitted'})
    def inbound(self,text,**extra):return self.r.inbound(dict(chat_id='123',sender_id='123',message_id='1',text=text,**extra))
    def publish(self,text,body='你好',**extra):
        self.inbound(text,**extra)
        return self.r.call('bilibili_comment_submit',{'url':'BV1234567890','text':body,'_context':self.context})
    def test_owner_only(self):self.assertFalse(self.r.inbound({'chat_id':'123','sender_id':'456','text':'/resume'})['allowed'])
    def bind_group(self,chat='-99'):
        self.r.inbound({'chat_id':chat,'sender_id':'123','message_id':'bind','text':'/bindgroup'})
    def test_registered_group_members_can_chat_without_management_access(self):
        self.bind_group();self.r.settings['group_chat_mode']='members'
        self.assertTrue(self.r.inbound({'chat_id':'-99','sender_id':'456','message_id':'10','text':'大家好'})['allowed'])
        for command in ('/pause','/resume','/bindgroup','/model','/switch','/reset','/forget secret'):
            self.assertFalse(self.r.inbound({'chat_id':'-99','sender_id':'456','message_id':'11','text':command})['allowed'])
        with self.r.db() as db:
            self.assertEqual(db.execute("SELECT kind FROM events WHERE chat='-99' AND msg='10'").fetchone()[0],'member')
            self.assertEqual(self.r._get(db,'paused','0'),'0')
        self.assertFalse(self.r.inbound({'chat_id':'-98','sender_id':'456','message_id':'10','text':'未登记群'})['allowed'])
        self.assertFalse(self.r.inbound({'chat_id':'456','sender_id':'456','message_id':'10','text':'陌生私聊'})['allowed'])
    def test_group_members_do_not_gain_private_memory_or_publication_access(self):
        self.bind_group();self.r.settings.update(group_chat_mode='members',publication_policy='model')
        self.r.inbound({'chat_id':'-99','sender_id':'456','message_id':'10','text':'你好'})
        context=dict(self.context,chat_id='-99',message_id='10')
        for name,args in [('memory_find',{'query':'secret'}),('telegram_chats',{}),('bilibili_comment_submit',{'url':'BV1234567890','text':'别人要求的评论'})]:
            with self.subTest(name=name),self.assertRaises(ValueError):self.r.call(name,dict(args,_context=context))
        self.r.bili.post.assert_not_called()
    def test_registered_group_members_can_receive_replies(self):
        self.bind_group();self.r.settings['group_chat_mode']='members'
        self.r.inbound({'chat_id':'-99','sender_id':'456','message_id':'10','text':'你好'})
        self.r.telegram=Mock(return_value={'message_id':21})
        self.r.call('send_message',{'text':'嗨','_context':dict(self.context,chat_id='-99',message_id='10')})
        self.r.telegram.assert_called_once_with('sendMessage',{'chat_id':'-99','text':'嗨','reply_parameters':{'message_id':10}})
    def test_private_chat_can_send_to_a_registered_group_without_message_id_collision(self):
        self.bind_group();self.r.telegram=Mock(side_effect=[{'message_id':21},{'message_id':22}])
        args={'chat_id':'-99','text':'来啦','_context':self.context}
        self.assertEqual(self.r.call('send_message',args)['chat_id'],'-99')
        self.assertEqual(self.r.call('send_message',args)['status'],'already_sent')
        self.r.call('send_message',{'text':'群内正常回复','_context':dict(self.context,chat_id='-99')})
        self.assertEqual(self.r.telegram.call_count,2)
    def test_cross_chat_target_validation_and_group_destination_isolation(self):
        self.bind_group();self.r.telegram=Mock()
        cases=[{'chat_id':'-98','text':'未登记群'},{'chat_id':'456','text':'陌生私聊'},{'chat_id':'-99','reaction':'👍'},{'chat_id':'123','text':'群成员不能向主人私聊发消息','_context':dict(self.context,chat_id='-99')},{'chat_id':'-99','text':'后台不发群','_context':dict(self.context,session_key='heartbeat')}]
        for args in cases:
            with self.subTest(args=args),self.assertRaises(ValueError):self.r.call('send_message',dict({'_context':self.context},**args))
        self.r.telegram.assert_not_called()
    def test_registered_chat_listing_uses_only_registered_targets(self):
        self.bind_group()
        with self.r.db() as db:self.r._set(db,'group_titles',json.dumps({'-99':'测试群'}))
        result=self.r.call('telegram_chats',{'_context':self.context})
        self.assertEqual([r['chat_id'] for r in result['chats']],['123','-99'])
        self.assertEqual(result['chats'][1]['title'],'测试群')
    def test_short_parts_send_in_order_and_record_once(self):
        self.r.telegram=Mock(side_effect=[{'message_id':11},{'message_id':12}])
        result=self.r.call('send_message',{'parts':['那群确实清净','但要你拉我才行哦'],'_context':self.context})
        self.assertEqual(result['message_ids'],['11','12'])
        self.assertEqual([call.args[1]['text'] for call in self.r.telegram.call_args_list],['那群确实清净','但要你拉我才行哦'])
        again=self.r.call('send_message',{'parts':['那群确实清净','但要你拉我才行哦'],'_context':self.context})
        self.assertEqual(again['status'],'already_sent')
        extra=self.r.call('send_message',{'parts':['不要追加这一条'],'_context':self.context})
        self.assertEqual(extra['status'],'already_sent')
        self.assertEqual(self.r.telegram.call_count,2)
        with self.r.db() as db:self.assertEqual(db.execute("SELECT COUNT(*) FROM events WHERE kind='assistant'").fetchone()[0],1)
    def test_reaction_only_completes_without_a_text_message(self):
        self.r.telegram=Mock(return_value=True)
        result=self.r.call('send_message',{'reaction':'❤️','_context':self.context})
        self.r.telegram.assert_called_once_with('setMessageReaction',{'chat_id':'123','message_id':1,'reaction':[{'type':'emoji','emoji':'❤'}]})
        self.assertEqual(result['status'],'sent');self.assertEqual(result['message_ids'],[])
        self.assertEqual(result['reaction'],{'message_id':'1','emoji':'❤'})
        self.assertEqual(self.r.call('send_message',{'reaction':'❤️','_context':self.context})['status'],'already_sent')
        self.assertEqual(self.r.telegram.call_count,1)
        with self.r.db() as db:
            row=db.execute('SELECT kind,content,status FROM sends').fetchone()
            self.assertEqual(row['kind'],'reaction');self.assertEqual(row['status'],'sent')
            self.assertIn('消息反应 #1：❤',row['content'])
    def test_reaction_and_text_are_sent_once_in_the_same_batch(self):
        self.r.telegram=Mock(side_effect=[True,{'message_id':81},{'message_id':82}])
        result=self.r.call('send_message',{'reaction':'🤣','reaction_message_id':'42','text':'笑死\n\n谁教你的','_context':self.context})
        self.assertEqual([call.args[0] for call in self.r.telegram.call_args_list],['setMessageReaction','sendMessage','sendMessage'])
        self.assertEqual(self.r.telegram.call_args_list[0].args[1]['message_id'],42)
        self.assertEqual(result['message_ids'],['81','82'])
    def test_reaction_can_be_removed_without_sending_text(self):
        self.r.telegram=Mock(return_value=True)
        self.r.call('send_message',{'reaction':'','reaction_message_id':'42','_context':self.context})
        self.r.telegram.assert_called_once_with('setMessageReaction',{'chat_id':'123','message_id':42,'reaction':[]})
    def test_reaction_can_accompany_a_selected_sticker(self):
        self.r.catalog=[{'id':'laugh','name':'笑死','kind':'sticker','file_id':'cached'}]
        self.r.telegram=Mock(side_effect=[True,{'message_id':83,'sticker':{'file_id':'cached'}}])
        result=self.r.call('emote_send',{'id':'laugh','reaction':'🤣','_context':self.context})
        self.assertEqual([call.args[0] for call in self.r.telegram.call_args_list],['setMessageReaction','sendSticker'])
        self.assertEqual(result['emote'],'laugh')
    def test_reaction_invalid_target_and_missing_context_do_not_send(self):
        self.r.telegram=Mock()
        cases=[{'reaction':'👍','reaction_message_id':'-1'},{'reaction':'👍','reaction_message_id':'1/other'},{'reaction':True},{'reaction':' '},{'reaction_message_id':'42','text':'hi'},{'reaction':'👍','_context':dict(self.context,message_id='')}]
        for arguments in cases:
            with self.subTest(arguments=arguments),self.assertRaises(ValueError):
                self.r.call('send_message',dict({'_context':self.context},**arguments))
        self.r.telegram.assert_not_called()
    def test_reaction_failure_stops_text_and_is_not_repeated(self):
        self.r.telegram=Mock(side_effect=ValueError('Telegram结果不明'))
        arguments={'reaction':'👍','text':'收到','_context':self.context}
        with self.assertRaises(ValueError):self.r.call('send_message',arguments)
        self.assertEqual(self.r.call('send_message',arguments)['status'],'not_repeated')
        self.r.telegram.assert_called_once()
    def test_unconfirmed_reaction_response_is_not_recorded_as_success(self):
        self.r.telegram=Mock(return_value=False)
        with self.assertRaises(ValueError):self.r.call('send_message',{'reaction':'👍','_context':self.context})
        with self.r.db() as db:self.assertEqual(db.execute('SELECT status FROM sends').fetchone()[0],'unknown')
    def test_short_parts_are_bounded_and_background_stays_quiet(self):
        self.r.telegram=Mock()
        for args in ({'parts':['a']*4},{'parts':['a'*81]},{'parts':['']},{'parts':['hi'],'text':'duplicate'},{'parts':['a','b'],'_context':dict(self.context,session_key='heartbeat')}):
            with self.subTest(args=args),self.assertRaises(ValueError):self.r.call('send_message',dict({'_context':self.context},**args))
        self.r.telegram.assert_not_called()
    def test_paragraph_text_sends_as_two_messages_with_one_receipt(self):
        self.r.telegram=Mock(side_effect=[{'message_id':61},{'message_id':62}])
        body='图里也没一条叫大肥鱼啊，那我可不敢认领这份战绩。\n\n截图真假和赛制我还没验证，暂时不认这个成绩。'
        result=self.r.call('send_message',{'text':body,'_context':self.context})
        self.assertEqual(result['message_ids'],['61','62'])
        self.assertEqual([call.args[1]['text'] for call in self.r.telegram.call_args_list],body.split('\n\n'))
        with self.r.db() as db:
            row=db.execute('SELECT content,message_ids,status FROM sends').fetchone()
            self.assertEqual(row['content'],body);self.assertEqual(json.loads(row['message_ids']),['61','62']);self.assertEqual(row['status'],'sent')
        self.assertEqual(self.r.call('send_message',{'text':body,'_context':self.context})['status'],'already_sent')
        self.assertEqual(self.r.telegram.call_count,2)
    def test_paragraph_text_before_emote_is_not_kept_in_one_caption(self):
        self.r.catalog=[{'id':'test','name':'开心','kind':'photo','mime':'image/jpeg','path':'unused','file_id':'cached'}]
        self.r.telegram=Mock(side_effect=[{'message_id':61},{'message_id':62},{'message_id':63,'photo':[{'file_id':'cached'}]}])
        self.r.call('emote_send',{'id':'test','text':'是我呀\n\n谁给我换的衣服','_context':self.context})
        self.assertEqual([call.args[0] for call in self.r.telegram.call_args_list],['sendMessage','sendMessage','sendPhoto'])
        self.assertEqual(self.r.telegram.call_args_list[-1].args[1]['caption'],'')
    def test_emote_deduplicates_via_receipt_without_extra_turn_guard(self):
        self.r.catalog=[{'id':'test','name':'开心','kind':'sticker','path':'unused','file_id':'cached'}]
        self.r.telegram=Mock(return_value={'message_id':61,'sticker':{'file_id':'cached'}})
        with self.r.db() as db:db.execute('INSERT INTO emote_turns VALUES(?,?,?)',('123','1',time.time()))
        result=self.r.call('emote_send',{'id':'test','_context':self.context})
        self.assertEqual(result['status'],'sent')
        again=self.r.call('emote_send',{'id':'test','_context':self.context})
        self.assertEqual(again['status'],'already_sent');self.r.telegram.assert_called_once()
    def test_direct_emote_query_selects_and_sends_without_lookup(self):
        self.r.catalog=[{'id':'first','name':'嘿嘿','kind':'sticker','file_id':'one'}, {'id':'laugh','name':'笑死','tags':['开心'],'kind':'sticker','file_id':'two'}]
        self.r.telegram=Mock(return_value={'message_id':71,'sticker':{'file_id':'two'}})
        result=self.r.call('emote_send',{'query':'笑死','_context':self.context})
        self.assertEqual(result['emote'],'laugh')
        self.r.telegram.assert_called_once_with('sendSticker',{'chat_id':'123','sticker':'two'})
    def test_direct_emote_query_does_not_send_unrelated_first_item(self):
        self.r.catalog=[{'id':'first','name':'嘿嘿','kind':'sticker','file_id':'one'}];self.r.telegram=Mock()
        with self.assertRaises(ValueError):self.r.call('emote_send',{'query':'完全不存在的含义','_context':self.context})
        self.r.telegram.assert_not_called()
    def test_emote_catalog_is_available_without_guessing_query(self):
        self.r.catalog=[{'id':str(i),'name':'表情'+str(i),'tags':['test']} for i in range(16)]
        result=self.r.call('emote_find',{'_context':self.context})
        self.assertEqual(result['total'],16);self.assertEqual(len(result['emotes']),16)
        self.assertEqual(set(result['emotes'][0]),{'id','name'})
    def test_emote_candidates_include_recent_names_without_filtering(self):
        self.r.catalog=[{'id':'test','name':'笑死','kind':'sticker','file_id':'cached'}]
        self.r.telegram=Mock(return_value={'message_id':61,'sticker':{'file_id':'cached'}})
        self.r.call('emote_send',{'id':'test','_context':self.context})
        result=self.r.call('emote_find',{'query':'笑死','_context':self.context})
        self.assertEqual(result['recently_sent'],['笑死']);self.assertEqual(result['emotes'][0]['id'],'test')
    def test_model_managed_publication_accepts_model_target_and_body(self):
        self.r.settings['publication_policy']='model';self.inbound('现在再试试')
        result=self.r.call('bilibili_comment_submit',{'url':'BV0987654321','text':'模型根据视频写的感想','_context':self.context})
        self.assertEqual(result['status'],'submitted');self.r.bili.post.assert_called_once_with('BV0987654321','模型根据视频写的感想',None,None)
    def test_model_managed_publication_allows_multiple_distinct_comments(self):
        self.r.settings['publication_policy']='model'
        for url,body in [('BV1234567890','第一条'),('BV0987654321','第二条'),('BV1234567890','另一条')]:
            self.r.call('bilibili_comment_submit',{'url':url,'text':body,'_context':self.context})
        self.assertEqual(self.r.bili.post.call_count,3)
    def test_model_managed_publication_reuses_confirmed_receipt(self):
        self.r.settings['publication_policy']='model'
        args={'url':'BV1234567890','text':'你好','_context':self.context}
        self.r.call('bilibili_comment_submit',args)
        again=self.r.call('bilibili_comment_submit',args)
        self.assertTrue(again['already_submitted']);self.r.bili.post.assert_called_once()
    def test_model_managed_publication_does_not_repeat_unknown_send(self):
        self.r.settings['publication_policy']='model';self.r.bili.post=Mock(side_effect=ValueError('network unknown'))
        args={'url':'BV1234567890','text':'你好','_context':self.context}
        with self.assertRaises(ValueError):self.r.call('bilibili_comment_submit',args)
        self.assertEqual(self.r.call('bilibili_comment_submit',args)['status'],'not_repeated')
        self.r.bili.post.assert_called_once()
    def test_model_managed_publication_replies_and_background_have_no_text_gate(self):
        self.r.settings['publication_policy']='model'
        self.r.call('bilibili_comment_submit',{'url':'BV1234567890','text':'回复内容','root':'42','parent':'43','_context':dict(self.context,session_key='heartbeat')})
        self.r.bili.post.assert_called_once_with('BV1234567890','回复内容','42','43')
    def test_model_managed_publication_retains_session_isolation(self):
        self.r.settings['publication_policy']='model'
        for chat in ('999','-99'):
            with self.assertRaises(ValueError):self.r.call('bilibili_comment_submit',{'url':'BV1234567890','text':'你好','_context':dict(self.context,chat_id=chat)})
        self.r.bili.post.assert_not_called()
    def test_model_managed_publication_works_in_owner_registered_group(self):
        self.r.settings['publication_policy']='model'
        self.r.inbound({'chat_id':'-99','sender_id':'123','message_id':'bind','text':'/bindgroup'})
        self.r.inbound({'chat_id':'-99','sender_id':'123','message_id':'1','text':'在群里交代一条评论'})
        self.r.call('bilibili_comment_submit',{'url':'BV1234567890','text':'群里交代的评论','_context':dict(self.context,chat_id='-99')})
        self.r.bili.post.assert_called_once()
    def test_paragraph_split_preserves_code_and_single_newlines(self):
        body='说明\r\n\r\n```c\r\nint a;\r\n\r\nint b;\r\n```\r\n\r\n结束\r\n下一行'
        self.assertEqual(runtime_policy.split_chat_paragraphs(body),['说明','```c\nint a;\n\nint b;\n```','结束\n下一行'])
        self.assertEqual(runtime_policy.split_chat_paragraphs('  \n\n嗯\n\n\n'),['嗯'])
    def test_background_paragraphs_remain_one_message(self):
        self.r.telegram=Mock(return_value={'message_id':61});self.r.outbound=Mock(return_value={'allowed':True,'ticket':'background-test'})
        self.r.complete=Mock()
        self.r.call('send_message',{'text':'第一段\n\n第二段','_context':dict(self.context,session_key='heartbeat')})
        self.r.telegram.assert_called_once();self.assertEqual(self.r.telegram.call_args.args[1]['text'],'第一段\n\n第二段')
    def test_short_parts_then_emote_and_partial_failure(self):
        self.r.catalog=[{'id':'test','name':'开心','kind':'photo','mime':'image/jpeg','path':'unused','file_id':'cached'}]
        self.r.telegram=Mock(side_effect=[{'message_id':21},{'message_id':22},{'message_id':23,'photo':[{'file_id':'cached'}]}])
        result=self.r.call('emote_send',{'id':'test','parts':['那群确实清净','等你拉我哦'],'_context':self.context})
        self.assertEqual(result['message_ids'],['21','22','23'])
        self.assertEqual([call.args[0] for call in self.r.telegram.call_args_list],['sendMessage','sendMessage','sendPhoto'])
        self.assertEqual(self.r.telegram.call_args_list[-1].args[1]['caption'],'')
        self.r.telegram=Mock(side_effect=[{'message_id':31},ValueError('network unknown')])
        self.context['message_id']='2'
        with self.assertRaises(ValueError):self.r.call('send_message',{'parts':['first','second'],'_context':self.context})
        with self.r.db() as db:
            row=db.execute('SELECT status,message_ids FROM sends ORDER BY rowid DESC LIMIT 1').fetchone()
            self.assertEqual(row['status'],'unknown');self.assertEqual(json.loads(row['message_ids']),['31'])
        retry=self.r.call('send_message',{'parts':['first','second'],'_context':self.context})
        self.assertEqual(retry['status'],'not_repeated');self.assertEqual(self.r.telegram.call_count,2)
    def test_publish_requires_current_original_instruction(self):
        for text in ('看看 BV1234567890','能不能给 BV1234567890 发评论？','给 BV1234567890 先别发评论','他说给 BV1234567890 发评论：你好','“给 BV1234567890 发评论：你好”'):
            with self.subTest(text=text):
                with self.r.db() as db:db.execute('DELETE FROM events')
                with self.assertRaises(ValueError):self.publish(text)
        self.r.bili.post.assert_not_called()
    def test_forward_never_authorizes(self):
        with self.assertRaises(ValueError):self.publish('给 BV1234567890 发评论：你好',forwarded=True)
        self.r.bili.post.assert_not_called()
    def test_post_once_and_exact_body(self):
        self.publish('给 BV1234567890 发评论：你好')
        with self.assertRaises(ValueError):self.r.call('bilibili_comment_submit',{'url':'BV1234567890','text':'你好','_context':self.context})
        self.assertEqual(self.r.bili.post.call_count,1)
    def test_delegated_wording(self):self.publish('给 BV1234567890 留条评论，内容你写','新的措辞')
    def test_no_undelegated_changes(self):
        with self.assertRaises(ValueError):self.publish('给 BV1234567890 发评论：你好','改写了')
        self.r.bili.post.assert_not_called()
    def test_background_never_posts(self):
        self.context['session_key']='heartbeat'
        with self.assertRaises(ValueError):self.publish('给 BV1234567890 发评论：你好')
    def test_target_mismatch(self):
        self.r.bili.info=Mock(side_effect=[{'bvid':'BV1234567890'},{'bvid':'BV0987654321'}])
        self.inbound('给 BV1234567890 发评论：你好')
        with self.assertRaises(ValueError):self.r.call('bilibili_comment_submit',{'url':'BV0987654321','text':'你好','_context':self.context})
        self.r.bili.post.assert_not_called()
    def test_private_history_isolated(self):
        identifier=self.r.event('123','user','私人原文')
        self.r.inbound({'chat_id':'-99','sender_id':'123','text':'/bindgroup'})
        context=dict(self.context,chat_id='-99')
        with self.assertRaises(ValueError):self.r.call('history_expand',{'id':identifier,'_context':context})
        with self.assertRaises(ValueError):self.r.call('memory_find',{'query':'','_context':context})
        self.assertFalse(self.r.outbound({'chat_id':'999','content':'不可外发'})['allowed'])
    def test_recent_history_does_not_require_a_guessed_keyword(self):
        self.r.event('123','user','刚看到一个有趣的比喻',msg='1')
        latest=self.r.event('123','assistant','什么比喻？',msg='2')
        result=self.r.call('history_find',{'limit':1,'_context':self.context})
        self.assertEqual(result['view'],'recent')
        self.assertEqual([row['id'] for row in result['records']],[latest])
        self.assertEqual(result['records'][0]['snippet'],'什么比喻？')
        found=self.r.call('history_find',{'query':'比喻','kind':'user','_context':self.context})
        self.assertEqual(len(found['records']),1)
        self.assertNotIn('open_topics',found)
    def test_recent_context_is_bounded_and_keeps_only_active_topics_and_sent_shares(self):
        source=self.r.event('123','user','鲸'*1200)
        topic=self.r.call('memory_save',{'category':'topic','content':'待聊的小插曲','quote':'鲸','source_id':source,'_context':self.context})
        self.r.call('memory_save',{'category':'topic','content':'已过期的话题','quote':'鲸','source_id':source,'_context':self.context})
        with self.r.db() as db:
            db.execute('UPDATE memories SET expires=? WHERE id!=?',(time.time()-1,topic['id']))
            for status in ('sent','unknown','reserved'):
                db.execute('INSERT INTO sends(ticket,chat,kind,mode,status,content,created) VALUES(?,?,?,?,?,?,?)',(status,'123','text','background',status,status,time.time()))
        result=self.r.call('history_find',{'_context':self.context})
        self.assertEqual(len(result['records'][0]['snippet']),400)
        self.assertEqual([row['content'] for row in result['open_topics']],['待聊的小插曲'])
        self.assertEqual([row['content'] for row in result['recent_proactive_shares']],['sent'])
        for args in ({'query':None},{'limit':True},{'limit':21},{'kind':'made_up'}):
            with self.subTest(args=args),self.assertRaises(ValueError):self.r.call('history_find',dict(args,_context=self.context))
    def test_group_recent_context_has_no_private_topics_or_shares(self):
        self.bind_group()
        self.r.event('123','user','私聊内容')
        self.r.event('-99','member','群内内容')
        result=self.r.call('history_find',{'_context':dict(self.context,chat_id='-99')})
        self.assertEqual([row['snippet'] for row in result['records']],['群内内容'])
        self.assertNotIn('open_topics',result)
        self.assertNotIn('recent_proactive_shares',result)
    def test_memory_evidence_and_forget(self):
        identifier=self.r.event('123','user','我喜欢自然交流')
        args={'category':'core','content':'喜欢自然交流','quote':'我喜欢自然交流','source_id':identifier,'_context':self.context}
        self.r.call('memory_save',args)
        self.assertIn('自然交流',(self.r.directory/'core.md').read_text(encoding='utf-8'))
        self.r.inbound({'chat_id':'123','sender_id':'123','message_id':'2','text':'/forget 自然交流'})
        self.assertNotIn('自然交流',(self.r.directory/'core.md').read_text(encoding='utf-8'))
        with self.assertRaises(ValueError):self.r.call('memory_save',dict(args,quote='不存在的原话'))
    def test_dedup_and_pause(self):
        request={'chat_id':'123','content':'一条消息'}
        self.assertTrue(self.r.outbound(request)['allowed']);self.assertTrue(self.r.outbound(request)['skip'])
        self.inbound('/pause');self.assertFalse(self.r.action('background_allowed',{})['allowed'])
    def test_same_short_reply_in_distinct_turns_is_not_suppressed(self):
        self.r.telegram=Mock(side_effect=[{'message_id':51},{'message_id':52}])
        self.r.call('send_message',{'text':'嗯','_context':self.context})
        self.r.call('send_message',{'text':'嗯','_context':dict(self.context,message_id='2')})
        self.assertEqual(self.r.telegram.call_count,2)
        self.assertEqual(self.r.call('send_message',{'text':'再补一段','_context':dict(self.context,message_id='2')})['status'],'already_sent')
    def test_native_channel_deduplicates_by_owner_message_not_reply_text(self):
        first=self.r.outbound({'chat_id':'123','message_id':'1','content':'嗯'})
        self.r.complete({'ticket':first['ticket'],'status':'sent','message_ids':['51']})
        self.assertTrue(self.r.outbound({'chat_id':'123','message_id':'1','content':'不重复'})['skip'])
        self.assertFalse(self.r.outbound({'chat_id':'123','message_id':'2','content':'嗯'}).get('skip',False))
    def test_active_emotes_in_two_owner_turns_have_no_global_cooldown(self):
        self.r.catalog=[{'id':'test','name':'开心','kind':'photo','mime':'image/jpeg','path':'unused','file_id':'cached'}]
        self.r.telegram=Mock(return_value={'message_id':51,'photo':[{'file_id':'cached'}]})
        self.r.call('emote_send',{'id':'test','_context':self.context})
        self.r.call('emote_send',{'id':'test','_context':dict(self.context,message_id='2')})
        self.assertEqual(self.r.telegram.call_count,2)
    def test_sub_budget_reservation(self):
        number=self.r.activity.reserve(1_900_000,'test')
        with self.assertRaises(ValueError):self.r.activity.reserve(100_001,'test')
        self.r.activity.settle(number,0);self.assertEqual(self.r.activity.used(),0)
    def test_image_chat_scope(self):
        path='/var/lib/picoclaw/.picoclaw/workspace/media/tmp/picoclaw_media/tg-test.jpg'
        self.r.action('image_register',{'chat_id':'123','path':path})
        self.r.inbound({'chat_id':'-99','sender_id':'123','text':'/bindgroup'})
        self.assertTrue(self.r.action('image_allowed',{'chat_id':'123','path':path})['allowed'])
        self.assertFalse(self.r.action('image_allowed',{'chat_id':'-99','path':path})['allowed'])
    def test_quiet_hours_skip_background(self):
        quiet=datetime.datetime(2026,10,22,2,0,tzinfo=runtime_policy.UTC8)
        with patch.object(runtime_policy.dt,'datetime') as clock:
            clock.now.return_value=quiet
            self.assertTrue(self.r.wake({})['skip'])
        self.assertTrue(self.r.outbound({'chat_id':'123','content':'本人主动聊天照常'})['allowed'])
    def test_daily_contact_cap_and_spacing(self):
        noon=datetime.datetime(2026,10,22,12,0,tzinfo=runtime_policy.UTC8)
        with self.r.db() as db:
            for number in range(3):db.execute('INSERT INTO wakes VALUES(?,?,?,1,1)',('old'+str(number),time.time()-20000-number*20000,'2026-10-22'))
            db.execute('INSERT INTO wakes VALUES(?,?,?,0,0)',('current',time.time(),'2026-10-22'))
            self.r._set(db,'current_wake','current')
        with patch.object(runtime_policy.dt,'datetime') as clock:
            clock.now.return_value=noon
            self.assertFalse(self.r.outbound({'chat_id':'123','content':'第四次','mode':'background'})['allowed'])
            with self.r.db() as db:
                db.execute("DELETE FROM wakes WHERE id IN ('old1','old2')")
                db.execute("UPDATE wakes SET created=? WHERE id='old0'",(time.time()-60,))
            self.assertFalse(self.r.outbound({'chat_id':'123','content':'间隔太近','mode':'background'})['allowed'])
    def test_unanswered_pauses_next_opportunity(self):
        with self.r.db() as db:self.r._set(db,'unanswered',2)
        self.assertTrue(self.r.wake({})['skip'])
        self.inbound('我回来了')
        with self.r.db() as db:self.assertEqual(self.r._get(db,'unanswered'),'0')
    def test_background_opportunity_can_send_and_record_a_real_tool_receipt(self):
        noon=datetime.datetime(2026,10,22,12,0,tzinfo=runtime_policy.UTC8)
        self.r.telegram=Mock(return_value={'message_id':71})
        context=dict(self.context,session_key='heartbeat',message_id='')
        with patch.object(runtime_policy.dt,'datetime') as clock:
            clock.now.return_value=noon
            self.assertNotIn('skip',self.r.wake({}))
            result=self.r.call('send_message',{'text':'刚想起那个梗','_context':context})
            self.assertEqual(result['status'],'sent')
            self.r.telegram.assert_called_once_with('sendMessage',{'chat_id':'123','text':'刚想起那个梗'})
            recent=self.r.call('history_find',{'_context':context})
            self.assertEqual(recent['recent_proactive_shares'][0]['message_ids'],'["71"]')
            self.r.action('wake_done',{})
            self.assertFalse(self.r.action('background_allowed',{})['allowed'])
        with self.r.db() as db:
            self.assertEqual(db.execute('SELECT contacted,finished FROM wakes').fetchone()[:],(1,1))
            self.assertEqual(self.r._get(db,'unanswered'),'1')

class GatewayTests(unittest.TestCase):
    def test_bilibili_share_text_retains_part_and_time(self):
        from bilibili_client import BiliClient
        client=BiliClient('missing')
        self.assertEqual(client.normalize_url('这个视频 https://www.bilibili.com/video/BV1234567890?p=2&t=36&share_source=abc 看看'),'https://www.bilibili.com/video/BV1234567890?p=2&t=36')
    def request(self,content):return {'model':gateway.MODEL,'messages':[{'role':'user','content':content}],'max_tokens':8192}
    def image(self,raw=b'\xff\xd8\xffhello'):return {'type':'image_url','image_url':{'url':'data:image/jpeg;base64,'+base64.b64encode(raw).decode()}}
    def test_images_not_counted_as_text(self):
        _,small,_,_=gateway.prepare_request(self.request([self.image()]))
        _,large,_,_=gateway.prepare_request(self.request([self.image(b'\xff\xd8\xff'+b'x'*1_000_000)]))
        self.assertEqual(small,large)
    def test_external_url_and_many_images_denied(self):
        for content in ([{'type':'image_url','image_url':{'url':'https://example.com/a.jpg'}}],[self.image()]*5):
            with self.assertRaises(gateway.Denied):gateway.prepare_request(self.request(content))
    def test_output_limit(self):
        data=self.request('hi');data['max_tokens']=8193
        with self.assertRaises(gateway.Denied):gateway.prepare_request(data)
    def test_no_native_search(self):
        data=self.request('hi');data['enable_search']=True
        raw,_,_,_=gateway.prepare_request(data);self.assertFalse(json.loads(raw)['enable_search'])

if __name__=='__main__':unittest.main(verbosity=2)
