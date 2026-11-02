"""X photo provenance, isolation, actual send/cache path and bounded image decoding."""
import contextlib,importlib.util,io,json,subprocess,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import Mock,patch
import x_emotes

BASE=Path(__file__).resolve().parent
def load(name,file):
    spec=importlib.util.spec_from_file_location(name,BASE/file);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module
base=load('x_emote_base','test-fatfish.py')
URL='https://pbs.twimg.com/media/real_photo.jpg'
JPEG=b'\xff\xd8\xff\xe0bounded-normalized-fixture'

class XEmoteTests(unittest.TestCase):
    def setUp(self):
        base.PolicyTests.setUp(self);self.r.settings['platform_read_policy']='model'
    bind_group=base.PolicyTests.bind_group
    def reading(self,chat='123',media=None,tool='x_post_read',**extra):
        post={'id':'1234567890123456789','author':{'screenName':'sample'},'text':'一张图','media':media if media is not None else [{'type':'photo','url':URL}]}
        post.update(extra)
        return self.r.record_reading(chat,tool,{'data':[post]})['record_id']
    def save(self,source=None,ctx=None,**values):
        self.r.settings.update(worker_python='python-fixture',image_worker='image-worker-fixture')
        args=dict(name='偷笑',tags=['好笑'],url=URL,source_id=source or self.reading(),_context=ctx or self.context)
        args.update(values);return self.r.call('emote_save',args)
    @staticmethod
    def converted(command,**kwargs):
        assert '--static-only' in command and '--output' in command
        Path(command[command.index('--output')+1]).write_bytes(JPEG)
        return subprocess.CompletedProcess(command,0,b'{}',b'')
    def test_real_photo_import_find_send_and_telegram_file_cache(self):
        with patch.object(x_emotes.subprocess,'run',side_effect=self.converted):saved=self.save()
        self.assertEqual(saved['source_url'],'https://x.com/sample/status/1234567890123456789')
        found=self.r.call('emote_find',{'query':'偷笑','_context':self.context})
        self.assertEqual(found['emotes'][0]['id'],saved['id'])
        self.r.telegram=Mock(return_value={'message_id':44,'photo':[{'file_id':'cached-photo'}]})
        self.r.call('emote_send',{'id':saved['id'],'_context':self.context})
        first=self.r.telegram.call_args
        self.assertEqual(first.args[0],'sendPhoto');self.assertEqual(first.args[2][0],'photo')
        self.r.call('emote_send',{'id':saved['id'],'_context':dict(self.context,message_id='2')})
        self.assertEqual(self.r.telegram.call_args.args,('sendPhoto',{'chat_id':'123','caption':'','photo':'cached-photo'}))
        self.assertEqual(len(list(self.r.emotes.external.root.glob('*.jpg'))),1)
    def test_only_structured_photo_media_are_sources(self):
        with patch.object(x_emotes.subprocess,'run') as run:
            for media,tool in [([], 'x_post_read'),([{'type':'video','url':URL}],'x_post_read'),([{'type':'animated_gif','url':URL}],'x_post_read'),([{'type':'photo','url':URL}],'web_fetch')]:
                record=self.reading(media=media,tool=tool,text='图链接 '+URL)
                with self.assertRaises(ValueError):self.save(record)
            record=self.r.event('123','user',json.dumps({'tool':'x_post_read','result':{'data':[{'id':'123','media':[{'type':'photo','url':URL}]}]}}))
            with self.assertRaises(ValueError):self.save(record)
            run.assert_not_called()
    def test_other_chats_and_fabricated_records_cannot_grant_sources(self):
        self.bind_group();private=self.reading();group=self.reading('-99')
        ctx=dict(self.context,chat_id='-99')
        with patch.object(x_emotes.subprocess,'run') as run:
            for record,context,extra in [(private,ctx,{}),(private,ctx,{'source_chat_id':'123'}),(group,self.context,{}),(999999,self.context,{})]:
                with self.assertRaises(ValueError):self.save(record,context,**extra)
            run.assert_not_called()
    def test_private_import_of_group_reading_stays_private(self):
        self.bind_group();record=self.reading('-99')
        with patch.object(x_emotes.subprocess,'run',side_effect=self.converted):saved=self.save(record,source_chat_id='-99')
        self.assertEqual(self.r.emotes.catalog('-99'),[])
        self.assertEqual(self.r.emotes.catalog('123')[0]['id'],saved['id'])
    def test_same_image_different_scopes_has_separate_labels_and_ids(self):
        self.bind_group();self.bind_group('-98');group_ctx=dict(self.context,chat_id='-99')
        with patch.object(x_emotes.subprocess,'run',side_effect=self.converted):
            private=self.save(name='private label',tags=['private tag'])
            group=self.save(self.reading('-99'),group_ctx,name='group label',tags=['group tag'])
        self.assertNotEqual(private['id'],group['id']);self.assertEqual(len(list(self.r.emotes.external.root.glob('*.jpg'))),1)
        item=self.r.emotes.catalog('-99')[0];self.assertEqual(item['name'],'group label');self.assertEqual(item['tags'],['group tag'])
        self.assertEqual(self.r.emotes.catalog('-98'),[])
        self.r.telegram=Mock()
        with self.assertRaises(ValueError):self.r.call('emote_send',{'id':private['id'],'_context':group_ctx})
        self.r.telegram.assert_not_called()
    def test_owner_can_forward_private_image_without_implicitly_sharing_library(self):
        self.bind_group()
        with patch.object(x_emotes.subprocess,'run',side_effect=self.converted):saved=self.save()
        self.r.telegram=Mock(return_value={'message_id':45})
        self.r.call('emote_send',{'id':saved['id'],'chat_id':'-99','_context':self.context})
        self.assertEqual(self.r.telegram.call_args.args[1]['chat_id'],'-99');self.assertEqual(self.r.emotes.catalog('-99'),[])
    def test_group_repeat_cannot_revoke_owner_sharing_or_rewrite_global_labels(self):
        self.bind_group();self.bind_group('-98');ctx=dict(self.context,chat_id='-99');record=self.reading('-99')
        with patch.object(x_emotes.subprocess,'run',side_effect=self.converted):saved=self.save(record,ctx)
        self.r.call('emote_update',{'id':saved['id'],'name':'global label','tags':['global tag'],'share_in_groups':True,'_context':self.context})
        with patch.object(x_emotes.subprocess,'run',side_effect=self.converted):self.save(record,ctx,name='local label',tags=['local tag'],share_in_groups=False)
        self.assertEqual(self.r.emotes.catalog('-98')[0]['name'],'global label')
        self.assertEqual(self.r.emotes.catalog('-99')[0]['name'],'local label')
        self.assertTrue(self.r.emotes.catalog('-98')[0]['shared'])
        with self.assertRaises(ValueError):self.r.call('emote_update',{'id':saved['id'],'active':False,'_context':ctx})
    def test_external_update_and_deactivate_work(self):
        with patch.object(x_emotes.subprocess,'run',side_effect=self.converted):saved=self.save()
        self.r.call('emote_update',{'id':saved['id'],'name':'得意','tags':['开心'],'share_in_groups':True,'_context':self.context})
        self.bind_group();self.assertEqual(self.r.emotes.catalog('-99')[0]['name'],'得意')
        self.r.call('emote_update',{'id':saved['id'],'active':False,'_context':self.context})
        self.assertEqual(self.r.emotes.catalog('123'),[])
    def test_duplicate_import_reuses_file_and_scoped_identity(self):
        with patch.object(x_emotes.subprocess,'run',side_effect=self.converted):first=self.save();second=self.save()
        self.assertEqual(first['id'],second['id']);self.assertEqual(len(list(self.r.emotes.external.root.glob('*.jpg'))),1)
    def test_bad_url_or_missing_source_never_launches_worker(self):
        with patch.object(x_emotes.subprocess,'run') as run:
            for url in ('http://pbs.twimg.com/media/a.jpg','https://pbs.twimg.com.evil.test/media/a.jpg','https://user@pbs.twimg.com/media/a.jpg','https://pbs.twimg.com:444/media/a.jpg','https://video.twimg.com/a.mp4','https://pbs.twimg.com/profile_images/a.jpg','https://127.0.0.1/a.jpg'):
                with self.assertRaises(ValueError):self.save(url=url)
            with self.assertRaises(ValueError):self.save(source_id=True)
            with self.assertRaises(ValueError):self.save(message_id='1')
            run.assert_not_called()
    def test_strict_platform_policy_is_not_bypassed_by_import(self):
        self.r.settings['platform_read_policy']='strict'
        with patch.object(x_emotes.subprocess,'run') as run:
            with self.assertRaises(ValueError):self.save()
            run.assert_not_called()
    def test_size_variant_url_matches_real_photo(self):
        with patch.object(x_emotes.subprocess,'run',side_effect=self.converted):saved=self.save(url='https://pbs.twimg.com/media/real_photo?format=jpg&name=large')
        self.assertEqual(saved['status'],'saved')
    def test_timeout_and_conversion_errors_leave_no_files_or_rows(self):
        for effect in (subprocess.TimeoutExpired('worker',25),subprocess.CompletedProcess([],1,b'',b'')):
            with patch.object(x_emotes.subprocess,'run',side_effect=effect if isinstance(effect,Exception) else None,return_value=effect):
                with self.assertRaises(ValueError):self.save()
            self.assertEqual(list(self.r.emotes.external.root.iterdir()),[])
            with self.r.db() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM external_emotes').fetchone()[0],0)
    def test_storage_quota_and_parallel_import_do_not_publish(self):
        with patch.object(x_emotes.subprocess,'run',side_effect=self.converted),patch.object(x_emotes,'MAX_BYTES',1):
            with self.assertRaises(ValueError):self.save()
        self.assertEqual(list(self.r.emotes.external.root.iterdir()),[])
        x_emotes.IMPORT_LOCK.acquire()
        try:
            with patch.object(x_emotes.subprocess,'run') as run:
                with self.assertRaises(ValueError):self.save()
                run.assert_not_called()
        finally:x_emotes.IMPORT_LOCK.release()
    def test_db_failure_cleans_only_new_content_file(self):
        source=self.reading();original=self.r.db;calls=0
        @contextlib.contextmanager
        def fail_insert():
            nonlocal calls
            calls+=1
            if calls==3:raise RuntimeError('fixture write failure')
            with original() as db:yield db
        with patch.object(x_emotes.subprocess,'run',side_effect=self.converted),patch.object(self.r,'db',fail_insert):
            with self.assertRaises(RuntimeError):self.save(source)
        self.assertEqual(list(self.r.emotes.external.root.iterdir()),[])
        with patch.object(x_emotes.subprocess,'run',side_effect=self.converted):self.save(source)
        files=list(self.r.emotes.external.root.glob('*.jpg'));self.assertEqual(len(files),1);calls=0
        with patch.object(x_emotes.subprocess,'run',side_effect=self.converted),patch.object(self.r,'db',fail_insert):
            with self.assertRaises(RuntimeError):self.save(source)
        self.assertEqual(list(self.r.emotes.external.root.glob('*.jpg')),files)
    def test_restore_of_a_referenced_missing_file_is_not_deleted_on_later_db_error(self):
        source=self.reading()
        with patch.object(x_emotes.subprocess,'run',side_effect=self.converted):saved=self.save(source)
        path=Path(self.r.emotes.catalog('123')[0]['path']);path.unlink();original=self.r.db;calls=0
        @contextlib.contextmanager
        def fail_insert():
            nonlocal calls
            calls+=1
            if calls==3:raise RuntimeError('fixture write failure')
            with original() as db:yield db
        with patch.object(x_emotes.subprocess,'run',side_effect=self.converted),patch.object(self.r,'db',fail_insert):
            with self.assertRaises(RuntimeError):self.save(source)
        self.assertTrue(path.is_file());self.assertEqual(self.r.emotes.catalog('123')[0]['id'],saved['id'])

