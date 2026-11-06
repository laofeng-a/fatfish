import importlib.util,unittest
from pathlib import Path
import io,json,queue,tempfile,os
from unittest.mock import Mock,patch
spec=importlib.util.spec_from_file_location('worker',Path(__file__).with_name('worker.py'));worker=importlib.util.module_from_spec(spec);spec.loader.exec_module(worker)

class WorkerTests(unittest.TestCase):
    def test_existing_codex_executable_is_kept(self):
        with tempfile.TemporaryDirectory() as directory:
            binary=Path(directory)/'custom.exe';binary.touch()
            self.assertEqual(worker.resolve_codex_executable(str(binary)),str(binary))
    def test_missing_versioned_executable_uses_newest_desktop_cache(self):
        with tempfile.TemporaryDirectory() as directory,patch.dict(worker.os.environ,{'LOCALAPPDATA':directory}):
            root=Path(directory)/'OpenAI'/'Codex'/'bin'
            for version,stamp in [('old',100),('current',200)]:
                binary=root/version/'codex.exe';binary.parent.mkdir(parents=True);binary.touch();os.utime(binary,(stamp,stamp))
            self.assertEqual(worker.resolve_codex_executable(str(root/'removed'/'codex.exe')),str(root/'current'/'codex.exe'))
    def test_path_fallback_and_missing_binary_are_explicit(self):
        with tempfile.TemporaryDirectory() as directory,patch.dict(worker.os.environ,{'LOCALAPPDATA':directory}):
            with patch.object(worker.shutil,'which',return_value='installed-codex.exe'):
                self.assertEqual(worker.resolve_codex_executable('removed.exe'),'installed-codex.exe')
            with patch.object(worker.shutil,'which',return_value=None),self.assertRaises(FileNotFoundError):
                worker.resolve_codex_executable('removed.exe')
    def app(self):
        app=worker.AppServer.__new__(worker.AppServer);app.active={};app.final={};app.routes={};app.routes_file=None;app.writer_strategy='queue'
        app.rpc=Mock();app.usage_cache=None;app.read_usage=Mock(return_value={'ordinary_usage_allowed':True,'limits':[]});return app
    def test_create_uses_new_thread_and_durable_retry_receipt(self):
        app=self.app()
        with tempfile.TemporaryDirectory() as directory:
            app.rpc.side_effect=[{'thread':{'id':'new-native-thread'}},{}]
            job={'id':'create12345678','action':'create','cwd':directory,'title':'独立任务'}
            result=app.execute(job);again=app.execute(job)
        self.assertEqual(result['state'],'created');self.assertEqual(again['thread_id'],result['thread_id'])
        self.assertTrue(again['already_created']);self.assertEqual(app.rpc.call_count,2)
        self.assertEqual(app.rpc.call_args_list[0].args[0],'thread/start')
        self.assertNotIn('model',app.rpc.call_args_list[0].args[1])
    def test_create_rejects_missing_directory_before_rpc(self):
        app=self.app()
        with self.assertRaises(ValueError):app.execute({'id':'create12345678','action':'create','cwd':'relative-path'})
        app.rpc.assert_not_called()
    def test_create_inherits_working_directory_without_copying_history(self):
        app=self.app()
        with tempfile.TemporaryDirectory() as directory:
            app.rpc.side_effect=[{'thread':{'cwd':directory}},{'thread':{'id':'new-native-thread'}},{}]
            result=app.execute({'id':'create12345678','action':'create','source_thread_id':'original-thread'})
        self.assertEqual(result['thread_id'],'new-native-thread')
        self.assertEqual([c.args[0] for c in app.rpc.call_args_list],['thread/read','thread/start','thread/name/set'])
    def test_list_returns_small_metadata_only(self):
        app=self.app();app.rpc.return_value={'data':[{'id':'thread','name':'test','cwd':'C:/test','status':{'type':'idle'},'secret_extra':'not exported'}]}
        result=app.execute({'action':'list','query':'test'})
        self.assertNotIn('secret_extra',result['threads'][0]);self.assertEqual(app.rpc.call_args.args,('thread/list',{'limit':8,'archived':False,'searchTerm':'test'}))
    def test_list_includes_all_registered_native_chats_and_removes_duplicates(self):
        app=self.app();app.routes={
            'main':{'thread_id':'main','name':'配置大肥鱼','native':True,'shared':True},
            'second':{'thread_id':'second','name':'配置大肥鱼2','native':True,'shared':True},
        }
        def rpc(method,args):
            if method=='thread/list':return {'data':[{'id':'second','name':'配置大肥鱼2'},{'id':'other','name':'其他'},{'id':'other','name':'其他'}],'nextCursor':'more'}
            if method=='thread/read':return {'thread':{'id':args['threadId'],'name':'配置大肥鱼','status':{'type':'idle'}}}
            self.fail(method)
        app.rpc.side_effect=rpc
        result=app.execute({'action':'list','query':''})
        rows={row['id']:row for row in result['threads']}
        self.assertEqual(len(result['threads']),3)
        self.assertEqual(set(rows),{'main','second','other'})
        self.assertTrue(rows['main']['native']);self.assertTrue(rows['second']['native'])
        self.assertEqual(result['default_thread_id'],'second');self.assertTrue(result['recent_results_have_more'])
    def test_list_search_includes_matching_registered_native_chats(self):
        app=self.app();app.routes={
            'main':{'thread_id':'main','name':'配置大肥鱼','native':True,'shared':True},
            'second':{'thread_id':'second','name':'配置大肥鱼2','native':True,'shared':True},
            'third':{'thread_id':'third','name':'其他','native':True,'shared':True},
        }
        app.rpc.side_effect=lambda method,args: {'data':[]} if method=='thread/list' else {'thread':{'id':args['threadId'],'name':app.routes[args['threadId']]['name']}}
        result=app.execute({'action':'list','query':'配置大肥鱼'})
        self.assertEqual({row['id'] for row in result['threads']},{'main','second'})
        self.assertEqual([call.args[0] for call in app.rpc.call_args_list],['thread/list','thread/read','thread/read'])
    def test_list_reports_unavailable_native_without_hiding_other_chats(self):
        app=self.app();app.routes={'lost':{'thread_id':'lost','name':'旧对话','native':True,'shared':True}}
        def rpc(method,args):
            if method=='thread/list':return {'data':[{'id':'other','name':'其他'}]}
            raise RuntimeError('not found')
        app.rpc.side_effect=rpc
        result=app.execute({'action':'list'})
        self.assertEqual([row['id'] for row in result['threads']],['other'])
        self.assertEqual(result['unavailable_native_count'],1)
    def test_send_continues_existing_thread_without_permission_changes(self):
        app=self.app();app.routes={'owned-job':{'thread_id':'abc','created_by_fatfish':True}};app.rpc.side_effect=[{'thread':{'id':'abc','status':{'type':'notLoaded'}}},{'thread':{'id':'abc','status':{'type':'idle'}}},{'turn':{'id':'turn1'}}]
        result=app.execute({'action':'send','thread_id':'abc','text':'继续'})
        self.assertEqual(result['turn_id'],'turn1')
        params=app.rpc.call_args.args[1];self.assertEqual(params['threadId'],'abc');self.assertNotIn('approvalPolicy',params);self.assertNotIn('sandboxPolicy',params)
    def test_active_turn_uses_steer_and_stop(self):
        app=self.app();app.active['abc']='turn1';app.rpc.return_value={'thread':{'status':{'type':'active'}}}
        app.execute({'action':'send','thread_id':'abc','text':'换个方向'})
        self.assertEqual(app.rpc.call_args.args[0],'turn/steer')
        app.execute({'action':'stop','thread_id':'abc'})
        self.assertEqual(app.rpc.call_args.args,('turn/interrupt',{'threadId':'abc','turnId':'turn1'}))
    def test_stop_idle_does_not_create_work(self):
        app=self.app();app.routes={'owned-job':{'thread_id':'abc','created_by_fatfish':True}};app.rpc.return_value={'thread':{'status':{'type':'idle'}}}
        self.assertEqual(app.execute({'action':'stop','thread_id':'abc'})['state'],'already_idle');self.assertEqual(app.rpc.call_count,1)
    def test_desktop_writer_conflict_queues_in_original_thread_without_starting_or_forking(self):
        app=self.app();app.rpc.side_effect=[{'thread':{'id':'abc','status':{'type':'notLoaded'}}},{'data':[]},{'queuedSubmission':{'id':'queued1'}}]
        result=app.execute({'id':'job123','action':'send','thread_id':'abc','text':'继续做这个任务'})
        self.assertEqual(result['state'],'queued_on_owner');self.assertFalse(result['started']);self.assertNotIn('turn_id',result)
        self.assertEqual(result['thread_id'],'abc');self.assertEqual(result['queued_submission_id'],'queued1')
        method,params=app.rpc.call_args.args;self.assertEqual(method,'thread/queue/add');self.assertEqual(params['threadId'],'abc')
        self.assertEqual(params['clientUserMessageId'],'fatfish-job123');self.assertEqual(params['input'][0]['text'],'继续做这个任务')
        self.assertNotIn('thread/fork',[x.args[0] for x in app.rpc.call_args_list]);self.assertNotIn('turn/start',[x.args[0] for x in app.rpc.call_args_list])
        self.assertNotIn('thread/resume',[x.args[0] for x in app.rpc.call_args_list]);self.assertTrue(app.routes['abc']['native'])
    def test_writer_conflict_can_stop_the_active_desktop_turn(self):
        app=self.app();app.rpc.side_effect=[{'thread':{'status':{'type':'notLoaded'}}},{'data':[]},{'data':[{'id':'desktop-turn','status':'inProgress','items':[{'type':'userMessage','clientId':'desktop-user'}]}]},{}]
        result=app.execute({'action':'stop','thread_id':'abc'})
        self.assertTrue(result['stopped']);self.assertEqual(result['state'],'interruption_requested')
        self.assertEqual(app.rpc.call_args.args,('turn/interrupt',{'threadId':'abc','turnId':'desktop-turn'}))
    def test_fork_strategy_creates_independent_thread_without_permission_override(self):
        app=self.app();app.writer_strategy='fork'
        app.rpc.side_effect=[{'thread':{'id':'source','cwd':'C:/project','name':'项目','status':{'type':'notLoaded'}}},
            RuntimeError('thread source already has an active writer'),{'data':[{'id':'done1','status':'completed'}]},
            {'thread':{'id':'remote'}},{},{'turn':{'id':'turn1'}}]
        result=app.execute({'id':'job1','action':'send','thread_id':'source','text':'检查问题'})
        self.assertTrue(result['remote_session_created']);self.assertEqual(result['thread_id'],'remote');self.assertEqual(result['source_thread_id'],'source')
        params=app.rpc.call_args_list[3].args[1]
        self.assertEqual(params,{'threadId':'source','excludeTurns':True,'deferGoalContinuation':True,'lastTurnId':'done1'})
        self.assertEqual(app.routes['source']['thread_id'],'remote')
        self.assertNotIn('sandbox',params);self.assertNotIn('approvalPolicy',params)
        self.assertEqual(app.rpc.call_args.args,('turn/start',{'threadId':'remote','input':[{'type':'text','text':'检查问题'}]}))
    def test_later_send_and_stop_reuse_remote_route(self):
        app=self.app();app.routes={'source':{'thread_id':'remote'}}
        app.rpc.side_effect=[{'thread':{'status':{'type':'idle'}}},{'turn':{'id':'turn1'}}]
        result=app.execute({'action':'send','thread_id':'source','text':'接着做'})
        self.assertEqual(result['thread_id'],'remote');self.assertEqual(app.rpc.call_args_list[0].args,('thread/read',{'threadId':'remote'}))
        app.rpc=Mock(return_value={'thread':{'status':{'type':'idle'}}})
        result=app.execute({'action':'stop','thread_id':'source'});self.assertEqual(result['thread_id'],'remote')
    def test_attach_native_never_resumes_or_forks(self):
        app=self.app();app.rpc.return_value={'thread':{'id':'source-thread','name':'原生','status':{'type':'notLoaded'}}}
        result=app.attach_shared('source-thread')
        self.assertTrue(result['native']);self.assertEqual(result['thread_id'],'source-thread')
        self.assertEqual([x.args[0] for x in app.rpc.call_args_list],['thread/read'])
    def test_native_send_only_adds_to_desktop_queue_and_preserves_model(self):
        app=self.app();app.routes={'source-thread':{'thread_id':'source-thread','native':True,'shared':True}}
        app.rpc.side_effect=[{'thread':{'id':'source-thread','status':{'type':'notLoaded'}}},{'data':[]},{'queuedSubmission':{'id':'q1'}}]
        result=app.execute({'id':'job1','action':'send','thread_id':'source-thread','text':'继续'})
        self.assertEqual(result['state'],'queued_on_owner');self.assertFalse(result['started'])
        calls=[x.args[0] for x in app.rpc.call_args_list]
        self.assertNotIn('thread/resume',calls);self.assertNotIn('thread/fork',calls)
        self.assertNotIn('model',app.rpc.call_args.args[1])
    def test_read_original_follows_shared_history(self):
        app=self.app();app.routes={'source':{'thread_id':'remote','shared':True}}
        app.rpc.side_effect=[{'thread':{'id':'remote','status':{'type':'idle'}}},{'data':[]},{'data':[]}]
        result=app.execute({'action':'read','thread_id':'source'})
        self.assertEqual(result['thread_id'],'remote');self.assertEqual(result['shared_thread_id'],'remote')
        self.assertEqual(app.rpc.call_args_list[1].args[1]['threadId'],'remote')
    def test_routes_survive_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            app=self.app();app.routes_file=Path(folder)/'routes.json';app.routes={'source-thread-123':{'thread_id':'remote-thread-456'}}
            app.save_routes();other=self.app();other.routes_file=app.routes_file
            self.assertEqual(other.load_routes(),app.routes)
    def test_other_resume_errors_are_not_mistaken_for_writer_conflicts(self):
        app=self.app();app.routes={'owned-job':{'thread_id':'abc','created_by_fatfish':True}};app.rpc.side_effect=[{'thread':{'status':{'type':'notLoaded'}}},RuntimeError('permission denied')]
        with self.assertRaisesRegex(RuntimeError,'permission denied'):app.execute({'action':'send','thread_id':'abc','text':'继续'})
        self.assertEqual(app.rpc.call_count,2)
    def test_each_new_desktop_thread_is_auto_registered_without_writer_acquisition(self):
        app=self.app()
        for identifier in ('first-desktop-thread','second-desktop-thread'):
            app.rpc.side_effect=[{'thread':{'id':identifier,'name':'新桌面对话','status':{'type':'notLoaded'}}},{'data':[]},{'queuedSubmission':{'id':'q-'+identifier}}]
            result=app.execute({'id':'send-'+identifier,'action':'send','thread_id':identifier,'text':'继续'})
            self.assertEqual(result['thread_id'],identifier);self.assertEqual(result['state'],'queued_on_owner')
            self.assertTrue(app.routes[identifier]['native'])
        methods=[x.args[0] for x in app.rpc.call_args_list]
        self.assertEqual(methods,['thread/read','thread/queue/list','thread/queue/add']*2)
    def test_auto_registered_thread_persists_across_restart(self):
        app=self.app()
        with tempfile.TemporaryDirectory() as directory:
            app.routes_file=Path(directory)/'routes.json'
            app.rpc.side_effect=[{'thread':{'id':'new-desktop-thread'}},{'data':[]},{'queuedSubmission':{'id':'q'}}]
            app.execute({'id':'job-new','action':'send','thread_id':'new-desktop-thread','text':'继续'})
            other=self.app();other.routes_file=app.routes_file;other.routes=other.load_routes()
            self.assertTrue(other.routes['new-desktop-thread']['native'])
    def test_queue_client_id_is_stable_for_retry_and_distinct_between_jobs(self):
        app=self.app();app.rpc.return_value={'queuedSubmission':{'id':'queued1'}}
        for identifier in ('job1','job1','job2'):app.queue_on_owner({'id':identifier,'text':'继续'},'abc')
        values=[x.args[1]['clientUserMessageId'] for x in app.rpc.call_args_list if x.args[0]=='thread/queue/add']
        self.assertEqual(values[0],values[1]);self.assertNotEqual(values[0],values[2])
    def test_existing_queued_job_is_returned_without_another_add(self):
        app=self.app();app.rpc.return_value={'data':[{'id':'queue1','clientUserMessageId':'fatfish-job1'}]}
        result=app.queue_on_owner({'id':'job1','text':'继续'},'abc')
        self.assertTrue(result['already_queued']);self.assertEqual(result['queued_submission_id'],'queue1')
        self.assertEqual(app.rpc.call_count,1);self.assertEqual(app.rpc.call_args.args[0],'thread/queue/list')
    def test_chinese_history_stays_below_server_result_byte_limit(self):
        app=self.app();items=[{'type':'agentMessage','text':'鲸'*3000} for _ in range(8)]
        app.rpc.side_effect=[{'thread':{'status':{'type':'idle'}}},{'data':[{'items':items} for _ in range(3)]},{'data':[]}]
        value=app.execute({'action':'read','thread_id':'abc'})
        self.assertLess(len(json.dumps(value,ensure_ascii=False).encode()),32000)
        self.assertLessEqual(sum(len(x['text']) for t in value['turns'] for x in t['messages']),6000)
        self.assertTrue(all(x['truncated'] for t in value['turns'] for x in t['messages']))
    def test_read_shows_pending_messages_separately_from_executed_turns(self):
        app=self.app();app.rpc.side_effect=[{'thread':{'status':{'type':'notLoaded'}}},{'data':[]},
            {'data':[{'id':'q1','clientUserMessageId':'fatfish-job1','input':[{'type':'text','text':'鲸'*2000}]}]}]
        result=app.execute({'action':'read','thread_id':'abc'})
        self.assertEqual(result['turns'],[]);self.assertEqual(len(result['pending_messages'][0]['text']),500)
        self.assertEqual(result['pending_messages'][0]['id'],'q1')
    def test_ssh_never_exposes_token_in_command_or_allows_forwarding(self):
        process=Mock();process.stdout=io.StringIO('{"id":1,"result":{"job":null}}\n');process.stdin=io.StringIO()
        config={'ssh':'ssh','key':'key','destination':'user@host'}
        with patch.object(worker.subprocess,'Popen',return_value=process) as spawn:
            bridge=worker.SshBridge(config,'secret-token');result=bridge.post({'action':'poll'})
        command=spawn.call_args.args[0]
        self.assertNotIn('-L',command);self.assertNotIn('-R',command);self.assertNotIn('secret-token',' '.join(command))
        self.assertEqual(command[-1],'fatfish-codex-bridge');self.assertEqual(result,{'job':None})
        self.assertIn('IdentitiesOnly=yes',command)
    def test_mismatched_ssh_response_cannot_be_used_as_another_job_result(self):
        bridge=worker.SshBridge.__new__(worker.SshBridge);bridge.counter=0;bridge.token='secret';bridge.process=Mock();bridge.responses=queue.Queue()
        bridge.responses.put({'id':9,'result':{'ok':True}})
        with self.assertRaises(RuntimeError):bridge.post({'action':'poll'})

    def test_existing_ssh_alias_uses_only_restricted_key_and_keeps_network_config(self):
        process=Mock();process.stdout=io.StringIO('');process.stdin=io.StringIO()
        resolved=Mock(stdout='hostname sg.example.test\nuser owner\nport 22\nidentityfile admin-key\nproxycommand none\n')
        config={'ssh':'ssh','key':'bridge-key','destination':'obsolete-host','ssh_alias':'sg',
                'proxy_command':'obsolete-proxy','host_key_alias':'obsolete-key-alias'}
        with patch.object(worker.subprocess,'run',return_value=resolved) as resolve, patch.object(worker.subprocess,'Popen',return_value=process) as spawn:
            worker.SshBridge(config,'secret')
        self.assertEqual(resolve.call_args.args[0],['ssh','-G','sg'])
        command=spawn.call_args.args[0]
        self.assertEqual(command[-2:],[ 'owner@sg.example.test','fatfish-codex-bridge'])
        self.assertIn('none',command);self.assertIn('bridge-key',command)
        self.assertNotIn('admin-key',command);self.assertNotIn('obsolete-proxy',' '.join(command))
        self.assertEqual(config['destination'],'obsolete-host')

if __name__=='__main__':unittest.main()
