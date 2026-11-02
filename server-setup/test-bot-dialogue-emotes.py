"""Peer conversations terminate; peer stickers can actually enter the library."""
import importlib.util,time,unittest
from pathlib import Path
from unittest.mock import Mock,patch
import runtime_policy
spec=importlib.util.spec_from_file_location('peer_emote_base',Path(__file__).with_name('test-fatfish.py'))
existing=importlib.util.module_from_spec(spec);spec.loader.exec_module(existing)

class PeerDialogueEmoteTests(unittest.TestCase):
    setUp=existing.PolicyTests.setUp
    bind_group=existing.PolicyTests.bind_group
    def ready(self):
        self.bind_group();self.r.settings.update(group_chat_mode='members',group_bot_chat=True)
        self.human()
    def human(self,msg='10',chat='-99'):
        return self.r.inbound({'chat_id':chat,'sender_id':'123','message_id':msg,'text':'大家聊两句'})
    def peer(self,msg='20',**extra):
        data={'chat_id':'-99','sender_id':'888','sender_is_bot':True,'sender_username':'peer_bot','message_id':msg,'text':'鲸鱼尾巴卡住了，谁帮个忙？','delivery_phase':'observe'}
        data.update(extra);return self.r.inbound(data)
    def ctx(self,msg='10',chat='-99'):return dict(self.context,chat_id=chat,message_id=msg)
    def media(self,unique='u',file='f'):return {'kind':'sticker','file_id':file,'unique_id':unique,'emoji':'😂'}
    def test_observation_delivery_and_replay_are_idempotent(self):
        self.ready()
        for _ in range(2):self.assertFalse(self.peer().get('handled',False))
        self.assertFalse(self.peer(delivery_phase='deliver').get('handled',False))
        self.assertTrue(self.peer()['handled']);self.assertTrue(self.peer(delivery_phase='deliver')['handled'])
        with self.r.db() as db:self.assertEqual(db.execute("SELECT COUNT(*) FROM bot_dialogue_receipts WHERE status='delivered'").fetchone()[0],1)
    def test_no_more_than_two_peer_opportunities_until_next_human(self):
        self.ready();now=time.time()
        for offset,msg in [(0,'20'),(6,'21')]:
            with patch('group_bot_dialogue.time.time',return_value=now+offset):self.assertFalse(self.peer(msg,delivery_phase='deliver').get('handled',False))
        with patch('group_bot_dialogue.time.time',return_value=now+12):self.assertTrue(self.peer('22')['handled'])
        with patch('group_bot_dialogue.time.time',return_value=now+18):
            self.human('11');self.assertFalse(self.peer('23').get('handled',False))
    def test_disabling_peer_mode_revokes_pending_delivery(self):
        self.ready();self.peer();self.r.settings['group_bot_chat']=False
        self.assertTrue(self.peer(delivery_phase='deliver')['handled'])
        self.r.settings['group_bot_chat']=True;self.assertTrue(self.peer()['handled'])
    def test_short_burst_media_and_commands_do_not_start_new_turns(self):
        self.ready();self.peer()
        self.assertTrue(self.peer('21')['handled'])
        self.assertTrue(self.peer('22',text='',media=self.media())['handled'])
        self.assertTrue(self.peer('23',text='/pause')['handled'])
        self.assertEqual(self.r.emotes.inbox('-99')[0]['message_id'],'22')
    def test_idle_or_unregistered_bot_messages_remain_passive(self):
        self.ready();now=time.time()
        with patch('group_bot_dialogue.time.time',return_value=now+1801):self.assertTrue(self.peer()['handled'])
        self.assertFalse(self.peer('21',chat_id='-100')['allowed'])
        self.assertFalse(self.peer('22',chat_id='123')['allowed'])
    def test_old_record_and_stale_pending_message_are_not_reprocessed(self):
        self.ready();self.r.settings['group_bot_chat']=False;self.peer();self.r.settings['group_bot_chat']=True
        self.assertTrue(self.peer()['handled'])
        now=time.time()
        with patch('group_bot_dialogue.time.time',return_value=now+6):self.assertFalse(self.peer('21').get('handled',False))
        with patch('group_bot_dialogue.time.time',return_value=now+130):self.assertTrue(self.peer('21',delivery_phase='deliver')['handled'])
    def test_bot_text_turn_is_labeled_as_bot_and_does_not_grant_owner_tools(self):
        self.ready();result=self.peer();self.assertIn('"sender_is_bot": true',result['context']);self.assertIn('不是jg',result['context'])
        for name,args in [('memory_find',{'query':'secret'}),('reminder',{'action':'list'}),('bilibili_comment_submit',{'url':'BV1234567890','text':'未经主人交代的评论'})]:
            with self.subTest(name=name),self.assertRaises(ValueError):self.r.call(name,dict(args,_context=self.ctx('20')))
    def test_peer_sticker_can_be_collected_in_group_then_resent_by_file_id(self):
        self.ready();self.peer(text='',media=self.media());self.r.telegram=Mock(return_value={'message_id':50})
        saved=self.r.call('emote_save',{'name':'偷笑','tags':['嘿嘿'],'message_id':'20','_context':self.ctx()})
        self.r.call('emote_send',{'id':saved['id'],'_context':self.ctx()})
        self.r.telegram.assert_called_once_with('sendSticker',{'chat_id':'-99','sticker':'f','reply_parameters':{'message_id':10}})
        self.assertEqual(saved['source_chat_id'],'-99')
    def test_group_inbox_does_not_disclose_private_or_other_group_media(self):
        self.ready();self.r.inbound({'chat_id':'123','sender_id':'123','message_id':'30','media':self.media('private')})
        self.bind_group('-98');self.r.inbound({'chat_id':'-98','sender_id':'123','message_id':'31','media':self.media('other')})
        self.peer(text='',media=self.media('public'))
        inbox=self.r.call('emote_find',{'inbox':True,'_context':self.ctx()})['received_media']
        self.assertEqual([m['message_id'] for m in inbox],['20'])
        for source in ('123','-98'):
            with self.assertRaises(ValueError):self.r.call('emote_find',{'inbox':True,'source_chat_id':source,'_context':self.ctx()})
            with self.assertRaises(ValueError):self.r.call('emote_save',{'name':'不能跨群拿','source_chat_id':source,'message_id':'30','_context':self.ctx()})
    def test_owner_private_can_find_and_collect_a_registered_group_sticker(self):
        self.ready();self.peer(text='',media=self.media())
        inbox=self.r.call('emote_find',{'inbox':True,'_context':self.context})['received_media']
        self.assertEqual(inbox[0]['source_chat_id'],'-99')
        saved=self.r.call('emote_save',{'name':'群里那张','message_id':'20','source_chat_id':'-99','_context':self.context})
        self.assertEqual(self.r.emotes.catalog('-99')[0]['id'],saved['id'])
    def test_same_image_private_labels_do_not_leak_into_group(self):
        self.ready();self.r.inbound({'chat_id':'123','sender_id':'123','message_id':'30','media':self.media()})
        private=self.r.call('emote_save',{'name':'private secret label','tags':['private secret tag'],'message_id':'30','_context':self.context})
        self.peer(text='',media=self.media())
        shared=self.r.call('emote_save',{'name':'群里的偷笑','tags':['好笑'],'message_id':'20','_context':self.ctx()})
        self.assertEqual(private['id'],shared['id'])
        group=self.r.emotes.catalog('-99')[0];self.assertEqual(group['name'],'群里的偷笑');self.assertEqual(group['tags'],['好笑'])
        self.assertEqual(self.r.emotes.catalog('123')[0]['name'],'private secret label')
    def test_group_collection_does_not_globally_share_or_mutate_private_library(self):
        self.ready();self.peer(text='',media=self.media())
        with self.assertRaises(ValueError):self.r.call('emote_save',{'name':'不能跨群广播','message_id':'20','share_in_groups':True,'_context':self.ctx()})
        saved=self.r.call('emote_save',{'name':'本群贴纸','message_id':'20','_context':self.ctx()})
        self.bind_group('-98');self.assertEqual(self.r.emotes.catalog('-98'),[])
        with self.assertRaises(ValueError):self.r.call('emote_update',{'id':saved['id'],'active':False,'_context':self.ctx()})
    def test_private_correction_of_group_origin_updates_visible_group_tags(self):
        self.ready();self.peer(text='',media=self.media())
        saved=self.r.call('emote_save',{'name':'笑','message_id':'20','_context':self.ctx()})
        self.r.call('emote_update',{'id':saved['id'],'tags':['阴阳怪气'],'_context':self.context})
        self.assertEqual(self.r.emotes.catalog('-99')[0]['tags'],['阴阳怪气'])

if __name__=='__main__':unittest.main()
