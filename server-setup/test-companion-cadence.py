"""Integration checks for one shared, receipt-driven two-minute chat deadline."""
import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from unittest.mock import Mock

from runtime_policy import Runtime


def timestamp(clock):
    return dt.datetime.fromisoformat('2026-11-06T'+clock+'+08:00').timestamp()


class CompanionCadenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        root=Path(self.tmp.name);(root/'catalog.json').write_text('[]',encoding='utf-8')
        self.config=root/'runtime.json'
        self.settings={'owner_id':'123','state_dir':str(root),'catalog_file':str(root/'catalog.json'),
                       'companion_cadence_enabled':True,'explicit_internal_note_filter_enabled':True,
                       'autonomy_interval_seconds':3600}
        self.config.write_text(json.dumps(self.settings),encoding='utf-8')
        self.now=timestamp('18:00:00')
        clock=patch('time.time',side_effect=lambda:self.now);clock.start();self.addCleanup(clock.stop)
        self.r=Runtime(self.config)
        self.ctx={'channel':'telegram','chat_id':'123','sender_id':'123','session_key':'telegram:123'}
        self.sequence=0

    def deadline(self):
        with self.r.db()as db:return json.loads(self.r._get(db,'next_chat_due','null'))

    def inbound(self,message='1',text='刚才那个选择还有意思'):
        self.r.inbound(dict(self.ctx,message_id=message,text=text))
        return dict(self.ctx,message_id=message)

    def reserve(self,context,text='那我再补一句',kind='text',origin='auto_final',**extra):
        return self.r.outbound(dict(context,content=text,kind=kind,delivery_origin=origin,**extra))

    def complete(self,ticket,status='sent',ids=None):
        if ids is None:
            self.sequence+=1;ids=[str(100+self.sequence)]
        return self.r.complete({'ticket':ticket,'status':status,'message_ids':ids})

    def replied(self,message='1',text='刚才那个选择还有意思'):
        context=self.inbound(message,text)
        reserved=self.reserve(context)
        self.assertTrue(reserved.get('ticket'),reserved)
        self.complete(reserved['ticket'])
        self.r.conversations.trace({'chat_id':'123','message_id':message,'state':'completed'})
        return context

    def claim(self,number=1):
        wake=format(number,'024x')
        with self.r.db()as db:decision=self.r.companion.claim(db,wake,self.now)
        self.assertIsNotNone(decision)
        self.assertEqual(decision['kind'],'chat')
        self.assertTrue(decision['session_key'].startswith('agent:autonomy-'))
        return decision,{'channel':'telegram','chat_id':'123','sender_id':'heartbeat',
                         'session_key':decision['session_key'],'message_id':'heartbeat:'+wake}

    def test_only_confirmed_real_reply_arms_two_minutes(self):
        context=self.inbound()
        self.assertIsNone(self.deadline())
        reserved=self.reserve(context)
        self.assertIsNone(self.deadline(),'Intent to send is not a delivery receipt')
        self.complete(reserved['ticket'])
        self.assertEqual(self.deadline(),self.now+120)

    def test_new_message_replaces_one_plan_after_the_new_reply(self):
        self.replied();self.now+=30
        context=self.inbound('2','对，还有一个想法')
        self.assertIsNone(self.deadline(),'Old idle plan must not run during the new human turn')
        self.now+=8;reserved=self.reserve(context,text='接住新的一句')
        self.complete(reserved['ticket'])
        self.assertEqual(self.deadline(),self.now+120)

    def test_failed_unknown_and_no_message_id_do_not_arm(self):
        for i,(status,ids)in enumerate([('rejected',[]),('unknown',[]),('sent',[])],1):
            with self.subTest(status=status,ids=ids):
                context=self.inbound(str(i));reserved=self.reserve(context,text='尝试'+str(i))
                self.complete(reserved['ticket'],status,ids)
                self.assertIsNone(self.deadline())

    def test_forwarded_owner_content_refreshes_chat_without_control_authority(self):
        self.replied();self.now+=10
        context=dict(self.ctx,message_id='2',text='关闭自主活动',forwarded=True)
        self.r.inbound(context)
        self.assertIsNone(self.deadline())
        with self.r.db()as db:self.assertEqual(db.execute('SELECT enabled FROM loop_autonomy').fetchone()[0],1)
        ticket=self.reserve(context,text='接着你转来的内容')['ticket'];self.complete(ticket)
        self.assertEqual(self.deadline(),self.now+120)
        self.now=self.deadline();decision,_=self.claim()
        self.assertIn('关闭自主活动',decision['context'])
        self.assertIn('"kind": "forwarded"',decision['context'])

    def test_media_delivery_counts_but_reaction_without_new_message_does_not(self):
        context=self.inbound()
        reserved=self.reserve(context,text='[表情：歪头]',kind='media')
        self.complete(reserved['ticket'],ids=['501'])
        self.assertEqual(self.deadline(),self.now+120)
        context=self.inbound('2','哈哈')
        reserved=self.reserve(context,text='[消息反应：❤]',kind='reaction')
        self.complete(reserved['ticket'],ids=[])
        self.assertIsNone(self.deadline())

    def test_due_peek_is_read_only_and_claim_consumes_once(self):
        self.replied();due=self.deadline();self.now=due-1
        with self.r.db()as db:self.assertFalse(self.r.companion.peek(db,self.now))
        self.now=due
        for _ in range(2):
            decision=self.r.action('chat_due',{})
            self.assertTrue(decision['allowed']);self.assertTrue(decision['chat_due'])
            self.assertEqual(self.deadline(),due)
        self.claim()
        self.assertIsNone(self.deadline())
        with self.r.db()as db:
            self.assertIsNone(self.r.companion.claim(db,'b'*24,self.now))

    def test_coalesced_received_receipt_does_not_delay_due_check(self):
        self.inbound('1','先说半句')
        self.replied('2','再补半句')
        self.now=self.deadline()
        with self.r.db()as db:
            self.assertEqual(db.execute("SELECT state FROM conversation_receipts WHERE msg='1'").fetchone()[0],'received')
        self.assertTrue(self.r.action('chat_due',{})['chat_due'])

    def test_quiet_round_keeps_default_free_activity_and_does_not_reschedule(self):
        self.replied()
        with self.r.db()as db:default_due=db.execute('SELECT next_due FROM loop_autonomy').fetchone()[0]
        self.now=self.deadline();decision,_=self.claim()
        self.r.action('wake_done',{'wake_id':decision['wake_id'],'outcome':'completed','summary':'安静读完了一篇文章'})
        self.assertIsNone(self.deadline())
        with self.r.db()as db:
            self.assertEqual(db.execute('SELECT next_due FROM loop_autonomy').fetchone()[0],default_due)
            self.assertEqual(db.execute('SELECT enabled FROM loop_autonomy').fetchone()[0],1)

    def test_successful_tick_send_rearms_from_actual_delivery_time(self):
        self.replied();self.now=self.deadline();_,context=self.claim()
        self.now+=7
        reserved=self.reserve(context,text='我又想到刚才那个玩笑了',origin='send_tool')
        self.complete(reserved['ticket'],ids=['601'])
        self.assertEqual(self.deadline(),self.now+120)

    def test_internal_statement_is_not_sent_and_ends_this_tick(self):
        self.replied();self.now=self.deadline();_,context=self.claim()
        blocked=self.reserve(context,text='这一轮不打扰他',origin='send_tool')
        self.assertTrue(blocked.get('skip'),blocked)
        self.assertEqual(blocked.get('reason'),'explicit_internal_note')
        self.assertIsNone(self.deadline())
        another=self.reserve(context,text='我换个句子继续发',origin='send_tool')
        self.assertTrue(another.get('skip'),another)
        self.assertNotIn('ticket',another)

    def test_foreground_direct_goodnight_is_not_internal_note(self):
        context=self.inbound(text='我要去睡啦')
        decision=self.reserve(context,text='我先不打扰你啦，晚安',origin='send_tool')
        self.assertTrue(decision.get('ticket'),decision)

    def test_explicit_sending_tool_filters_all_parts_before_any_telegram_call(self):
        self.replied();self.now=self.deadline();_,context=self.claim()
        self.r.telegram=Mock(return_value={'message_id':502})
        result=self.r.call('send_message',{'parts':['一条普通正文','本轮不外发。'],'_context':context})
        self.assertEqual(result['status'],'silent');self.r.telegram.assert_not_called()
        self.assertFalse(result['delivery_receipt']['answer_delivered'])
        self.assertIsNone(self.deadline())

    def test_caption_uses_the_same_real_outbound_gate(self):
        self.replied();self.now=self.deadline();_,context=self.claim()
        decision=self.r.outbound(dict(context,kind='media',caption='这一轮不打扰他',delivery_origin='send_tool'))
        self.assertTrue(decision['skip']);self.assertNotIn('ticket',decision)

    def test_new_human_message_invalidates_old_proactive_candidate(self):
        self.replied();self.now=self.deadline();_,old=self.claim()
        self.now+=1;self.inbound('2','等等，我有新消息')
        decision=self.reserve(old,text='还在接之前的话',origin='send_tool')
        self.assertTrue(decision.get('skip'),decision)
        self.assertNotIn('ticket',decision)

    def test_late_receipt_does_not_override_a_newer_exchange(self):
        self.replied();self.now=self.deadline();_,old=self.claim()
        reserved=self.reserve(old,text='旧回合已经提交的消息',origin='send_tool')
        self.now+=5;self.replied('2','新的一轮交流')
        newer_due=self.deadline()
        self.now+=9;self.complete(reserved['ticket'],ids=['701'])
        self.assertEqual(self.deadline(),newer_due)

    def test_duplicate_completion_is_idempotent(self):
        context=self.inbound();reserved=self.reserve(context);self.complete(reserved['ticket'],ids=['801'])
        due=self.deadline();self.now+=40;self.complete(reserved['ticket'],ids=['801'])
        self.assertEqual(self.deadline(),due)

    def test_owner_off_and_epoch_change_prevent_late_receipt_rearming(self):
        self.replied();self.now=self.deadline();_,old=self.claim()
        reserved=self.reserve(old,text='正在发送的旧消息',origin='send_tool')
        self.now+=1;self.inbound('2','关闭自主活动')
        self.complete(reserved['ticket'],ids=['901'])
        self.assertIsNone(self.deadline())
        self.now+=86400
        self.assertFalse(self.r.action('chat_due',{})['chat_due'])
        with self.r.db()as db:
            self.assertEqual(db.execute('SELECT enabled FROM loop_autonomy').fetchone()[0],0)

    def test_daily_window_does_not_schedule_chat_past_2200(self):
        self.settings['autonomy_daily_window']={'start':'18:00','end':'22:00','utc_offset_minutes':480}
        self.config.write_text(json.dumps(self.settings),encoding='utf-8');self.r=Runtime(self.config)
        self.now=timestamp('21:59:00');self.replied()
        self.assertIsNone(self.deadline())
        self.now=timestamp('22:00:00')
        self.assertFalse(self.r.action('chat_due',{})['chat_due'])

    def test_legacy_chat_tool_cannot_change_default_free_period(self):
        context=self.inbound()
        with self.r.db()as db:before=dict(db.execute('SELECT * FROM loop_autonomy').fetchone())
        result=self.r.call('silent_alarm',{'action':'create','purpose':'chat','task':'陪聊','interval_seconds':120,'_context':context})
        self.assertTrue(result['managed'])
        with self.r.db()as db:self.assertEqual(dict(db.execute('SELECT * FROM loop_autonomy').fetchone()),before)
        self.assertIsNone(self.deadline())

    def test_chat_claim_does_not_mutate_task_or_quota_state(self):
        context=self.inbound();task=self.r.call('task_loop',{'action':'register','goal':'另一个执行任务','_context':context})
        reserved=self.reserve(context,origin='send_tool');self.complete(reserved['ticket']);self.r.conversations.trace({'chat_id':'123','message_id':'1','state':'completed'})
        with self.r.db()as db:
            db.execute("INSERT INTO loop_quota VALUES('codex',?,'pending',?)",(self.now+120,json.dumps([task['task_id']])))
            before=dict(db.execute('SELECT version,monitor,next_due,data FROM loop_tasks WHERE id=?',(task['task_id'],)).fetchone())
        self.now=self.deadline();self.claim()
        with self.r.db()as db:
            self.assertEqual(dict(db.execute('SELECT version,monitor,next_due,data FROM loop_tasks WHERE id=?',(task['task_id'],)).fetchone()),before)
            self.assertEqual(db.execute("SELECT status FROM loop_quota WHERE provider='codex'").fetchone()[0],'pending')


if __name__=='__main__':unittest.main(verbosity=2)
