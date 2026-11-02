"""No real Codex, SSH or Telegram calls. Queue authority and replay checks."""
import importlib.util,json,threading,time,unittest
from pathlib import Path
from unittest.mock import Mock,patch
spec=importlib.util.spec_from_file_location('bridge_base',Path(__file__).with_name('test-fatfish.py'))
base=importlib.util.module_from_spec(spec);spec.loader.exec_module(base)

class BridgeTests(unittest.TestCase):
    setUp=base.PolicyTests.setUp
    bind_group=base.PolicyTests.bind_group
    def call(self,args,context=None):return self.r.call('codex',dict(args,_context=context or self.context))
    def test_offline_never_claims_it_submitted(self):
        self.assertFalse(self.call({'action':'status'})['online'])
        self.assertEqual(self.call({'action':'send','thread_id':'0123456789abcdef','text':'do it'})['state'],'offline')
        with self.r.db() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM codex_jobs').fetchone()[0],0)
    def test_only_owner_private_can_control_and_background_cannot(self):
        self.bind_group()
        for ctx in (dict(self.context,chat_id='-99'),dict(self.context,session_key='heartbeat'),dict(self.context,chat_id='456')):
            with self.assertRaises(ValueError):self.call({'action':'status'},ctx)
    def test_worker_poll_result_and_same_turn_deduplicate(self):
        self.r.codex.worker({'action':'poll'})
        results=[]
        thread=threading.Thread(target=lambda:results.append(self.call({'action':'list'})));thread.start()
        job=None
        for _ in range(30):
            job=self.r.codex.worker({'action':'poll'})['job']
            if job:break
            time.sleep(.02)
        self.assertIsNotNone(job);self.assertEqual(job['action'],'list')
        self.assertFalse(self.r.codex.worker({'action':'result','id':job['id'],'claim':'wrong','result':{}})['accepted'])
        self.assertTrue(self.r.codex.worker({'action':'result','id':job['id'],'claim':job['claim'],'result':{'threads':[]}})['accepted'])
        thread.join(3);self.assertFalse(thread.is_alive());self.assertEqual(results[0]['state'],'done')
        self.assertEqual(self.call({'action':'list'})['job_id'],job['id'])
        self.assertIsNone(self.r.codex.worker({'action':'poll'})['job'])
    def test_expired_claim_never_replays_a_send(self):
        with self.r.db() as db:db.execute('INSERT INTO codex_jobs VALUES(?,?,?,?,?,?,?,?,?,?)',('job','1','fp','send','{}','claimed','secret',1,1,''))
        self.assertIsNone(self.r.codex.worker({'action':'poll'})['job'])
        self.assertEqual(self.call({'action':'status','job_id':'job'})['state'],'unknown')
    def test_worker_result_has_fixed_size_and_redacts_credentials(self):
        with self.r.db() as db:db.execute('INSERT INTO codex_jobs VALUES(?,?,?,?,?,?,?,?,?,?)',('job','1','fp','read','{}','claimed','secret',time.time(),time.time(),''))
        with self.assertRaises(ValueError):self.r.codex.worker({'action':'result','id':'job','claim':'secret','result':{'x':'鲸'*20000}})
        self.r.codex.worker({'action':'result','id':'job','claim':'secret','result':{'text':'sk-'+'a'*32}})
        self.assertNotIn('sk-',json.dumps(self.call({'action':'status','job_id':'job'})))
    def test_completion_must_match_real_send_job_and_is_delivered_once(self):
        self.r.telegram=Mock(return_value={'message_id':9})
        with self.r.db() as db:db.execute('INSERT INTO codex_jobs VALUES(?,?,?,?,?,?,?,?,?,?)',('job','1','fp','send','{}','done','secret',1,1,json.dumps({'turn_id':'turn123'})))
        event={'action':'completed','job_id':'job','turn_id':'turn123','status':'completed','text':'修好了'}
        self.assertFalse(self.r.codex.worker(dict(event,turn_id='wrongturn'))['accepted'])
        self.assertTrue(self.r.codex.worker(event)['accepted']);self.r.codex.worker(event)
        self.assertEqual(self.r.telegram.call_count,1)
        self.assertEqual(self.call({'action':'status','job_id':'job'})['result']['final'],'修好了')
    def test_no_raw_shell_or_permission_override_action(self):
        for action in ('exec','shell','sudo','start','change_permissions'):
            with self.assertRaises(ValueError):self.call({'action':action})
    def test_status_reports_actual_worker_queue_capability(self):
        self.r.codex.worker({'action':'poll','protocol_version':2})
        self.assertTrue(self.call({'action':'status'})['supports_desktop_queue'])
    def test_protocol_four_advertises_shared_control(self):
        self.r.codex.worker({'action':'poll','protocol_version':4})
        status=self.call({'action':'status'})
        self.assertTrue(status['supports_shared_control']);self.assertTrue(status['supports_independent_remote_session'])
    def test_multiple_requests_for_same_turn_all_receive_result_with_one_notification(self):
        self.r.telegram=Mock(return_value={'message_id':9})
        with self.r.db() as db:
            for job in ('first','followup'):
                db.execute('INSERT INTO codex_jobs VALUES(?,?,?,?,?,?,?,?,?,?)',(job,job,job,'send','{}','done','secret',1,1,json.dumps({'turn_id':'sharedturn123'})))
        for job in ('first','followup'):
            self.r.codex.worker({'action':'completed','job_id':job,'turn_id':'sharedturn123','status':'completed','text':'共同任务完成'})
            self.assertEqual(self.call({'action':'status','job_id':job})['result']['final'],'共同任务完成')
        self.assertEqual(self.r.telegram.call_count,1)
    def test_internal_probe_completion_does_not_send_a_telegram_message(self):
        self.r.telegram=Mock()
        with self.r.db() as db:db.execute('INSERT INTO codex_jobs VALUES(?,?,?,?,?,?,?,?,?,?)',('probe','maintenance','fp','probe','{}','done','secret',1,1,json.dumps({'turn_id':'probe123'})))
        result=self.r.codex.worker({'action':'completed','job_id':'probe','turn_id':'probe123','status':'completed','text':'连接成功'})
        self.assertTrue(result['probe']);self.r.telegram.assert_not_called()
        self.assertEqual(self.call({'action':'status','job_id':'probe'})['result']['final'],'连接成功')
    def test_status_does_not_reinject_all_old_read_results(self):
        with self.r.db() as db:db.execute('INSERT INTO codex_jobs VALUES(?,?,?,?,?,?,?,?,?,?)',('read','1','fp','read','{}','done','secret',1,1,json.dumps({'text':'x'*30000})))
        status=self.call({'action':'status'})
        self.assertLess(len(json.dumps(status)),2000);self.assertNotIn('result',status['jobs'][0])
    def test_failed_notification_keeps_codex_result_without_blind_reposting(self):
        self.r.telegram=Mock(side_effect=TimeoutError())
        with self.r.db() as db:db.execute('INSERT INTO codex_jobs VALUES(?,?,?,?,?,?,?,?,?,?)',('job','1','fp','send','{}','done','secret',1,1,json.dumps({'turn_id':'turn123'})))
        event={'action':'completed','job_id':'job','turn_id':'turn123','status':'completed','text':'任务完成'}
        result=self.r.codex.worker(event);self.assertEqual(result['notification_status'],'unknown')
        self.r.codex.worker(event);self.assertEqual(self.r.telegram.call_count,1)
        saved=self.call({'action':'status','job_id':'job'})['result']
        self.assertEqual(saved['final'],'任务完成');self.assertEqual(saved['notification_status'],'unknown')
    def test_active_rpc_heartbeat_renews_claim_without_claiming_another_job(self):
        with self.r.db() as db:
            for identifier,state,claim in [('active','claimed','known'),('waiting','queued','')]:
                db.execute('INSERT INTO codex_jobs VALUES(?,?,?,?,?,?,?,?,?,?)',(identifier,identifier,identifier,'list','{"action":"list"}',state,claim,time.time(),1,''))
        self.assertIsNone(self.r.codex.worker({'action':'poll','active_id':'active','claim':'wrong'})['job'])
        with self.r.db() as db:self.assertEqual(db.execute("SELECT updated FROM codex_jobs WHERE id='active'").fetchone()[0],1)
        self.r.codex.worker({'action':'poll','active_id':'active','claim':'known'})
        with self.r.db() as db:self.assertGreater(db.execute("SELECT updated FROM codex_jobs WHERE id='active'").fetchone()[0],1)
        self.assertEqual(self.r.codex.worker({'action':'poll'})['job']['id'],'waiting')

if __name__=='__main__':unittest.main()
