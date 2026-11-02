"""Behavioral regressions; all Telegram delivery is mocked."""
import importlib.util,json,time,unittest
from pathlib import Path
from unittest.mock import Mock
import runtime_policy
spec=importlib.util.spec_from_file_location('chat_base_tests',Path(__file__).with_name('test-fatfish.py'))
existing=importlib.util.module_from_spec(spec);spec.loader.exec_module(existing)

class ChatFeatureTests(unittest.TestCase):
    setUp=existing.PolicyTests.setUp
    bind_group=existing.PolicyTests.bind_group
    def receive(self,message='10',kind='sticker',**extra):
        data={'chat_id':'123','sender_id':'123','message_id':message,'text':'','media':{'kind':kind,'file_id':'file_'+message,'unique_id':'unique_'+message,'emoji':'😂'}}
        data.update(extra);return self.r.inbound(data)
    def save(self,**extra):return self.r.call('emote_save',dict({'name':'偷笑','tags':['嘿嘿','幸灾乐祸'],'_context':self.context},**extra))
    def remind(self,**extra):return self.r.call('reminder',dict({'action':'create','text':'收衣服','delay_seconds':60,'_context':self.context},**extra))
    def test_collect_reply_and_send_without_downloading_image(self):
        self.receive();self.r.inbound({'chat_id':'123','sender_id':'123','message_id':'1','text':'收下这个','reply_to_message_id':'10'})
        saved=self.save();self.assertEqual(saved['source_message_id'],'10')
        self.r.telegram=Mock(return_value={'message_id':51})
        self.r.call('emote_send',{'id':saved['id'],'_context':self.context})
        self.r.telegram.assert_called_once_with('sendSticker',{'chat_id':'123','sticker':'file_10'})
    def test_collect_requires_observed_media_and_persists_after_restart(self):
        with self.assertRaises(ValueError):self.save(message_id='500')
        self.receive();saved=self.save(message_id='10')
        r=runtime_policy.Runtime(self.r.directory/'settings.json')
        self.assertEqual(r.call('emote_find',{'_context':self.context})['emotes'][0]['id'],saved['id'])
        self.assertEqual(self.save(message_id='10')['id'],saved['id'])
        self.assertEqual(len(r.emotes.catalog('123')),1)
    def test_correct_tags_disable_and_private_group_visibility(self):
        self.receive();saved=self.save(message_id='10');self.bind_group()
        self.assertEqual(self.r.call('emote_find',{'_context':dict(self.context,chat_id='-99')})['total'],0)
        self.r.call('emote_update',{'id':saved['id'],'tags':['阴阳怪气'],'share_in_groups':True,'_context':self.context})
        result=self.r.call('emote_find',{'query':'阴阳怪气','_context':dict(self.context,chat_id='-99')})
        self.assertEqual(result['emotes'][0]['id'],saved['id'])
        self.r.call('emote_update',{'id':saved['id'],'active':False,'_context':self.context})
        self.assertEqual(self.r.emotes.catalog('123'),[])
    def test_group_cannot_manage_private_library_or_view_private_inbox(self):
        self.receive();self.bind_group();ctx=dict(self.context,chat_id='-99')
        for name,args in [('emote_save',{'name':'不行'}),('emote_update',{'id':'fake','active':False})]:
            with self.subTest(name=name),self.assertRaises(ValueError):self.r.call(name,dict(args,_context=ctx))
        self.assertEqual(self.r.call('emote_find',{'inbox':True,'_context':ctx})['received_media'],[])
    def test_collect_photo_and_animation_reuse_telegram_files(self):
        for number,kind,method,field in [('10','image','sendPhoto','photo'),('11','animation','sendAnimation','animation')]:
            self.receive(number,kind);context=dict(self.context,message_id=number)
            saved=self.r.call('emote_save',{'name':kind,'message_id':number,'_context':context})
            self.r.telegram=Mock(return_value={'message_id':number+'0'})
            self.r.call('emote_send',{'id':saved['id'],'_context':context})
            self.r.telegram.assert_called_once_with(method,{'chat_id':'123','caption':'',field:'file_'+number})
    def test_reply_image_can_be_collected_even_if_original_not_previously_seen(self):
        self.receive('1',media=None,text='收下这个',reply_to_message_id='8',reply_media={'kind':'image','file_id':'reply_file','unique_id':'reply_unique'})
        self.assertEqual(self.save()['source_message_id'],'8')
    def test_reminder_survives_restart_and_delivers_once(self):
        now=time.time();created=self.remind();again=self.remind()
        self.assertEqual(created['id'],again['id']);self.assertTrue(again['already_created'])
        r=runtime_policy.Runtime(self.r.directory/'settings.json');r.reminders.recover()
        r.telegram=Mock(return_value={'message_id':91})
        self.assertTrue(r.reminders.tick(now+62));self.assertFalse(r.reminders.tick(now+65))
        r.telegram.assert_called_once_with('sendMessage',{'chat_id':'123','text':'⏰ 收衣服'})
        with r.db() as db:self.assertEqual(db.execute('SELECT status,last_message_id FROM reminders').fetchone()[:],('sent','91'))
    def test_modify_cancel_and_two_different_times(self):
        a=self.remind();b=self.remind(delay_seconds=120);self.assertNotEqual(a['id'],b['id'])
        changed=self.r.call('reminder',{'action':'update','id':a['id'],'text':'带伞','delay_seconds':180,'_context':self.context})
        self.assertEqual(changed['text'],'带伞')
        self.r.call('reminder',{'action':'cancel','id':b['id'],'_context':self.context})
        rows=self.r.call('reminder',{'action':'list','_context':self.context})['reminders']
        self.assertEqual([row['id'] for row in rows],[a['id']])
    def test_uncertain_delivery_is_not_blindly_retried(self):
        self.remind();self.r.telegram=Mock(side_effect=TimeoutError())
        self.assertTrue(self.r.reminders.tick(time.time()+70));self.assertFalse(self.r.reminders.tick(time.time()+80))
        rows=self.r.call('reminder',{'action':'list','_context':self.context})['reminders']
        self.assertEqual(rows[0]['status'],'uncertain');self.assertEqual(self.r.telegram.call_count,1)
    def test_interrupted_sending_recovers_as_uncertain_not_duplicate(self):
        self.remind()
        with self.r.db() as db:db.execute("UPDATE reminders SET status='sending'")
        self.r.reminders.recover();self.r.telegram=Mock()
        self.assertFalse(self.r.reminders.tick(time.time()+100));self.r.telegram.assert_not_called()
    def test_explicit_rate_limit_response_retries_only_after_its_deadline(self):
        self.remind();now=time.time()+70
        self.r.telegram=Mock(side_effect=runtime_policy.TelegramRejected({'error_code':429,'parameters':{'retry_after':20}}))
        self.r.reminders.tick(now);self.assertFalse(self.r.reminders.tick(now+10))
        self.r.telegram=Mock(return_value={'message_id':92})
        self.assertTrue(self.r.reminders.tick(now+21));self.assertEqual(self.r.telegram.call_count,1)
    def test_overdue_daily_reminder_sends_one_catchup_and_schedules_future(self):
        self.remind(repeat='daily');now=time.time()+3*86400
        self.r.telegram=Mock(return_value={'message_id':93})
        self.r.reminders.tick(now);self.assertIn('补提醒',self.r.telegram.call_args.args[1]['text'])
        self.assertFalse(self.r.reminders.tick(now+1))
    def test_reminders_are_explicit_requests_independent_of_autonomous_pause(self):
        self.remind()
        with self.r.db() as db:self.r._set(db,'paused',1);self.r._set(db,'unanswered',2)
        self.r.telegram=Mock(return_value={'message_id':94});self.assertTrue(self.r.reminders.tick(time.time()+70))
    def test_reminder_target_identity_and_time_validation(self):
        self.bind_group()
        for args in ({'chat_id':'999'},{'_context':dict(self.context,chat_id='-99')},{'_context':dict(self.context,session_key='heartbeat')},{'delay_seconds':True}):
            with self.subTest(args=args),self.assertRaises(ValueError):self.remind(**args)
        with self.assertRaises(ValueError):self.r.call('reminder',{'action':'create','text':'无时区','due_at':'2030-01-01T09:00:00','_context':self.context})

if __name__=='__main__':unittest.main()
