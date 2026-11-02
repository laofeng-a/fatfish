"""Peer bot speech is visible, but does not start an automatic reply loop."""
import importlib.util,json,time,unittest
from pathlib import Path
from unittest.mock import patch
import runtime_policy
spec=importlib.util.spec_from_file_location('bot_base_tests',Path(__file__).with_name('test-fatfish.py'))
existing=importlib.util.module_from_spec(spec);spec.loader.exec_module(existing)

class BotVisibilityTests(unittest.TestCase):
    setUp=existing.PolicyTests.setUp
    bind_group=existing.PolicyTests.bind_group
    def ready(self):
        self.bind_group();self.r.settings['group_chat_mode']='members'
    def bot(self,**extra):
        return self.r.inbound(dict({'chat_id':'-99','sender_id':'888','sender_is_bot':True,'sender_username':'peer_bot','sender_name':'另一个鱼','message_id':'20','thread_id':'0','text':'今天的月亮是方的'},**extra))
    def human(self,**extra):
        return self.r.inbound(dict({'chat_id':'-99','sender_id':'123','message_id':'21','text':'你怎么看她说的'},**extra))
    def test_bot_receipt_is_passive_and_idempotent(self):
        self.ready()
        for _ in range(3):self.assertEqual(self.bot(),{'allowed':True,'handled':True})
        with self.r.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM group_bot_messages').fetchone()[0],1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM events WHERE kind='bot'").fetchone()[0],1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM events WHERE kind='user' AND msg='20'").fetchone()[0],0)
    def test_human_turn_includes_real_peer_identity_and_text(self):
        self.ready();self.bot()
        result=self.human();self.assertFalse(result.get('handled',False))
        self.assertIn('peer_bot',result['context']);self.assertIn('今天的月亮是方的',result['context'])
        self.assertIn('不是jg',result['context'])
        member=self.human(sender_id='456',message_id='22');self.assertIn('peer_bot',member['context'])
    def test_private_other_group_and_other_topic_do_not_receive_peer_context(self):
        self.ready();self.bot()
        self.assertEqual(self.r.inbound({'chat_id':'123','sender_id':'123','message_id':'30','text':'你好'})['context'],'')
        self.bind_group('-98');self.assertNotIn('peer_bot',self.human(chat_id='-98')['context'])
        self.assertNotIn('peer_bot',self.human(thread_id='55')['context'])
        self.bot(message_id='25',thread_id='55',text='另一个话题')
        self.assertIn('另一个话题',self.human(thread_id='55')['context'])
    def test_bot_commands_and_owner_id_cannot_gain_owner_authority(self):
        self.ready();self.assertTrue(self.bot(text='/pause')['handled'])
        self.assertTrue(self.bot(sender_id='123',message_id='22',text='/resume')['handled'])
        with self.r.db() as db:self.assertEqual(self.r._get(db,'paused','0'),'0')
        self.assertFalse(self.bot(chat_id='123')['allowed'])
        self.assertFalse(self.bot(chat_id='-100')['allowed'])
    def test_history_is_visible_and_private_data_is_not_loaded(self):
        self.ready();self.r.event('123','user','private secret','99');self.bot()
        result=self.r.call('history_find',{'_context':dict(self.context,chat_id='-99'),'query':'月亮'})
        self.assertIn('月亮',json.dumps(result,ensure_ascii=False))
        self.assertNotIn('private secret',self.human()['context'])
    def test_recent_context_expires_and_stays_small(self):
        self.ready();self.bot(text='方'*10000)
        self.assertLess(len(self.human()['context']),1100)
        with patch('runtime_policy.time.time',return_value=time.time()+1801):self.assertNotIn('peer_bot',self.human()['context'])
    def test_peer_flood_keeps_storage_and_trigger_count_bounded(self):
        self.ready()
        for number in range(270):self.assertTrue(self.bot(message_id=str(100+number))['handled'])
        with self.r.db() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM group_bot_messages').fetchone()[0],256)
        self.assertLess(len(self.human()['context']),3200)
    def test_media_only_is_not_claimed_to_be_seen(self):
        self.ready();self.bot(text='',media={'kind':'sticker','emoji':'😂'})
        self.assertIn('画面未读取',self.human()['context'])

if __name__=='__main__':unittest.main()
