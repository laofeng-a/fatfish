import io,json,tempfile,unittest,uuid
from pathlib import Path
from unittest.mock import patch
from claude_connector import ClaudeExecutor
class Process:
    pid=12345
    def __init__(self,rows):self.stdin=io.StringIO();self.stdout=iter(json.dumps(x)+'\n' for x in rows)
    def wait(self):return 0
    def poll(self):return 0
class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name);self.projects=self.root/'projects';self.projects.mkdir();self.sid=str(uuid.uuid4());self.e=ClaudeExecutor(self.root/'worker','claude.exe',self.projects)
        self.e.routes[self.sid]={'cwd':str(self.root),'name':'旧会话'}
    def test_real_transcript_text_is_read_and_thinking_is_not_returned(self):
        folder=self.projects/'project';folder.mkdir();(folder/(self.sid+'.jsonl')).write_text(json.dumps({'type':'assistant','message':{'content':[{'type':'thinking','thinking':'private'},{'type':'text','text':'真实答复🐟'}]}},ensure_ascii=False)+'\n',encoding='utf-8')
        result=self.e.read(self.sid);self.assertEqual(result['messages'][0]['text'],'真实答复🐟');self.assertEqual(result['messages'][0]['phase'],'final_answer')
    def test_repeated_admission_does_not_repeat_work(self):
        with patch.object(self.e,'advance'):
            request={'action':'send','thread_id':self.sid,'id':'job1','text':'readme'};self.e.execute(request);self.assertTrue(self.e.execute(request)['already_submitted'])
        self.assertEqual(len(self.e.jobs),1)
    def test_error_result_success_subtype_is_still_failed(self):
        self.e.jobs['job1']={'thread_id':self.sid,'prompt':'hello','state':'starting'}
        rows=[{'type':'control_response','response':{'request_id':'init-job1','subtype':'success'}},{'type':'result','subtype':'success','is_error':True,'result':'API Error: 502','session_id':self.sid}]
        with patch('claude_connector.subprocess.Popen',return_value=Process(rows)) as popen:self.e.runner('job1')
        result=self.e.execute({'action':'watch','id':'job1'});self.assertEqual(result['status'],'failed');self.assertTrue(result['terminal_confirmed']);self.assertNotIn('--fork-session',popen.call_args.args[0])
    def test_eof_without_result_is_not_success(self):
        self.e.jobs['job1']={'thread_id':self.sid,'prompt':'hello','state':'starting'}
        with patch('claude_connector.subprocess.Popen',return_value=Process([])):self.e.runner('job1')
        self.assertEqual(self.e.jobs['job1']['status'],'failed')
    def test_ask_user_answer_reaches_original_control_request(self):
        self.e.jobs['job1']={'thread_id':self.sid,'state':'waiting_input','pending_questions':[{'call_id':'q1'}]};self.e.inputs['q1']=('job1',{'questions':[{'question':'格式？'}]});self.e.processes['job1']=Process([])
        self.e.execute({'action':'send','thread_id':self.sid,'id':'answer1','auto_reply':True,'text':json.dumps({'call_id':'q1','answers':{'格式？':'JSON'}})})
        wire=json.loads(self.e.processes['job1'].stdin.getvalue());self.assertEqual(wire['response']['request_id'],'q1');self.assertIn('大肥鱼代答，不是jg本人',wire['response']['response']['updatedInput']['answers']['格式？']);self.assertEqual(self.e.jobs['answer1']['answer_to'],'job1')
    def title_transcript(self,rows):
        folder=self.projects/'project';folder.mkdir()
        file=folder/(self.sid+'.jsonl');file.write_text('\n'.join(json.dumps(r,ensure_ascii=False) for r in rows)+'\n',encoding='utf-8')
        return file
    def test_custom_title_after_first_twelve_lines_and_outside_tail_is_found(self):
        rows=[{'type':'progress'}]*80+[{'type':'custom-title','customTitle':'头部真实标题'}]+[{'type':'progress','padding':'x'*400}]*250
        file=self.title_transcript(rows);self.assertGreater(file.stat().st_size,12000)
        self.assertEqual(self.e.metadata(self.sid)['name'],'头部真实标题')
        self.assertEqual(self.e.execute({'action':'list','query':'头部真实标题'})['threads'][0]['thread_id'],self.sid)
    def test_tail_rename_overrides_head_and_route_titles(self):
        self.title_transcript([{'type':'custom-title','customTitle':'最初标题'}]+[{'type':'progress','padding':'x'*400}]*250+[{'type':'custom-title','customTitle':'最后改名'}])
        self.assertEqual(self.e.metadata(self.sid)['name'],'最后改名')
    def test_no_custom_title_keeps_first_user_fallback(self):
        self.e.routes[self.sid]['name']=None
        self.title_transcript([{'type':'user','message':{'content':'用户首句话'}}]+[{'type':'progress','padding':'x'*400}]*250)
        self.assertEqual(self.e.metadata(self.sid)['name'],'用户首句话')
    def test_blank_tail_title_does_not_erase_real_head_title(self):
        self.title_transcript([{'type':'custom-title','customTitle':'保留标题'}]+[{'type':'progress','padding':'x'*400}]*250+[{'type':'custom-title','customTitle':''},{'type':'custom-title','customTitle':None}])
        self.assertEqual(self.e.metadata(self.sid)['name'],'保留标题')
if __name__=='__main__':unittest.main(verbosity=2)