class ReaderGuardTests(unittest.TestCase):
    setUp=base.PolicyTests.setUp
    def test_import_uses_the_existing_media_lock_and_low_memory_guard(self):
        facade=load('x_guard_facade','memory-reader.py');directory=Path(self.tmp.name)
        for name in ('main','upstream'):(directory/name).write_text('fixture-key')
        config=directory/'facade.json';config.write_text(json.dumps({'origin':facade.ORIGIN,'local_token_file':str(directory/'main'),'upstream_key_file':str(directory/'upstream')}))
        self.r.settings['platform_read_policy']='model';self.r.reminders.start=Mock();self.r.emotes.external.save=Mock(return_value={'status':'saved'})
        servers=[]
        class FakeServer:
            def __init__(self,address,handler):self.handler=handler
            def serve_forever(self):pass
        with patch.object(facade.http.server,'ThreadingHTTPServer',FakeServer):facade.serve(runtime=self.r,settings_path=config,on_ready=servers.append)
        def call():
            request=servers[0].handler.__new__(servers[0].handler);request.path='/mcp';request.send=Mock()
            body={'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'emote_save','arguments':{'name':'图','url':URL,'source_id':1,'_context':self.context}}}
            raw=json.dumps(body).encode();request.rfile=io.BytesIO(raw);request.headers={'Authorization':'Bearer fixture-key','Content-Length':str(len(raw))}
            request.do_POST();return request.send.call_args.args[1]['result']
        facade.HEAVY.acquire()
        try:self.assertTrue(call()['isError'])
        finally:facade.HEAVY.release()
        self.r.emotes.external.save.assert_not_called()
        with patch.object(facade.resources,'check_memory',side_effect=ValueError('low memory')):self.assertTrue(call()['isError'])
        self.r.emotes.external.save.assert_not_called()
        with patch.object(facade.resources,'check_memory'):self.assertFalse(call()['isError'])
        self.r.emotes.external.save.assert_called_once()

try:
    import PIL
    image_worker=load('x_emote_image_worker','image_worker.py')
except ImportError:image_worker=None

@unittest.skipIf(image_worker is None,'Pillow worker environment required')
class StaticImageTests(unittest.TestCase):
    def run_worker(self,raw):
        directory=tempfile.TemporaryDirectory();self.addCleanup(directory.cleanup);target=Path(directory.name)/'photo.jpg'
        opener=Mock();opener.open.return_value=io.BytesIO(raw)
        with patch.object(sys,'argv',['image_worker','--url',URL,'--static-only','--output',str(target)]),patch.object(image_worker.urllib.request,'build_opener',return_value=opener),contextlib.redirect_stdout(io.StringIO()):image_worker.main()
        return target
    def test_actual_rgba_decoding_normalizes_and_downsizes(self):
        raw=io.BytesIO();image_worker.Image.new('RGBA',(1500,1333),(1,2,3,128)).save(raw,'PNG')
        target=self.run_worker(raw.getvalue())
        with image_worker.Image.open(target) as image:self.assertEqual(image.format,'JPEG');self.assertLessEqual(max(image.size),1280)
    def test_animation_and_excessive_pixels_are_not_silently_imported(self):
        raw=io.BytesIO();a=image_worker.Image.new('RGB',(20,20),'red');b=image_worker.Image.new('RGB',(20,20),'blue');a.save(raw,'GIF',save_all=True,append_images=[b])
        with self.assertRaises(ValueError):self.run_worker(raw.getvalue())
        raw=io.BytesIO();image_worker.Image.new('RGB',(1500,1500)).save(raw,'PNG')
        with self.assertRaises(ValueError):self.run_worker(raw.getvalue())
    def test_corrupt_and_oversized_downloads_fail(self):
        for raw in (b'not an image',b'x'*(2*1024*1024+1)):
            with self.assertRaises((ValueError,image_worker.Image.UnidentifiedImageError if hasattr(image_worker.Image,'UnidentifiedImageError') else OSError)):self.run_worker(raw)
    def test_redirects_are_not_followed(self):
        self.assertIsNone(image_worker.NoRedirect().redirect_request(None,None,302,'',{},'https://127.0.0.1'))

if __name__=='__main__':unittest.main()
