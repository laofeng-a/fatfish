"""Group reply targets, quiet turns and transport payloads. No real delivery."""
import importlib.util,json,unittest
from pathlib import Path
from unittest.mock import Mock,patch
import runtime_policy
spec=importlib.util.spec_from_file_location('group_reply_base',Path(__file__).with_name('test-fatfish.py'))
existing=importlib.util.module_from_spec(spec);spec.loader.exec_module(existing)

class GroupReplyTests(unittest.TestCase):
    setUp=existing.PolicyTests.setUp
    bind_group=existing.PolicyTests.bind_group
    def ready(self):
        self.bind_group();self.r.settings['group_chat_mode']='members'
        self.r.inbound({'chat_id':'-99','sender_id':'456','sender_name':'Joe','message_id':'10','text':'有点意思','mentioned':True,'reply_to_self':False})
        self.group=dict(self.context,chat_id='-99',message_id='10')
        self.r.telegram=Mock(side_effect=[{'message_id':21},{'message_id':22},{'message_id':23}])
    def test_default_reply_targets_current_member_and_only_first_bubble(self):
        self.ready();result=self.r.call('send_message',{'parts':['这句有画面了','哈哈'],'_context':self.group})
        calls=self.r.telegram.call_args_list
        self.assertEqual(calls[0].args,('sendMessage',{'chat_id':'-99','text':'这句有画面了','reply_parameters':{'message_id':10}}))
        self.assertNotIn('reply_parameters',calls[1].args[1]);self.assertEqual(result['reply_to_message_id'],'10')
    def test_explicit_reply_can_target_earlier_other_member(self):
        self.ready();self.r.inbound({'chat_id':'-99','sender_id':'789','message_id':'9','text':'之前那个梗'})
        self.r.call('send_message',{'text':'接你的这句','reply_to_message_id':'9','_context':self.group})
        self.assertEqual(self.r.telegram.call_args.args[1]['reply_parameters'],{'message_id':9})
    def test_reply_to_peer_bot_uses_observed_message_id(self):
        self.ready();self.r.inbound({'chat_id':'-99','sender_id':'888','sender_is_bot':True,'message_id':'8','text':'我才是正牌'})
        self.r.call('send_message',{'text':'先比比谁更肥','reply_to_message_id':'8','_context':self.group})
        self.assertEqual(self.r.telegram.call_args.args[1]['reply_parameters'],{'message_id':8})
    def test_group_emote_replies_once_for_text_plus_file_or_file_alone(self):
        for with_text in (True,False):
            with self.subTest(with_text=with_text):
                self.ready();self.r.catalog=[{'id':'test_sticker','name':'偷笑','kind':'sticker','file_id':'known_file'}]
                context=dict(self.group,message_id='10' if with_text else '11')
                args={'id':'test_sticker','_context':context}
                if with_text:args['text']='哈哈'
                self.r.call('emote_send',args)
                calls=self.r.telegram.call_args_list
                self.assertIn('reply_parameters',calls[0].args[1])
                if with_text:self.assertNotIn('reply_parameters',calls[1].args[1])
    def test_private_and_cross_chat_do_not_implicitly_reply_to_source_message(self):
        self.ready();self.r.call('send_message',{'text':'私聊仍自然','_context':self.context})
        self.assertNotIn('reply_parameters',self.r.telegram.call_args.args[1])
        self.r.call('send_message',{'text':'去群里说一句','chat_id':'-99','_context':dict(self.context,message_id='2')})
        self.assertNotIn('reply_parameters',self.r.telegram.call_args.args[1])
    def test_unknown_cross_chat_and_invalid_message_ids_are_not_sent(self):
        self.ready();self.r.event('-98','member','别的群','7')
        for target in ('999','7','-1','abc',True):
            with self.subTest(target=target),self.assertRaises(ValueError):
                self.r.call('send_message',{'text':'不乱引用','reply_to_message_id':target,'_context':self.group})
        self.r.telegram.assert_not_called()
    def test_reaction_only_does_not_create_a_reply_bubble(self):
        self.ready();self.r.telegram=Mock(return_value=True)
        self.r.call('send_message',{'reaction':'🤣','_context':self.group})
        self.r.telegram.assert_called_once_with('setMessageReaction',{'chat_id':'-99','message_id':10,'reaction':[{'type':'emoji','emoji':'🤣'}]})
    def test_quiet_turn_reserves_nothing_and_creates_no_send(self):
        self.ready();before=None
        with self.r.db() as db:before=db.execute('SELECT COUNT(*) FROM sends').fetchone()[0]
        for sentinel in ('NO_REPLY','[NO_REPLY]'):
            self.assertEqual(self.r.outbound({'chat_id':'-99','content':sentinel}),{'allowed':True,'skip':True})
        with self.r.db() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM sends').fetchone()[0],before)
        self.r.telegram.assert_not_called()
    def test_group_turn_exposes_real_sender_and_attention_without_polluting_private(self):
        self.ready();self.r.settings['telegram_bot_username']='self_bot';result=self.r.inbound({'chat_id':'-99','sender_id':'456','sender_name':'Joe','message_id':'12','text':'@someone 在吗','mentioned':False,'reply_to_self':False})
        self.assertIn('"sender_id": "456"',result['context']);self.assertIn('"mentions_you": false',result['context'])
        self.assertIn('"your_username": "self_bot"',result['context'])
        self.assertIn('NO_REPLY',result['context']);self.assertNotIn('peer_bot',result['context'])
        self.assertEqual(self.r.inbound({'chat_id':'123','sender_id':'123','message_id':'50','text':'你好'})['context'],'')
    def test_upload_encodes_reply_parameters_as_json_in_multipart(self):
        token=self.r.directory/'telegram.token';token.write_text('local-test-token');self.r.settings['telegram_token_file']=str(token)
        file=self.r.directory/'emote.webp';file.write_bytes(b'fake-image')
        response=Mock();response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False)
        response.read.return_value=b'{"ok":true,"result":{"message_id":21}}'
        opener=Mock();opener.open.return_value=response
        with patch('runtime_policy.urllib.request.build_opener',return_value=opener):
            self.r.telegram('sendPhoto',{'chat_id':'-99','reply_parameters':{'message_id':10}},('photo',file,'image/webp'))
        raw=opener.open.call_args.args[0].data
        self.assertIn(b'{"message_id": 10}',raw);self.assertNotIn(b"{'message_id': 10}",raw)

if __name__=='__main__':unittest.main()
