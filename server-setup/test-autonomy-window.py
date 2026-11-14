"""Daily free-activity opportunities do not change task or quota lifecycles."""
import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from autonomy_window import DailyWindow
from runtime_policy import Runtime


SPEC={'start':'18:00','end':'22:00','utc_offset_minutes':480}


def at(value):
    return dt.datetime.fromisoformat(value+'+08:00').timestamp()


class DailyWindowTests(unittest.TestCase):
    def setUp(self):
        self.window=DailyWindow(SPEC)

    def test_start_inclusive_end_exclusive_in_utc8(self):
        for clock,inside in [('17:59:59',False),('18:00:00',True),('21:59:59',True),('22:00:00',False)]:
            with self.subTest(clock=clock):
                self.assertEqual(self.window.contains(at('2026-11-06T'+clock)),inside)

    def test_constrain_preserves_inside_and_moves_outside_to_next_opening(self):
        for original,expected in [
            ('2026-11-06T17:59:00','2026-11-06T18:00:00'),
            ('2026-11-06T18:37:12','2026-11-06T18:37:12'),
            ('2026-11-06T22:00:00','2026-11-07T18:00:00'),
            ('2026-11-07T01:00:00','2026-11-07T18:00:00'),
        ]:
            with self.subTest(original=original):
                self.assertEqual(self.window.constrain(at(original)),at(expected))

    def test_hourly_slots_are_anchored_and_skip_the_end_boundary(self):
        for original,inclusive,expected in [
            ('2026-11-06T17:59:00',False,'2026-11-06T18:00:00'),
            ('2026-11-06T18:00:00',True,'2026-11-06T18:00:00'),
            ('2026-11-06T18:00:00',False,'2026-11-06T19:00:00'),
            ('2026-11-06T18:05:00',False,'2026-11-06T19:00:00'),
            ('2026-11-06T21:00:00',True,'2026-11-06T21:00:00'),
            ('2026-11-06T21:00:00',False,'2026-11-07T18:00:00'),
            ('2026-11-06T22:00:00',True,'2026-11-07T18:00:00'),
        ]:
            with self.subTest(original=original,inclusive=inclusive):
                self.assertEqual(self.window.next_slot(at(original),3600,inclusive=inclusive),at(expected))

    def test_view_exposes_configured_window(self):
        self.assertEqual(self.window.view(),SPEC)

    def test_unconfigured_window_preserves_existing_unrestricted_behavior(self):
        window=DailyWindow(None);instant=at('2026-11-06T03:17:00')
        self.assertTrue(window.contains(instant))
        self.assertEqual(window.constrain(instant),instant)
        self.assertIsNone(window.view())


class AutonomyWindowRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        root=Path(self.tmp.name);(root/'catalog.json').write_text('[]',encoding='utf-8')
        config=root/'runtime.json';config.write_text(json.dumps({
            'owner_id':'123','state_dir':str(root),'catalog_file':str(root/'catalog.json'),
            'autonomy_interval_seconds':3600,'autonomy_daily_window':SPEC,
        }),encoding='utf-8')
        with patch('agent_loops.time.time',return_value=at('2026-11-06T17:00:00')):
            self.r=Runtime(config)
        self.ctx={'channel':'telegram','chat_id':'123','sender_id':'123','session_key':'telegram:123'}
        with self.r.db()as db:
            db.execute('UPDATE loop_autonomy SET enabled=1,epoch=1,interval_seconds=3600,next_due=? WHERE id=1',(at('2026-11-06T18:00:00'),))

    def claim(self,when,number=1):
        now=at(when)
        with patch('agent_loops.time.time',return_value=now),self.r.db()as db:
            return self.r.loops.claim(db,format(number,'024x'),now)

    def status(self,when):
        with patch('agent_loops.time.time',return_value=at(when)):
            return self.r.call('autonomy',{'action':'status','_context':self.ctx})

    def test_only_four_hourly_opportunities_and_next_day_continuation(self):
        self.assertIsNone(self.claim('2026-11-06T17:59:59'))
        for i,hour in enumerate([18,19,20,21],1):
            with self.subTest(hour=hour):
                # The minute heartbeat may arrive seconds after the scheduled slot.
                wake=self.claim('2026-11-06T'+str(hour)+':00:17',i)
                self.assertEqual(wake['kind'],'autonomy')
                expected='2026-11-06T'+str(hour+1)+':00:00' if hour<21 else '2026-11-07T18:00:00'
                with self.r.db()as db:
                    self.assertEqual(db.execute('SELECT next_due FROM loop_autonomy').fetchone()[0],at(expected))
        self.assertIsNone(self.claim('2026-11-06T22:00:00',5))
        self.assertIsNone(self.claim('2026-11-07T17:59:59',6))
        self.assertEqual(self.claim('2026-11-07T18:00:00',7)['kind'],'autonomy')

    def test_stale_due_outside_window_moves_to_next_open_without_a_wake(self):
        with self.r.db()as db:db.execute('UPDATE loop_autonomy SET next_due=?',(at('2026-11-06T21:00:00'),))
        self.assertIsNone(self.claim('2026-11-06T22:00:00'))
        with self.r.db()as db:
            self.assertEqual(db.execute('SELECT next_due FROM loop_autonomy').fetchone()[0],at('2026-11-07T18:00:00'))
            self.assertEqual(db.execute('SELECT COUNT(*) FROM loop_wakes').fetchone()[0],0)

    def test_window_is_not_a_revocation_of_running_epoch(self):
        with patch('agent_loops.time.time',return_value=at('2026-11-06T22:00:00')),self.r.db()as db:
            self.assertTrue(self.r.loops.active(db,1))
            self.assertTrue(self.r.loops.admitted(db,{'session_key':'agent:autonomy-1'}))

    def test_task_patrol_is_still_claimed_outside_free_activity_window(self):
        now=at('2026-11-06T22:00:00')
        with patch('agent_loops.time.time',return_value=now):
            self.r.inbound(dict(self.ctx,message_id='task-goal',text='完成报告'))
            task=self.r.call('task_loop',{'action':'register','goal':'完成报告','_context':dict(self.ctx,message_id='task-goal')})
        with self.r.db()as db:db.execute('UPDATE loop_tasks SET next_due=? WHERE id=?',(now-1,task['task_id']))
        wake=self.claim('2026-11-06T22:00:00')
        self.assertEqual(wake['kind'],'task');self.assertEqual(wake['session_key'],'agent:task-'+task['task_id'])

    def test_quota_watch_is_not_cancelled_when_daily_window_is_closed(self):
        now=at('2026-11-06T22:00:00')
        for i,epoch in enumerate([None,1],1):
            with self.subTest(origin_epoch=epoch),self.r.db()as db:
                db.execute('DELETE FROM loop_quota')
                db.execute("INSERT INTO loop_quota VALUES('codex',?,'pending','[]')",(now-1,))
                db.execute("INSERT OR REPLACE INTO loop_quota_watch VALUES('codex',1,?)",(epoch,))
            wake=self.claim('2026-11-06T22:00:00',i)
            self.assertEqual(wake['kind'],'quota');self.assertEqual(wake['session_key'],'agent:quota')
            with self.r.db()as db:self.assertEqual(db.execute('SELECT status FROM loop_quota').fetchone()[0],'checked')

    def test_owner_disable_does_not_reenable_at_next_days_opening(self):
        with patch('agent_loops.time.time',return_value=at('2026-11-06T20:30:00')):
            self.r.inbound(dict(self.ctx,message_id='off',text='关闭自主活动'))
        self.assertIsNone(self.claim('2026-11-07T18:00:00'))
        with self.r.db()as db:self.assertEqual(tuple(db.execute('SELECT enabled,epoch FROM loop_autonomy').fetchone()),(0,2))

    def test_status_distinguishes_master_switch_from_daily_window(self):
        for clock,inside in [('17:59:00',False),('18:00:00',True),('22:00:00',False)]:
            with self.subTest(clock=clock):
                value=self.status('2026-11-06T'+clock)
                self.assertEqual(value['enabled'],1)
                self.assertEqual(value['daily_window'],SPEC)
                self.assertEqual(value['in_daily_window'],inside)

    def test_adjust_clamps_next_opportunity_without_changing_requested_interval(self):
        with patch('agent_loops.time.time',return_value=at('2026-11-06T21:30:00')):
            result=self.r.call('autonomy',{'action':'adjust','interval_seconds':3600,'_context':self.ctx})
        self.assertEqual(result['next_due'],at('2026-11-07T18:00:00'));self.assertEqual(result['interval_seconds'],3600)

    def test_skip_last_opportunity_moves_to_next_days_window(self):
        with self.r.db()as db:db.execute('UPDATE loop_autonomy SET next_due=?',(at('2026-11-06T21:00:00'),))
        with patch('agent_loops.time.time',return_value=at('2026-11-06T20:30:00')):
            result=self.r.call('autonomy',{'action':'skip_next','_context':self.ctx})
        self.assertEqual(result['next_due'],at('2026-11-07T18:00:00'));self.assertEqual(result['enabled'],1)

    def test_noon_slots_and_evening_window_share_the_daily_schedule(self):
        self.r.loops.daily_window=DailyWindow(dict(SPEC,extra_times=['13:00','12:30']))
        self.assertIsNone(self.claim('2026-11-06T22:00:00'))
        with self.r.db() as db:
            self.assertEqual(db.execute('SELECT next_due FROM loop_autonomy').fetchone()[0],at('2026-11-07T12:30:00'))
        self.assertIsNone(self.claim('2026-11-07T12:29:59'))
        self.assertEqual(self.claim('2026-11-07T12:30:53',2)['kind'],'autonomy')
        self.assertIsNone(self.claim('2026-11-07T12:59:59'))
        self.assertEqual(self.claim('2026-11-07T13:00:53',3)['kind'],'autonomy')
        with self.r.db() as db:
            self.assertEqual(db.execute('SELECT next_due FROM loop_autonomy').fetchone()[0],at('2026-11-07T18:00:00'))
        self.assertEqual(self.claim('2026-11-07T18:00:53',4)['kind'],'autonomy')

    def test_skip_one_noon_slot_keeps_the_other(self):
        self.r.loops.daily_window=DailyWindow(dict(SPEC,extra_times=['12:30','13:00']))
        with self.r.db() as db:
            db.execute('UPDATE loop_autonomy SET next_due=?',(at('2026-11-06T12:30:00'),))
        with patch('agent_loops.time.time',return_value=at('2026-11-06T12:00:00')):
            result=self.r.call('autonomy',{'action':'skip_next','_context':self.ctx})
        self.assertEqual(result['next_due'],at('2026-11-06T13:00:00'))


if __name__=='__main__':unittest.main(verbosity=2)
