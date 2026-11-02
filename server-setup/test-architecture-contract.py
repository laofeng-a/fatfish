"""Current scheduling contracts; temp databases and mocked decisions only."""
import importlib.util
from pathlib import Path
import time
import unittest
from unittest.mock import patch
from second_brain_schedule import SecondBrainSchedule,DEFAULT_INTERVAL,RECENT_SUCCESS

spec=importlib.util.spec_from_file_location('architecture_base',Path(__file__).with_name('test-fatfish.py'))
base=importlib.util.module_from_spec(spec);spec.loader.exec_module(base)

class ArchitectureTests(unittest.TestCase):
    setUp=base.PolicyTests.setUp

    def hint(self,now):
        with self.r.db() as db:return SecondBrainSchedule(self.r).due_hint(db,now)

    def test_recent_list_success_controls_due_time(self):
        schedule=SecondBrainSchedule(self.r)
        self.assertTrue(self.hint(100))
        with patch('second_brain_schedule.time.time',return_value=100):
            schedule.success(None,'list_recent')
        self.assertFalse(self.hint(100+DEFAULT_INTERVAL-1))
        self.assertTrue(self.hint(100+DEFAULT_INTERVAL))

    def test_expanding_content_does_not_postpone_next_recent_review(self):
        schedule=SecondBrainSchedule(self.r)
        with patch('second_brain_schedule.time.time',return_value=100):
            schedule.success(None,'list_recent')
        with patch('second_brain_schedule.time.time',return_value=100+DEFAULT_INTERVAL-1):
            schedule.success('wake-2','get')
        self.assertTrue(self.hint(100+DEFAULT_INTERVAL))

    def test_topic_reads_remain_available_before_due_time(self):
        with self.r.db() as db:self.r._set(db,RECENT_SUCCESS,time.time())
        schedule=SecondBrainSchedule(self.r)
        self.assertEqual(schedule.before(self.context),(None,None))
        self.assertIsNone(schedule.before({'session_key':'heartbeat'})[1])

    def test_failed_attempt_does_not_record_success(self):
        self.assertTrue(self.hint(time.time()))
        SecondBrainSchedule(self.r).before({'session_key':'heartbeat'})
        with self.r.db() as db:self.assertEqual(self.r._get(db,RECENT_SUCCESS,''),'')
        self.assertTrue(self.hint(time.time()))

    def test_due_review_has_deterministic_wake_and_existing_spacing(self):
        self.r.settings['exploration_opportunities']={'enabled':True}
        with self.r.db() as db:
            self.r._set(db,'last_user',time.time()-10000)
            self.r._set(db,'last_wake',time.time()-10000)
        with patch.object(self.r.diary,'claim',return_value=None), \
             patch.object(self.r.exploration,'consider',return_value=None):
            result=self.r.wake({})
            self.assertEqual(result['kind'],'ordinary')
            self.assertIn('Second Brain',result['context'])
            with self.r.db() as db:db.execute('UPDATE wakes SET finished=1')
            self.assertTrue(self.r.wake({})['skip'])

    def test_due_review_keeps_existing_quiet_opportunity_policy(self):
        self.r.settings['exploration_opportunities']={'enabled':True}
        with self.r.db() as db:
            self.r._set(db,'last_user',time.time()-10000)
            self.r._set(db,'unanswered',2)
        with patch.object(self.r.diary,'claim',return_value=None), \
             patch.object(self.r.exploration,'consider',return_value=None):
            self.assertTrue(self.r.wake({})['skip'])

if __name__=='__main__':unittest.main()
