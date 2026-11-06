"""Runtime integration: durable routing, silent delivery, cancellation and quota."""
import importlib.util
import json
from pathlib import Path
import time
import unittest
from unittest.mock import Mock,patch

spec=importlib.util.spec_from_file_location('notes_fixture',Path(__file__).with_name('test-memory-notes.py'))
fixture=importlib.util.module_from_spec(spec);spec.loader.exec_module(fixture)


class LoopTests(unittest.TestCase):
    setUp=fixture.NoteTests.setUp

    def task(self,action='register',**args):return self.r.call('task_loop',dict(action=action,_context=self.ctx,**args))
    def request(self,task):return self.r.action('loop_context',{'session_key':'agent:task-'+task})

    def test_task_versions_original_source_and_parallel_isolation(self):
        self.r.inbound({'chat_id':'123','sender_id':'123','message_id':'1','text':'把真实目标做好，不要反复验收'})
        a=self.task(goal='做好真实目标',completion='实际可用');b=self.task(goal='另一任务')
        updated=self.task('update',task_id=a['task_id'],agreements='新增要求：保留现有服务')
        actual=self.request(a['task_id'])
        self.assertEqual(updated['version'],2)
        self.assertIn('把真实目标做好',actual['context']);self.assertIn('保留现有服务',actual['context'])
        self.assertNotIn('另一任务',actual['context'])
        self.assertEqual(self.request(b['task_id'])['version'],1)
        self.assertEqual(len(self.task('read',task_id=a['task_id'])['history']),1)

    def test_patrol_silent_final_explicit_send_once_and_report_switch(self):
        a=self.task(goal='部署目标');ctx=dict(self.ctx,session_key='agent:task-'+a['task_id'],message_id='heartbeat:'+('a'*24))
        self.r.telegram=Mock(return_value={'message_id':9})
        result=self.r.outbound(dict(ctx,content='本轮没有打扰jg',delivery_origin='auto_final',mode='background'))
        self.assertTrue(result['skip']);self.r.telegram.assert_not_called()
        for _ in range(2):self.r.call('send_message',dict(text='任务已完成',_context=ctx))
        self.assertEqual(self.r.telegram.call_count,1)
        self.task('report',task_id=a['task_id'],enabled=False)
        self.r.call('send_message',dict(text='新的内部进展',_context=ctx))
        self.assertEqual(self.r.telegram.call_count,1)
        self.task('update',task_id=a['task_id'],progress='关闭汇报仍然推进')
        self.assertIn('仍然推进',self.request(a['task_id'])['context'])

    def test_patrol_finish_keeps_quota_and_coalesces_real_window(self):
        a=self.task(goal='A');b=self.task(goal='B');reset=int(time.time())+120
        usage={'observed_at':'2026-11-05T00:00:00Z','ordinary_usage_allowed':False,'limits':[{'limit_id':'codex','primary':{'used_percent':100,'resets_at':reset}}]}
        with self.r.db() as db:self.r.codex.store_usage(db,usage)
        for task in (a,b):self.r.call('quota_wait',dict(provider='codex',reset_at=reset,task_id=task['task_id'],_context=self.ctx))
        with self.assertRaises(ValueError):self.r.call('quota_wait',dict(provider='codex',reset_at=reset+1,_context=self.ctx))
        self.task('finish',task_id=a['task_id'],leftovers='不影响使用的小尾巴')
        with self.r.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM loop_quota').fetchone()[0],1)
            wake=self.r.loops.claim(db,'q'*24,reset+1)
        self.assertEqual(wake['session_key'],'agent:task-'+b['task_id'])
        self.assertEqual(self.r.action('loop_context',{'session_key':wake['session_key'],'message_id':'heartbeat:'+wake['wake_id']})['wake_kind'],'quota')
        self.assertEqual(self.task('read',task_id=a['task_id'])['monitor'],False)

    def test_quiet_autonomy_continues_and_user_off_revokes_descendants(self):
        with self.r.db() as db:
            wake=self.r.loops.claim(db,'a'*24,time.time()+901)
        self.assertEqual(wake['kind'],'autonomy')
        ctx=dict(self.ctx,session_key=wake['session_key'],message_id='heartbeat:'+wake['wake_id'])
        self.assertEqual(self.r.action('loop_context',ctx)['wake_kind'],'autonomy')
        self.r.action('wake_done',dict(wake_id=wake['wake_id'],outcome='completed',summary='安静阅读'))
        a=self.r.call('silent_alarm',dict(action='create',purpose='maintenance',task='接着读',delay_seconds=120,_context=ctx))
        independent=self.r.call('reminder',dict(action='create',text='jg交办独立提醒',delay_seconds=300,_context=self.ctx))
        derived=self.r.call('reminder',dict(action='create',text='自主阅读派生提醒',delay_seconds=300,_context=ctx))
        self.r.inbound({'chat_id':'123','sender_id':'123','message_id':'off','text':'关闭自主活动'})
        with self.r.db() as db:
            self.assertEqual(db.execute('SELECT status FROM silent_alarms WHERE id=?',(a['id'],)).fetchone()[0],'cancelled')
            self.assertEqual(db.execute('SELECT status FROM reminders WHERE id=?',(derived['id'],)).fetchone()[0],'cancelled')
            self.assertEqual(db.execute('SELECT status FROM reminders WHERE id=?',(independent['id'],)).fetchone()[0],'pending')
        self.assertFalse(self.r.action('loop_context',ctx)['active'])
        with self.assertRaises(ValueError):self.r.call('autonomy',dict(action='adjust',interval_seconds=900,_context=ctx))
        self.r.inbound({'chat_id':'123','sender_id':'123','message_id':'on','text':'恢复自主活动'})
        self.assertFalse(self.r.action('loop_context',ctx)['active'])
        with self.r.db() as db:self.assertTrue(db.execute('SELECT enabled FROM loop_autonomy').fetchone()[0])

    def test_late_executor_event_is_saved_without_wake_or_stop(self):
        ctx=dict(self.ctx,session_key='agent:autonomy-1',message_id='heartbeat:'+('d'*24))
        task=self.r.loops.task({'action':'register','goal':'自主探索'},ctx,'123')
        origin=dict(ctx,task_id=task['task_id']);thread='actual-thread-123456'
        with self.r.db() as db:
            chain={'id':'chain','thread_id':thread,'automatic_exchanges':0}
            db.execute('INSERT INTO codex_chains(id,thread_id,initial_job,state,created,updated) VALUES(?,?,?,?,?,?)',('chain',thread,'first','running',time.time(),time.time()))
            queued=self.r.codex._create_followup(db,chain,None,'开展探索','source',True,origin)
            self.r.loops.bind_job(db,origin,'codex',queued['job_id'])
        self.r.inbound({'chat_id':'123','sender_id':'123','message_id':'off','text':'第二类先撤'})
        with self.r.db() as db:
            job=db.execute('SELECT * FROM codex_jobs WHERE id=?',(queued['job_id'],)).fetchone()
            self.assertEqual(job['state'],'queued') # Already dispatched, intentionally retained.
            event=self.r.codex.enqueue_event(db,job,'real-turn','completed','真实结果保留')
        result=self.r.codex.runtime_action('codex_event_poll',{})
        self.assertIsNone(result['event'])
        with self.r.db() as db:
            row=db.execute('SELECT content,outcome FROM codex_events WHERE id=?',(event,)).fetchone()
            self.assertIn('真实结果',row['content']);self.assertEqual(row['outcome'],'autonomy_revoked')
            self.assertEqual(db.execute("SELECT COUNT(*) FROM codex_jobs WHERE action='stop'").fetchone()[0],0)

    def test_automatic_followup_reuses_pending_request_across_wakes(self):
        task=self.task(goal='任务A');thread='real-thread-123456';origin=dict(self.ctx,task_id=task['task_id'],message_id='heartbeat:'+('b'*24))
        with self.r.db() as db:
            chain={'id':'chain','thread_id':thread,'automatic_exchanges':0}
            db.execute('INSERT INTO codex_chains(id,thread_id,initial_job,state,created,updated) VALUES(?,?,?,?,?,?)',('chain',thread,'first','running',time.time(),time.time()))
            a=self.r.codex._create_followup(db,chain,None,'继续具体缺口','one',True,origin)
            origin['message_id']='heartbeat:'+('c'*24)
            b=self.r.codex._create_followup(db,chain,None,'继续具体缺口','two',True,origin)
            self.assertEqual(a['job_id'],b['job_id']);self.assertTrue(b['reused_pending_request'])
            row=db.execute('SELECT * FROM codex_jobs WHERE id=?',(a['job_id'],)).fetchone()
            event=self.r.codex.enqueue_event(db,row,'actual-turn','completed','执行端真实结果')
        receipt=self.r.codex.runtime_action('codex_event_poll',{})['event']
        self.assertEqual(receipt['session_key'],'agent:task-'+task['task_id'])
        self.assertEqual(receipt['job_id'],a['job_id'])

    def test_reply_supplement_updates_only_associated_task(self):
        a=self.task(goal='A');b=self.task(goal='B')
        with self.r.db() as db:
            db.execute('INSERT INTO loop_bindings VALUES(?,?,?,NULL)',('sent','sent-1',a['task_id']))
        self.r.inbound({'chat_id':'123','sender_id':'123','message_id':'supplement','reply_to_message_id':'sent-1','text':'补充：输出保留原文'})
        self.assertIn('保留原文',self.request(a['task_id'])['context'])
        self.assertEqual(self.request(b['task_id'])['version'],1)

    def test_discussing_switch_does_not_execute_quoted_commands(self):
        self.r.inbound({'chat_id':'123','sender_id':'123','message_id':'discussion','text':'请实现这些能力：jg可以说“关闭自主活动”或“恢复自主活动”。'})
        with self.r.db() as db:
            row=db.execute('SELECT enabled,epoch FROM loop_autonomy').fetchone()
            self.assertEqual(tuple(row),(1,1))

    def test_forwarded_command_cannot_restore_autonomy(self):
        self.r.inbound({'chat_id':'123','sender_id':'123','message_id':'off','text':'关闭自主活动'})
        self.r.inbound({'chat_id':'123','sender_id':'123','message_id':'forward','forwarded':True,'text':'恢复自主活动'})
        with self.r.db() as db:self.assertEqual(db.execute('SELECT enabled FROM loop_autonomy').fetchone()[0],0)

    def test_user_cancels_patrols_without_stopping_executor_or_autonomy(self):
        a=self.task(goal='A');b=self.task(goal='B')
        self.r.inbound({'chat_id':'123','sender_id':'123','message_id':'stop-monitor','text':'把巡检关了吧'})
        for task in (a,b):
            row=self.task('read',task_id=task['task_id']);self.assertFalse(row['monitor']);self.assertEqual(row['status'],'active')
        auto=dict(self.ctx,session_key='agent:task-'+a['task_id'],message_id='heartbeat:'+('f'*24))
        with self.assertRaises(ValueError):self.r.call('task_loop',{'action':'monitor','enabled':True,'_context':auto})
        with self.r.db() as db:
            self.assertTrue(db.execute('SELECT enabled FROM loop_autonomy').fetchone()[0]);self.assertEqual(db.execute("SELECT COUNT(*) FROM codex_jobs WHERE action='stop'").fetchone()[0],0)

    def test_cancelled_patrol_stops_at_boundary_but_receipt_still_runs(self):
        task=self.task(goal='仍在运行的任务')
        with self.r.db() as db:
            db.execute('UPDATE loop_tasks SET next_due=0 WHERE id=?',(task['task_id'],));wake=self.r.loops.claim(db,'c'*24,time.time())
        self.r.inbound({'chat_id':'123','sender_id':'123','message_id':'cancel-monitor','text':'把巡检关了吧'})
        patrol=dict(self.ctx,session_key=wake['session_key'],message_id='heartbeat:'+wake['wake_id'])
        self.assertFalse(self.r.action('loop_context',patrol)['active'])
        with self.assertRaises(ValueError):self.r.call('memory_note_read',{'_context':patrol})
        receipt=dict(patrol,message_id='codex-event:'+('a'*24))
        self.assertTrue(self.r.action('loop_context',receipt)['active'])

    def test_task_cancel_blocks_old_human_turn_without_blocking_new_reply(self):
        self.r.inbound({'chat_id':'123','sender_id':'123','message_id':'1','text':'执行任务A'})
        task=self.task(goal='A')
        self.r.inbound({'chat_id':'123','sender_id':'123','message_id':'2','text':'task:'+task['task_id']+' 取消任务'})
        with self.assertRaises(ValueError):self.r.call('memory_note_read',{'_context':self.ctx})
        self.r.call('memory_note_read',{'_context':dict(self.ctx,message_id='2')})

    def test_autonomy_quota_retains_lineage_after_its_task_finishes(self):
        ctx=dict(self.ctx,session_key='agent:autonomy-1',message_id='heartbeat:'+('e'*24))
        task=self.r.loops.task({'action':'register','goal':'自发任务'},ctx,'123');reset=int(time.time())+120
        with self.r.db() as db:self.r.codex.store_usage(db,{'limits':[{'primary':{'used_percent':100,'resets_at':reset}}]})
        self.r.loops.quota({'provider':'codex','reset_at':reset,'task_id':task['task_id']},ctx)
        self.r.loops.task({'action':'finish','task_id':task['task_id']},ctx,'123')
        with self.r.db() as db:
            wake=self.r.loops.claim(db,'d'*24,reset+1)
            self.assertEqual(wake['session_key'],'agent:quota')
            quota_ctx=dict(self.ctx,session_key=wake['session_key'],message_id='heartbeat:'+wake['wake_id'])
            self.r.loops.bind_job(db,quota_ctx,'codex','slow-usage-job')
        self.r.inbound({'chat_id':'123','sender_id':'123','message_id':'off','text':'关闭自主活动'})
        self.assertFalse(self.r.action('loop_context',quota_ctx)['active'])
        with self.r.db() as db:self.assertTrue(self.r.loops.event_context(db,'codex','slow-usage-job')['revoked'])

    def test_quiet_autonomy_really_claims_next_opportunity(self):
        with self.r.db() as db:first=self.r.loops.claim(db,'a'*24,time.time()+901)
        self.r.action('wake_done',{'wake_id':first['wake_id'],'outcome':'completed','summary':'安静结束'})
        with self.r.db() as db:
            state=db.execute('SELECT * FROM loop_autonomy').fetchone()
            second=self.r.loops.claim(db,'b'*24,state['next_due']+1)
        self.assertEqual(first['session_key'],second['session_key'])
        self.assertEqual(second['kind'],'autonomy')

    def test_explicit_task_stop_queues_stop_but_monitor_stop_does_not(self):
        task=self.task(goal='执行目标');ctx=dict(self.ctx,task_id=task['task_id'],message_id='heartbeat:'+('a'*24))
        with self.r.db() as db:
            db.execute('INSERT INTO codex_chains(id,thread_id,initial_job,state,created,updated) VALUES(?,?,?,?,?,?)',('chain','actual-thread-123456','first','running',time.time(),time.time()))
            self.r.codex._create_followup(db,{'id':'chain','thread_id':'actual-thread-123456','automatic_exchanges':0},None,'执行原目标','source',True,ctx)
        self.task('monitor',task_id=task['task_id'],enabled=False)
        with self.r.db() as db:self.assertEqual(db.execute("SELECT COUNT(*) FROM codex_jobs WHERE action='stop'").fetchone()[0],0)
        self.r.inbound({'chat_id':'123','sender_id':'123','message_id':'stop','text':'task:'+task['task_id']+' 取消任务'})
        with self.r.db() as db:self.assertEqual(db.execute("SELECT COUNT(*) FROM codex_jobs WHERE action='stop'").fetchone()[0],1)

    def test_completed_job_receipts_survive_queue_pruning(self):
        task=self.task(goal='保留回执');ctx=dict(self.ctx,task_id=task['task_id'],message_id='heartbeat:'+('e'*24))
        with self.r.db() as db:
            db.execute('INSERT INTO codex_chains(id,thread_id,initial_job,state,created,updated) VALUES(?,?,?,?,?,?)',('chain','actual-thread-123456','first','running',time.time(),time.time()))
            job=self.r.codex._create_followup(db,{'id':'chain','thread_id':'actual-thread-123456','automatic_exchanges':0},None,'执行原目标','source',True,ctx)
            db.execute('UPDATE codex_jobs SET output=? WHERE id=?',(json.dumps({'thread_id':'actual-thread-123456','turn_id':'full-turn-id','final':'真实回执'}),job['job_id']))
            db.execute('DELETE FROM codex_jobs WHERE id=?',(job['job_id'],))
        record=self.task('read',task_id=task['task_id'])
        self.assertIn('full-turn-id',json.dumps(record['receipts']))

    def test_patrol_default_thread_comes_from_its_task(self):
        task=self.task(goal='A');ctx=dict(self.ctx,task_id=task['task_id'],session_key='agent:task-'+task['task_id'],message_id='heartbeat:'+('a'*24))
        with self.r.db() as db:
            db.execute('INSERT INTO codex_chains(id,thread_id,initial_job,state,created,updated) VALUES(?,?,?,?,?,?)',('chain','task-a-thread-1234','first','running',time.time(),time.time()))
            self.r.codex._create_followup(db,{'id':'chain','thread_id':'task-a-thread-1234','automatic_exchanges':0},None,'执行A','source',True,ctx)
            db.execute('INSERT OR REPLACE INTO codex_thread_bindings VALUES(?,?,?)',('123','unrelated-thread-b',time.time()))
        args={'action':'read'};resolved=self.r.loops.bridge_context(args,ctx,'codex')
        self.assertEqual(args['thread_id'],'task-a-thread-1234')
        with self.r.db() as db:
            self.assertEqual(self.r.codex.resolve_thread(db,args,resolved),'task-a-thread-1234')
            self.assertEqual(db.execute("SELECT thread_id FROM codex_thread_bindings WHERE scope='123'").fetchone()[0],'unrelated-thread-b')

    def test_reporting_master_switch_does_not_pause_tasks_or_autonomy(self):
        a=self.task(goal='A');b=self.task(goal='B')
        self.task('report',scope='all',enabled=False)
        for task in (a,b):
            state=self.request(task['task_id']);self.assertTrue(state['active']);self.assertFalse(state['report']);self.assertFalse(state['record']['report'])
        auto=self.r.action('loop_context',{'session_key':'agent:autonomy-1'});self.assertTrue(auto['active']);self.assertTrue(auto['report'])

    def test_new_human_reply_survives_autonomy_revocation(self):
        ctx=dict(self.ctx,session_key='agent:autonomy-1',message_id='heartbeat:'+('f'*24))
        task=self.r.loops.task({'action':'register','goal':'旧探索'},ctx,'123')
        with self.r.db() as db:db.execute('INSERT INTO loop_bindings VALUES(?,?,?,?)',('sent','old-result',task['task_id'],1))
        self.r.inbound({'chat_id':'123','sender_id':'123','message_id':'off','text':'关闭自主活动'})
        self.r.inbound({'chat_id':'123','sender_id':'123','message_id':'new-human','reply_to_message_id':'old-result','text':'这份结果给我再看看'})
        human=dict(self.ctx,message_id='new-human')
        self.assertTrue(self.r.action('loop_context',human)['active'])
        self.r.call('memory_note_read',{'_context':human})
        self.assertFalse(self.r.action('loop_context',ctx)['active'])


if __name__=='__main__':unittest.main(verbosity=2)
