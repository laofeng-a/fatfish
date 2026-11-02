import copy,unittest
from unittest.mock import Mock
from test_worker import worker

class UsageTests(unittest.TestCase):
    def app(self):
        app=worker.AppServer.__new__(worker.AppServer)
        app.rpc=Mock();app.usage_cache=None;app.watcher_seen={}
        return app
    def test_multibucket_nullable_and_credential_fields_are_filtered(self):
        raw={'ordinaryUsageAllowed':False,'accountId':'DO-NOT-EXPORT','rateLimitResetCredits':{'secret':'hidden'},
             'rateLimits':{'limitId':'legacy'},'rateLimitsByLimitId':{'core':{'limitId':'core','primary':{'usedPercent':46,'windowDurationMins':300,'resetsAt':123,'token':'hidden'},'secondary':None},'extra':{'primary':{'usedPercent':None}}}}
        got=worker.AppServer.sanitize_usage(raw,'observed')
        self.assertEqual(len(got['limits']),2);self.assertFalse(got['ordinary_usage_allowed'])
        self.assertEqual(got['limits'][0]['primary']['remaining_percent'],54)
        self.assertIsNone(got['limits'][0]['secondary']);self.assertIsNone(got['limits'][1]['primary']['remaining_percent'])
        self.assertNotIn('DO-NOT-EXPORT',str(got));self.assertNotIn('secret',str(got));self.assertNotIn('token',str(got))
    def test_missing_and_nonfinite_are_unknown_remaining_is_clamped(self):
        for used,want in [(None,None),(True,None),(float('nan'),None),(float('inf'),None),(110,0),(-5,100)]:
            got=worker.AppServer.sanitize_usage({'rateLimits':{'primary':{'usedPercent':used}}})
            self.assertEqual(got['limits'][0]['primary']['remaining_percent'],want)
            self.assertIsNone(got['ordinary_usage_allowed'])
    def test_read_is_readonly_and_cached_for_120_seconds(self):
        app=self.app();app.rpc.return_value={'ordinaryUsageAllowed':True,'rateLimits':{'primary':{'usedPercent':12}}}
        first=app.read_usage();second=app.read_usage()
        self.assertEqual(first,second);self.assertEqual(app.rpc.call_count,1)
        self.assertEqual(app.rpc.call_args.args,('account/rateLimits/read',{}))
        app.read_usage(force=True);self.assertEqual(app.rpc.call_count,2)
    def test_failure_does_not_expose_error_or_cache_bad_data(self):
        app=self.app();app.rpc.side_effect=RuntimeError('secret-token-and-account')
        with self.assertRaises(RuntimeError) as err:app.read_usage()
        self.assertNotIn('secret',str(err.exception));self.assertIsNone(app.usage_cache)
    def test_usage_snapshot_does_not_veto_requested_submission(self):
        app=self.app();app.routes={};app.active={};app.final={}
        app.rpc.side_effect=[{'thread':{'id':'target','status':{'type':'idle'}}},{'ordinaryUsageAllowed':False},{'turn':{'id':'actual-turn'}}]
        got=app.execute({'action':'send','thread_id':'target','text':'work'})
        self.assertEqual(got['turn_id'],'actual-turn')
        self.assertEqual([c.args[0] for c in app.rpc.call_args_list],['thread/read','account/rateLimits/read','turn/start'])
    def test_native_completion_uses_matching_client_and_final_only(self):
        app=self.app();app.rpc.side_effect=[{'data':[]},{'data':[{'id':'turn-one','status':'completed','items':[
            {'type':'userMessage','clientId':'fatfish-job-one'},
            {'type':'agentMessage','phase':'commentary','text':'progress'},
            {'type':'agentMessage','phase':'final_answer','text':'finished'}]}]}]
        got=app.inspect_submission('thread','fatfish-job-one')
        self.assertEqual(got['text'],'finished');self.assertEqual(got['turn_id'],'turn-one')
        self.assertEqual([c.args[0] for c in app.rpc.call_args_list],['thread/queue/list','thread/turns/list'])
    def test_watcher_reports_completion_and_throttles_repeated_reads(self):
        app=self.app();app.inspect_submission=Mock(return_value={'state':'completed','turn_id':'turn-one','status':'completed','text':'done','terminal_confirmed':True})
        watch={'job_id':'job-one','thread_id':'thread','client_user_message_id':'fatfish-job-one','created':0}
        event=app.poll_watchers([watch]);self.assertEqual(event[0]['action'],'completed')
        self.assertEqual(event[0]['client_user_message_id'],'fatfish-job-one')
        self.assertEqual(app.poll_watchers([watch]),[]);self.assertEqual(app.inspect_submission.call_count,1)
    def test_watcher_unknown_never_resubmits(self):
        app=self.app();app.inspect_submission=Mock(return_value={'state':'unknown'})
        events=app.poll_watchers([{'job_id':'job-one','thread_id':'thread','created':0}])
        self.assertEqual(events[0]['action'],'unknown');app.rpc.assert_not_called()

if __name__=='__main__':unittest.main()
