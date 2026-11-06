"""Reproduce the November 5 cancellations, stale requests and receipt failures."""
import base64
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock,patch

from runtime_policy import Runtime
from execution_status import normalized_output
from session_recovery import recover_session


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        root=Path(self.tmp.name);(root/'catalog.json').write_text('[]')
        cfg=root/'settings.json';cfg.write_text(json.dumps({'owner_id':'123','state_dir':str(root),'catalog_file':str(root/'catalog.json')}))
        self.r=Runtime(cfg);self.ctx={'channel':'telegram','chat_id':'123','message_id':'1','session_key':'telegram:123'}
        self.r.inbound(dict(self.ctx,sender_id='123',text='做一份报告'))
        self.task=self.r.call('task_loop',dict(action='register',goal='做报告',_context=self.ctx))
        self.bg=dict(self.ctx,session_key='agent:task-'+self.task['task_id'],message_id='heartbeat:'+'a'*24,task_id=self.task['task_id'])

    def alarm(self,**extra):
        return self.r.call('silent_alarm',dict(action='create',task='复查报告',purpose='maintenance',delay_seconds=60,_context=self.bg,**extra))

    def seed_send(self,job='request-one',text='原始任务',output=None,task=None):
        task=task or self.task['task_id'];thread='native-thread-123456';now=time.time()
        origin=dict(self.bg,task_id=task,instruction_version=1)
        with self.r.db() as db:
            db.execute("INSERT INTO codex_chains(id,thread_id,initial_job,state,created,updated) VALUES(?,?,?,'running',?,?)",(job,thread,job,now,now))
            db.execute("INSERT INTO codex_jobs(id,source,fingerprint,action,args,state,claim,created,updated,output,chain_id,origin) VALUES(?,?,?,'send',?,'done','',?,?,?,?,?)",
                (job,job,job,json.dumps({'action':'send','thread_id':thread,'text':text}),now,now,json.dumps(output or {'state':'queued_on_owner','thread_id':thread,'client_user_message_id':'fatfish-'+job}),job,json.dumps(origin)))
            self.r.loops.bind_job(db,origin,'codex',job)
        return job,thread

    def test_monitor_cancel_revokes_derived_alarm_at_claim_and_during_turn(self):
        alarm=self.alarm()
        with self.r.db() as db:
            self.r._set(db,'last_user',0)
            db.execute('INSERT INTO loop_wakes VALUES(?,?,NULL,?)',('a'*24,self.task['task_id'],'alarm'))
        self.r.inbound(dict(self.ctx,sender_id='123',message_id='2',text='把任务时钟撤了。'))
        with self.r.db() as db:
            self.assertEqual(db.execute('SELECT status FROM silent_alarms WHERE id=?',(alarm['id'],)).fetchone()[0],'cancelled')
            self.assertIsNone(self.r.silent_alarms.claim(db,'b'*24,time.time()+120))
            self.assertFalse(self.r.loops.admitted(db,self.bg))
        with self.assertRaises(ValueError):self.alarm()
        with self.assertRaises(ValueError):self.r.call('task_loop',dict(action='monitor',task_id=self.task['task_id'],enabled=True,_context=self.bg))

    def test_handoff_and_all_off_persist_even_without_llm_and_keep_executor(self):
        job,thread=self.seed_send();self.alarm()
        self.r.inbound(dict(self.ctx,sender_id='123',message_id='2',text='任务结束啦傻鱼。你把后台闹钟都关了'))
        with self.r.db() as db:
            auto=db.execute('SELECT enabled,epoch FROM loop_autonomy').fetchone();self.assertEqual(tuple(auto),(0,2))
            task=db.execute('SELECT data,monitor FROM loop_tasks WHERE id=?',(self.task['task_id'],)).fetchone()
            self.assertEqual(json.loads(task['data'])['status'],'handed_back');self.assertEqual(task['monitor'],0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM codex_jobs WHERE action='stop'").fetchone()[0],0)
            route=self.r.loops.event_context(db,'codex',job);self.assertTrue(route['archived'])
        with self.assertRaises(ValueError):self.r.call('codex',dict(action='continue',text='再做一次',thread_id=thread,_context=self.bg))

    def test_next_autonomy_time_does_not_change_interval(self):
        before=self.r.call('autonomy',dict(action='status',_context=self.ctx))
        after=self.r.call('autonomy',dict(action='adjust',delay_seconds=600,_context=self.ctx))
        self.assertEqual(after['interval_seconds'],before['interval_seconds'])
        self.assertAlmostEqual(after['next_due'],time.time()+600,delta=2)

    def test_free_alarm_message_does_not_overwrite_task_instruction(self):
        self.r.inbound(dict(self.ctx,sender_id='123',message_id='2',text='你设个自由闹钟吧，10min后'))
        self.r.call('task_loop',dict(action='monitor',task_id=self.task['task_id'],enabled=False,_context=dict(self.ctx,message_id='2')))
        with self.r.db() as db:
            data=json.loads(db.execute('SELECT data FROM loop_tasks WHERE id=?',(self.task['task_id'],)).fetchone()[0])
        self.assertNotIn('latest_instruction',data)

    def test_pending_matches_instruction_and_instruction_version(self):
        job,thread=self.seed_send(text='原始任务')
        with self.r.db() as db:
            self.assertIsNotNone(self.r.loops.pending_request(db,self.bg,'codex',thread,{'text':'原始任务'}))
            self.assertIsNone(self.r.loops.pending_request(db,self.bg,'codex',thread,{'text':'补写并同步报告'}))
            self.assertIsNone(self.r.loops.pending_request(db,dict(self.bg,instruction_version=2),'codex',thread,{'text':'原始任务'}))

    def test_terminal_evidence_overrides_old_queue_flags(self):
        output=normalized_output({'state':'queued_on_owner','started':False,'terminal_confirmed':True,'execution_state':'completed','requires_reconciliation':True})
        self.assertEqual(output['state'],'completed');self.assertTrue(output['started']);self.assertEqual(output['submission_state'],'queued_on_owner')
        self.assertNotIn('requires_reconciliation',output)

    def test_receipt_failure_preserves_execution_and_retry_cannot_redispatch(self):
        job,thread=self.seed_send(output={'thread_id':'native-thread-123456','turn_id':'actual-turn','terminal_confirmed':True,'execution_state':'completed'})
        with self.r.db() as db:
            row=db.execute('SELECT * FROM codex_jobs WHERE id=?',(job,)).fetchone()
            event=self.r.codex.enqueue_event(db,row,'actual-turn','completed','报告已经写完')
        polled=self.r.codex.runtime_action('codex_event_poll',{})['event'];claim=polled['claim']
        self.r.codex.runtime_action('codex_event_start',{'event_id':event,'claim':claim})
        ack=self.r.codex.runtime_action('codex_event_ack',{'event_id':event,'claim':claim,'outcome':'error'})
        self.assertTrue(ack['execution_preserved'])
        context=dict(self.bg,message_id='codex-event:'+event)
        with self.r.db() as db:
            chain=db.execute('SELECT execution_state,receipt_state FROM codex_chains WHERE id=?',(job,)).fetchone()
            self.assertEqual(tuple(chain),('completed','retry_wait'))
            self.assertTrue(self.r.loops.receipt_recovery(db,context))
        with self.assertRaises(ValueError):self.r.call('codex',dict(action='continue',text='重新做报告',thread_id=thread,_context=context))

    def test_new_task_does_not_reuse_old_execution_chain_on_same_thread(self):
        self.seed_send()
        other=self.r.call('task_loop',dict(action='register',goal='新的目标',_context=self.ctx))
        with patch('codex_bridge.time.sleep'):
            result=self.r.call('codex',dict(action='continue',text='新的目标',thread_id='native-thread-123456',_context=dict(self.bg,task_id=other['task_id'],session_key='agent:task-'+other['task_id'])))
        self.assertNotEqual(result['chain_id'],'request-one')

    def test_task_maintenance_checks_coalesce_and_failure_uses_patrol(self):
        first=self.alarm();second=self.alarm()
        self.assertEqual(first['id'],second['id'])
        now=time.time()+120
        with self.r.db() as db:
            alarm=self.r.silent_alarms.claim(db,'c'*24,now)
            self.assertEqual(alarm['id'],first['id'])
            self.assertGreaterEqual(db.execute('SELECT next_due FROM loop_tasks WHERE id=?',(self.task['task_id'],)).fetchone()[0],now+1500)
            self.r.silent_alarms.finish(db,'c'*24,'error','写入失败',now)
            self.assertEqual(db.execute('SELECT status FROM silent_alarms WHERE id=?',(first['id'],)).fetchone()[0],'error')

    def test_light_read_preserves_reconciled_superseded_submission(self):
        output={'thread_id':'native-thread-123456','state':'superseded','submission_status':'superseded','execution_state':'not_started','client_user_message_id':'old'}
        job,thread=self.seed_send(output=output)
        with self.r.db()as db:
            self.r.codex.reconcile_read_result(db,{'thread_id':thread,'pending_messages':[],'more_pending_messages':False})
            self.assertEqual(json.loads(db.execute('SELECT output FROM codex_jobs WHERE id=?',(job,)).fetchone()[0]),output)

    def test_queued_artifact_notifies_once_without_exposing_file_body(self):
        job,thread=self.seed_send();raw=b'original report';digest=hashlib.sha256(raw).hexdigest()
        with self.r.db() as db:
            db.execute("UPDATE codex_jobs SET action='artifact',state='claimed',claim='receive',notify_pending=1 WHERE id=?",(job,))
        result=self.r.codex.worker({'action':'result','id':job,'claim':'receive','result':{
            'thread_id':thread,'path':'C:/report.md','bytes':len(raw),'sha256':digest,'content_base64':base64.b64encode(raw).decode()}})
        self.assertTrue(result['model_wake_required'])
        with self.r.db() as db:
            content=db.execute('SELECT content FROM codex_events WHERE id=?',(result['event_id'],)).fetchone()[0]
            self.assertNotIn('content_base64',content)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM codex_events WHERE job_id=?',(job,)).fetchone()[0],1)
        self.assertFalse(self.r.codex.worker({'action':'result','id':job,'claim':'receive','result':{}})['accepted'])

    def test_artifact_original_bytes_and_readback_receipt(self):
        job,thread=self.seed_send();raw='完整报告\n'.encode();digest=hashlib.sha256(raw).hexdigest()
        with self.r.db() as db:
            row=db.execute('SELECT * FROM codex_jobs WHERE id=?',(job,)).fetchone()
            result=self.r.artifacts.ingest(db,row,{'path':'C:/report.md','thread_id':thread,'bytes':len(raw),'sha256':digest,'content_base64':base64.b64encode(raw).decode()})
        self.assertNotIn('content_base64',json.dumps(result))
        spec=importlib.util.spec_from_file_location('artifact_cli',Path(__file__).parents[1]/'workspace-tools/task_artifact.py');module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        received=[]
        def rpc(action,data):
            if action=='task_artifact_get':return self.r.artifacts.action(action,data)
            self.assertEqual(data['sha256'],digest);self.assertTrue(data['read_back']);received.append(data);return {'accepted':True}
        root=Path(self.tmp.name)/'workspace';root.mkdir()
        module.receive(job,'state/report.md',workspace=root,call=rpc)
        self.assertEqual((root/'state/report.md').read_bytes(),raw);self.assertEqual(len(received),1)
        self.r.artifacts.action('task_artifact_commit',dict(received[0],path='/var/lib/picoclaw/.picoclaw/workspace/state/report.md'))
        with self.r.db() as db:
            data=json.loads(db.execute('SELECT data FROM loop_tasks WHERE id=?',(self.task['task_id'],)).fetchone()[0])
        self.assertEqual(data['deliveries']['/var/lib/picoclaw/.picoclaw/workspace/state/report.md']['state'],'verified')

    def test_partial_append_restores_source_and_keeps_following_message(self):
        root=Path(self.tmp.name);path=root/'broken.jsonl'
        original={'source_message_id':'2','source_chat_id':'123','role':'user','content':'完整原话，不是摘要'}
        prefix=json.dumps(original,ensure_ascii=False).split('完整原话')[0]+'完整'
        following={'role':'user','content':'下一条完整消息'}
        path.write_bytes(prefix.encode()+json.dumps(following,ensure_ascii=False).encode()+b'\n')
        path.with_suffix('.meta.json').write_text(json.dumps({'count':1,'skip':0,'summary':'原摘要'}))
        result=recover_session(path,root/'backup',lambda header:(original['content'],'stored event'),apply=True)
        records=[json.loads(line)for line in path.read_text(encoding='utf-8').splitlines()]
        self.assertEqual([r['content']for r in records],[original['content'],following['content']])
        self.assertTrue(result['changes'][0]['restored_prefix']);self.assertTrue((root/'backup/broken.jsonl.before.gz').exists())

    def test_file_deliverable_cannot_finish_on_execution_text_alone(self):
        task=self.r.call('task_loop',dict(action='register',goal='交报告',deliverables=['state/report.md'],_context=self.ctx))
        with self.assertRaisesRegex(ValueError,'交付回执'):
            self.r.call('task_loop',dict(action='finish',task_id=task['task_id'],_context=self.ctx))

    def test_truncated_utf8_prefix_uses_recorded_original(self):
        path=Path(self.tmp.name)/'utf8.jsonl'
        prefix=b'{"source_message_id":"2","role":"user","content":"'+('傻鱼'.encode()[:-1])
        path.write_bytes(prefix+b'{"role":"user","content":"next"}\n')
        result=recover_session(path,Path(self.tmp.name)/'backup',lambda header:('傻鱼','recorded user'),True)
        self.assertTrue(result['changes'][0]['restored_prefix'])
        self.assertEqual(json.loads(path.read_bytes().splitlines()[0])['content'],'傻鱼')

    def test_truncated_injected_experience_recovers_original_human_text(self):
        path=Path(self.tmp.name)/'injected.jsonl'
        prefix=json.dumps({'source_kind':'human','source_message_id':'2','role':'user','content':'[当前相关的表达经验]\n截断的内部提示'},ensure_ascii=False).encode()[:-5]
        path.write_bytes(prefix+b'{"role":"user","content":"next"}\n')
        result=recover_session(path,Path(self.tmp.name)/'backup',lambda header:('傻鱼','events:2'),True)
        self.assertEqual(json.loads(path.read_bytes().splitlines()[0])['content'],'傻鱼')
        self.assertEqual(result['changes'][0]['strategy'],'recorded_user_text_without_truncated_injected_context')

    def test_legacy_quota_watch_survives_task_finish_without_reviving_goal(self):
        alarm=self.r.call('silent_alarm',dict(action='create',task='额度监听',purpose='quota_watch',interval_seconds=300,delay_seconds=1,_context=self.bg))
        self.r.call('task_loop',dict(action='finish',task_id=self.task['task_id'],_context=self.ctx))
        with self.r.db() as db:
            row=db.execute('SELECT * FROM silent_alarms WHERE id=?',(alarm['id'],)).fetchone();self.assertEqual(row['status'],'pending')
            wake=self.r._start_alarm_wake(db,row,'q'*24,__import__('datetime').datetime.now(__import__('datetime').timezone.utc))
            self.assertEqual(wake['session_key'],'agent:quota')
        self.assertTrue(self.r.action('loop_context',{'session_key':'agent:quota','message_id':'heartbeat:'+'q'*24})['active'])

    def test_older_human_turn_cannot_reenable_after_newer_cancellation(self):
        self.r.inbound(dict(self.ctx,sender_id='123',message_id='2',text='你把后台闹钟都关了'))
        with self.assertRaises(ValueError):self.r.call('autonomy',dict(action='enable',_context=self.ctx))
        with self.assertRaises(ValueError):self.r.call('task_loop',dict(action='monitor',task_id=self.task['task_id'],enabled=True,_context=self.ctx))

    def test_completed_same_instruction_reuses_result_but_new_instruction_queues(self):
        job,thread=self.seed_send(output={'state':'queued_on_owner','thread_id':'native-thread-123456','terminal_confirmed':True,
            'turn_id':'real-completed-turn','execution_state':'completed','completion_outcome':'succeeded','turn_status':'completed'})
        with patch('codex_bridge.time.sleep'):
            same=self.r.call('codex',dict(action='send',thread_id=thread,text='原始任务',_context=self.bg))
            other=self.r.call('codex',dict(action='send',thread_id=thread,text='补写并同步报告',_context=self.bg))
        self.assertTrue(same['reused_completed_result']);self.assertNotIn('reused_pending_request',same)
        self.assertNotEqual(other['job_id'],job)

    def test_recovery_processes_same_receipt_without_duplicate_send_or_executor_job(self):
        job,thread=self.seed_send(output={'thread_id':'native-thread-123456','turn_id':'actual-turn','terminal_confirmed':True,'execution_state':'completed'})
        with self.r.db() as db:
            row=db.execute('SELECT * FROM codex_jobs WHERE id=?',(job,)).fetchone();event=self.r.codex.enqueue_event(db,row,'actual-turn','completed','报告完成')
        context=dict(self.bg,message_id='codex-event:'+event);self.r.telegram=Mock(return_value={'message_id':990})
        for attempt in range(2):
            with self.r.db() as db:db.execute('UPDATE codex_events SET claimed_until=0 WHERE id=?',(event,))
            receipt=self.r.codex.runtime_action('codex_event_poll',{})['event'];claim=receipt['claim']
            self.r.codex.runtime_action('codex_event_start',{'event_id':event,'claim':claim})
            self.r.call('send_message',dict(text='报告已交付',_context=context))
            self.r.codex.runtime_action('codex_event_ack',{'event_id':event,'claim':claim,'outcome':'error'if attempt==0 else'completed'})
        self.assertEqual(self.r.telegram.call_count,1)
        with self.r.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM codex_jobs').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT execution_state,receipt_state FROM codex_chains WHERE id=?',(job,)).fetchone()[:],('completed','processed'))
        self.assertIsNone(self.r.codex.runtime_action('codex_event_poll',{})['event'])

    def test_new_human_goal_on_same_chat_registers_its_own_task(self):
        self.seed_send();self.r.inbound(dict(self.ctx,sender_id='123',message_id='2',text='新任务：研究另一个项目'))
        with patch('codex_bridge.time.sleep'):
            result=self.r.call('codex',dict(action='send',thread_id='native-thread-123456',text='研究另一个项目',_context=dict(self.ctx,message_id='2')))
        self.assertNotEqual(result['task_id'],self.task['task_id']);self.assertNotEqual(result['chain_id'],'request-one')

    def test_human_supplement_updates_current_task_version_before_dispatch(self):
        self.seed_send();self.r.inbound(dict(self.ctx,sender_id='123',message_id='2',text='补充要求：报告保留原文'))
        with patch('codex_bridge.time.sleep'):
            result=self.r.call('codex',dict(action='continue',thread_id='native-thread-123456',text='报告保留原文',_context=dict(self.ctx,message_id='2')))
        self.assertEqual(result['task_id'],self.task['task_id'])
        state=self.r.action('loop_context',{'session_key':'agent:task-'+self.task['task_id']})
        self.assertEqual(state['version'],2);self.assertIn('报告保留原文',state['context'])

    def test_fresh_read_of_handed_back_task_is_a_query_not_a_resume(self):
        self.seed_send();self.r.inbound(dict(self.ctx,sender_id='123',message_id='2',text='剩下的给我就行'))
        self.r.inbound(dict(self.ctx,sender_id='123',message_id='3',text='看一下刚才的执行状态'))
        with self.r.db() as db:self.r._set(db,'codex_worker_seen',time.time())
        with patch('codex_bridge.time.sleep'):
            result=self.r.call('codex',dict(action='read',thread_id='native-thread-123456',_context=dict(self.ctx,message_id='3')))
        self.assertNotIn('task_id',result)
        with self.r.db() as db:
            self.assertIsNone(db.execute("SELECT task_id FROM loop_bindings WHERE kind='codex' AND key=?",(result['job_id'],)).fetchone())
            self.assertEqual(json.loads(db.execute('SELECT data FROM loop_tasks WHERE id=?',(self.task['task_id'],)).fetchone()[0])['status'],'handed_back')

if __name__=='__main__':unittest.main(verbosity=2)
