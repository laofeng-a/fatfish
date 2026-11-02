"""Owner cross-chat reads, actual turn states and group privacy. Offline only."""
import importlib.util,json,unittest,time
from pathlib import Path
from unittest.mock import Mock
spec=importlib.util.spec_from_file_location('conversation_base',Path(__file__).with_name('test-fatfish.py'))
base=importlib.util.module_from_spec(spec);spec.loader.exec_module(base)

class ConversationTests(unittest.TestCase):
    setUp=base.PolicyTests.setUp
    bind_group=base.PolicyTests.bind_group
    def ready(self):
        self.bind_group();self.bind_group('-98');self.r.settings['group_chat_mode']='members'
        self.r.inbound({'chat_id':'123','sender_id':'123','message_id':'1','text':'PRIVATE_FACT'})
        self.r.inbound({'chat_id':'-99','sender_id':'456','message_id':'318','text':'@fish @peer token来','mentioned':True})
        self.r.inbound({'chat_id':'-98','sender_id':'789','message_id':'12','text':'OTHER_GROUP_FACT'})
        self.group=dict(self.context,chat_id='-99',message_id='318')
    def call(self,name,args=None,context=None):return self.r.call(name,dict(args or {},_context=context or self.context))
    def test_private_can_search_registered_group_and_expand_real_source(self):
        self.ready();result=self.call('history_find',{'chat_id':'-99','query':'token来'})
        self.assertEqual(result['chat_id'],'-99');self.assertEqual(result['records'][0]['msg'],'318')
        record=self.call('history_expand',{'chat_id':'-99','id':result['records'][0]['id']})
        self.assertEqual(record['content'],'@fish @peer token来');self.assertEqual(record['chat'],'-99')
        self.assertNotIn('open_topics',result)
    def test_private_default_is_still_private(self):
        self.ready();result=self.call('history_find')
        self.assertEqual(result['chat_id'],'123');self.assertIn('PRIVATE_FACT',json.dumps(result));self.assertNotIn('OTHER_GROUP_FACT',json.dumps(result))
    def test_group_cannot_read_private_or_other_group_even_with_known_id(self):
        self.ready()
        for target in ('123','-98','456','-999'):
            for name,args in [('history_find',{'chat_id':target}),('history_expand',{'chat_id':target,'id':1}),('chat_status',{'chat_id':target})]:
                with self.subTest(name=name,target=target),self.assertRaises(ValueError):self.call(name,args,self.group)
        with self.r.db() as db:identifier=db.execute("SELECT id FROM events WHERE chat='123' AND msg='1'").fetchone()[0]
        with self.assertRaises(ValueError):self.call('history_expand',{'id':identifier},self.group)
    def test_owner_cannot_read_unregistered_group_or_stranger_private(self):
        self.ready()
        for target in ('456','-999',True,123,'../123'):
            with self.subTest(target=target),self.assertRaises(ValueError):self.call('history_find',{'chat_id':target})
    def test_background_cannot_query_another_conversation(self):
        self.ready()
        with self.assertRaises(ValueError):self.call('history_find',{'chat_id':'-99'},dict(self.context,session_key='heartbeat'))
    def test_user_filter_includes_peer_and_human_members(self):
        self.ready();self.r.inbound({'chat_id':'-99','sender_id':'888','sender_is_bot':True,'message_id':'319','text':'peer话'})
        result=self.call('history_find',{'chat_id':'-99','kind':'user'})
        self.assertEqual({r['msg'] for r in result['records']},{'318','319'})
    def test_time_window_is_applied_and_requires_timezone(self):
        self.ready()
        with self.r.db() as db:db.execute("UPDATE events SET created=100 WHERE chat='-99'")
        self.assertEqual(self.call('history_find',{'chat_id':'-99','after':'1970-01-01T00:01:40+00:00','before':'1970-01-01T00:01:41Z'})['records'][0]['msg'],'318')
        self.assertEqual(self.call('history_find',{'chat_id':'-99','after':'1970-01-01T00:01:41Z'})['records'],[])
        for args in ({'after':'2026-10-22T12:00:00'},{'after':True},{'after':'1970-01-01T00:02:00Z','before':'1970-01-01T00:01:00Z'}):
            with self.assertRaises(ValueError):self.call('history_find',dict(args,chat_id='-99'))
    def test_status_received_mentioned_no_reply_and_duplicate_observation(self):
        self.ready();self.assertTrue(self.r.action('turn_trace',{'chat_id':'-99','message_id':'318','state':'no_reply'})['allowed'])
        self.r.inbound({'chat_id':'-99','sender_id':'456','message_id':'318','text':'duplicate','mentioned':True})
        status=self.call('chat_status',{'chat_id':'-99','message_id':'318'})['message']
        self.assertTrue(status['recorded']);self.assertTrue(status['mentions_you']);self.assertEqual(status['processing'],'no_reply');self.assertEqual(status['delivery'],'intentionally_silent')
        self.r.action('turn_trace',{'chat_id':'-99','message_id':'318','state':'processing'})
        self.assertEqual(self.call('chat_status',{'chat_id':'-99','message_id':'318'})['message']['processing'],'no_reply')
    def test_status_errors_and_successful_send_use_real_receipts(self):
        self.ready();self.r.telegram=Mock(return_value={'message_id':800})
        self.call('send_message',{'text':'补回','reply_to_message_id':'318'},self.group)
        self.r.action('turn_trace',{'chat_id':'-99','message_id':'318','state':'completed'})
        status=self.call('chat_status',{'chat_id':'-99','message_id':'318'})['message']
        self.assertEqual(status['delivery'],'sent');self.assertEqual(status['sent_message_ids'],['800'])
        self.r.action('turn_trace',{'chat_id':'-98','message_id':'12','state':'error'})
        self.assertEqual(self.call('chat_status',{'chat_id':'-98','message_id':'12'})['message']['processing'],'error')
    def test_unknown_delivery_is_not_claimed_failed_or_successful(self):
        self.ready();self.r.telegram=Mock(side_effect=ValueError('发送结果不明'))
        with self.assertRaises(ValueError):self.call('send_message',{'text':'test'},self.group)
        self.assertEqual(self.call('chat_status',{'chat_id':'-99','message_id':'318'})['message']['delivery'],'unknown')
    def test_missing_and_legacy_receipts_preserve_uncertainty(self):
        self.ready();self.r.event('-99','member','older','5')
        old=self.call('chat_status',{'chat_id':'-99','message_id':'5'})['message']
        missing=self.call('chat_status',{'chat_id':'-99','message_id':'999'})['message']
        self.assertTrue(old['recorded']);self.assertIsNone(old['mentions_you']);self.assertEqual(old['processing'],'not_recorded')
        self.assertFalse(missing['recorded']);self.assertIn('不能',missing['note'])
    def test_group_status_has_no_private_capabilities_or_other_chat_records(self):
        self.ready();result=self.call('chat_status',{},self.group)
        self.assertNotIn('capabilities',result);self.assertNotIn('PRIVATE_FACT',json.dumps(result));self.assertNotIn('OTHER_GROUP_FACT',json.dumps(result))
    def test_message_metadata_and_table_are_bounded(self):
        self.ready()
        with self.r.db() as db:
            for i in range(2001):self.r.conversations.receive(db,{'chat_id':'-99','message_id':str(1000+i)})
            self.assertEqual(db.execute("SELECT COUNT(*) FROM conversation_receipts WHERE chat='-99'").fetchone()[0],2000)
    def test_diagnostic_states_are_fixed_and_never_store_error_secrets(self):
        self.ready();self.assertFalse(self.r.action('turn_trace',{'chat_id':'-99','message_id':'318','state':'secret error text'})['allowed'])
        self.assertFalse(self.r.action('turn_trace',{'chat_id':'-999','message_id':'318','state':'error'})['allowed'])
    def test_double_mention_context_explicitly_addresses_this_bot(self):
        self.ready();decision=self.r.inbound({'chat_id':'-99','sender_id':'456','message_id':'20','text':'@fish @peer 在不','mentioned':True})
        self.assertIn('"mentions_you": true',decision['context']);self.assertIn('同时@别人也包括你',decision['context'])

if __name__=='__main__':unittest.main()
