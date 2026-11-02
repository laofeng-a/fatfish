import importlib.util
from pathlib import Path
import time
import unittest
from unittest.mock import patch
from second_brain_schedule import SecondBrainSchedule,KEY,DEFAULT_INTERVAL

spec=importlib.util.spec_from_file_location('schedule_base',Path(__file__).with_name('test-fatfish.py'))
base=importlib.util.module_from_spec(spec);spec.loader.exec_module(base)

class Tests(unittest.TestCase):
    setUp=base.PolicyTests.setUp
    def schedule(self):return SecondBrainSchedule(self.r)
    def state(self,at,wake='old'):
        with self.r.db() as db:self.r._set(db,KEY+'_at',at);self.r._set(db,KEY+'_wake',wake)
    def test_foreground_on_demand_unaffected(self):
        self.state(time.time())
        self.assertEqual(self.schedule().before(self.context),(None,None))
        self.schedule().success(None)
    def test_background_skips_before_48_hours_and_survives_recreation(self):
        self.state(100)
        with patch('second_brain_schedule.time.time',return_value=100+DEFAULT_INTERVAL-1):
            ticket,result=self.schedule().before({'session_key':'heartbeat'})
        self.assertIsNone(ticket);self.assertEqual(result['status'],'not_due');self.assertIn('+08:00',result['next_check_at'])
        with patch('second_brain_schedule.time.time',return_value=100+DEFAULT_INTERVAL):
            ticket,result=SecondBrainSchedule(self.r).before({'session_key':'heartbeat'})
        self.assertIsNone(result);self.assertEqual(ticket,'')
    def test_successful_read_only_records_a_success_not_an_attempt(self):
        ticket,result=self.schedule().before({'session_key':'heartbeat'});self.assertIsNone(result)
        self.assertIsNone(self.schedule().before({'session_key':'heartbeat'})[1])
        self.schedule().success(ticket)
        self.assertEqual(self.schedule().before({'session_key':'heartbeat'})[1]['status'],'not_due')
    def test_same_live_wake_can_expand_but_finished_or_next_wake_cannot(self):
        now=time.time();self.state(now,'wake1')
        with self.r.db() as db:
            self.r._set(db,'current_wake','wake1');db.execute('INSERT INTO wakes(id,created,day,finished) VALUES(?,?,?,0)',('wake1',now,'today'))
        self.assertEqual(self.schedule().before({'session_key':'heartbeat'}),('wake1',None))
        self.schedule().success('wake1')
        with self.r.db() as db:
            self.assertEqual(float(self.r._get(db,KEY+'_at')),now)
            db.execute("UPDATE wakes SET finished=1 WHERE id='wake1'")
        self.assertEqual(self.schedule().before({'session_key':'heartbeat'})[1]['status'],'not_due')
        with self.r.db() as db:self.r._set(db,'current_wake','wake2')
        self.assertEqual(self.schedule().before({'session_key':'heartbeat'})[1]['status'],'not_due')
    def test_old_wake_does_not_extend_permission_forever(self):
        now=time.time();self.state(now,'wake1')
        with self.r.db() as db:
            self.r._set(db,'current_wake','wake1');db.execute('INSERT INTO wakes(id,created,day,finished) VALUES(?,?,?,0)',('wake1',now-4000,'today'))
        self.assertEqual(self.schedule().before({'session_key':'heartbeat'})[1]['status'],'not_due')

if __name__=='__main__':unittest.main()
