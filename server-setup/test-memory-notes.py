"""Offline tests of memory scope, durable writes, revisions and resource limits."""
import datetime as dt
import json
import os
from pathlib import Path
import tempfile
import unittest
from runtime_policy import Runtime,UTC8


class NoteTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        root=Path(self.tmp.name);(root/'catalog.json').write_text('[]')
        self.config=root/'settings.json'
        self.config.write_text(json.dumps({'owner_id':'123','state_dir':str(root),'catalog_file':str(root/'catalog.json')}))
        self.r=Runtime(self.config)
        self.ctx={'channel':'telegram','chat_id':'123','message_id':'1','session_key':'telegram:123'}

    def read(self,path='MEMORY.md',context=None):return self.r.call('memory_note_read',{'path':path,'_context':context or self.ctx})
    def write(self,text,path='MEMORY.md',revision=None,**extra):
        if revision is None:revision=self.read(path)['revision']
        return self.r.call('memory_note_write',dict(path=path,content=text,expected_revision=revision,_context=self.ctx,**extra))

    def test_durable_native_file_and_find(self):
        self.write('真实的小插曲：把鲸鱼叫成了金鱼。')
        self.assertIn('金鱼',(self.r.directory/'notes/MEMORY.md').read_text(encoding='utf-8'))
        other=Runtime(self.config)
        self.assertIn('金鱼',other.call('memory_note_read',{'_context':self.ctx})['content'])
        self.assertEqual(len(self.r.call('memory_find',{'query':'金鱼','_context':self.ctx})['notes']),1)
        self.assertEqual(self.r.notes.search(''),[])

    def test_revision_conflict_and_archive(self):
        first=self.write('旧说法')
        self.write('更新后的说法')
        with self.assertRaises(ValueError):self.write('覆盖并发修改',revision=first['revision'])
        with self.r.db() as db:self.assertEqual(db.execute('SELECT previous_content FROM note_versions ORDER BY id DESC LIMIT 1').fetchone()[0],'旧说法')
        self.assertEqual(self.read()['content'],'更新后的说法')

    def test_generated_core_does_not_overwrite_native_notes(self):
        self.write('我自己形成的看法')
        with self.r.db() as db:self.r._write_core(db)
        self.assertEqual(self.read()['content'],'我自己形成的看法')

    @unittest.skipIf(os.name=='nt','POSIX shared file permissions')
    def test_shared_memory_permissions_survive_both_python_writers(self):
        self.write('自己整理的记忆')
        with self.r.db() as db:self.r._write_core(db)
        for path in [self.r.notes.root/'MEMORY.md',self.r.directory/'core.md']:
            self.assertEqual(path.stat().st_mode&0o777,0o660)
        self.write('更新后的记忆')
        self.assertEqual((self.r.notes.root/'MEMORY.md').stat().st_mode&0o777,0o660)
        self.write('原有偏好','USER.md')
        self.assertEqual((self.r.notes.profile/'USER.md').stat().st_mode&0o777,0o660)

    def test_group_cannot_read_write_or_find_notes(self):
        self.write('PRIVATE')
        self.r.inbound({'chat_id':'-99','sender_id':'123','text':'/bindgroup'})
        group=dict(self.ctx,chat_id='-99')
        for name,args in [('memory_note_read',{}),('memory_note_write',{'path':'MEMORY.md','content':'leak','expected_revision':self.read()['revision']}),('memory_find',{'query':'PRIVATE'})]:
            with self.assertRaises(ValueError):self.r.call(name,dict(args,_context=group))

    def test_only_fixed_paths_and_valid_past_dates(self):
        for path in ('../SOUL.md','/etc/passwd','202610/20261101.md','202602/20260230.md','299901/29990101.md','202610\\20261022.md'):
            with self.subTest(path=path),self.assertRaises(ValueError):self.read(path)

    @unittest.skipIf(os.name=='nt','POSIX server symlink boundary')
    def test_symlinks_and_hardlinks_refused(self):
        outside=self.r.directory/'outside';outside.write_text('secret')
        target=self.r.notes.root/'MEMORY.md';target.symlink_to(outside)
        with self.assertRaises(ValueError):self.read()
        target.unlink();os.link(outside,target)
        with self.assertRaises(ValueError):self.read()
        target.unlink()
        (self.r.notes.root/'202610').symlink_to(self.r.directory,target_is_directory=True)
        with self.assertRaises(ValueError):self.read('202610/20261001.md')

    def test_utf8_size_limit_and_daily_path(self):
        self.write('鲸'*2666)
        with self.assertRaises(ValueError):self.write('鲸'*2667)
        today=dt.datetime.now(UTC8).strftime('%Y%m/%Y%m%d.md')
        self.write('当天的真实经历',today)
        text='鲸'*5996+'金鱼约定'
        self.write(text,today)
        self.assertEqual(self.read(today)['content'],text)
        self.assertTrue(any(n['path']==today for n in self.r.notes.search('金鱼约定')))
        with self.assertRaises(ValueError):self.write('鲸'*6001,today)
        self.assertEqual(len(self.read('index')['files']),2)

    def test_source_scope_and_version_retention(self):
        identifier=self.r.event('123','user','一起聊过的事')
        self.write('真实的交流片段',source_ids=[identifier])
        bad=self.r.event('-99','user','群聊')
        with self.assertRaises(ValueError):self.write('混用来源',source_ids=[bad])
        for number in range(8):self.write(str(number))
        with self.r.db() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM note_versions').fetchone()[0],5)

    def test_managed_files_are_real_separate_paths_and_recoverable(self):
        for name in ('USER.md','HEARTBEAT.md'):
            with self.subTest(name=name):
                self.write('原来的偏好或后台计划',name)
                self.write('更新后的内容',name)
                current=self.read(name)
                previous=self.r.call('memory_note_read',{'path':name,'version':current['versions'][0]['id'],'_context':self.ctx})
                self.assertEqual(previous['content'],'原来的偏好或后台计划')
                self.assertTrue(previous['historical'])
                self.assertEqual(previous['current_revision'],current['revision'])
                self.write(previous['content'],name,revision=current['revision'])
                self.assertEqual((self.r.directory/'profile'/name).read_text(encoding='utf-8'),'原来的偏好或后台计划')
                self.assertFalse((self.r.notes.root/name).exists())
                self.assertIn(name,[item['path'] for item in self.read('index')['files']])

    def test_managed_files_block_group_access_and_arbitrary_paths(self):
        self.r.inbound({'chat_id':'-99','sender_id':'123','text':'/bindgroup'})
        group=dict(self.ctx,chat_id='-99')
        for name in ('USER.md','HEARTBEAT.md'):
            self.write('共享使用偏好',name)
            for tool,args in [('memory_note_read',{'path':name}),('memory_note_write',{'path':name,'content':'改群里的偏好','expected_revision':self.read(name)['revision']})]:
                with self.assertRaises(ValueError):self.r.call(tool,dict(args,_context=group))
        for name in ('AGENT.md','SOUL.md','../profile/USER.md','profile/USER.md','user.md','HEARTBEAT.md/../USER.md'):
            with self.subTest(path=name),self.assertRaises(ValueError):self.read(name)

    def test_managed_versions_are_scoped_and_do_not_modify_live_file(self):
        self.write('原偏好','USER.md');self.write('新偏好','USER.md')
        version=self.read('USER.md')['versions'][0]['id']
        for name in ('HEARTBEAT.md','MEMORY.md'):
            with self.assertRaises(ValueError):self.r.call('memory_note_read',{'path':name,'version':version,'_context':self.ctx})
        self.assertEqual(self.read('USER.md')['content'],'新偏好')
        for bad in (0,-1,True,'1'):
            with self.subTest(version=bad),self.assertRaises(ValueError):self.r.notes.read('USER.md',bad)

    def test_profile_recovery_survives_many_later_diary_versions(self):
        self.write('最初的约定','USER.md');self.write('新的约定','USER.md')
        version=self.read('USER.md')['versions'][0]['id']
        with self.r.db() as db:
            for number in range(240):
                db.execute('INSERT INTO note_versions(path,previous_content,revision,new_revision,sources,context,created) VALUES(?,?,?,?,?,?,?)',('audit-'+str(number),'audit','x','y','[]','{}',number))
        self.write('整理日记')
        self.assertEqual(self.r.notes.read('USER.md',version)['content'],'最初的约定')
        with self.r.db() as db:self.assertLessEqual(db.execute('SELECT COUNT(*) FROM note_versions').fetchone()[0],200)

    def test_managed_size_revision_and_background_writes(self):
        for name in ('USER.md','HEARTBEAT.md'):
            with self.subTest(path=name):
                first=self.write('有效的已有约定',name)
                with self.assertRaises(ValueError):self.write(' ',name)
                with self.assertRaises(ValueError):self.write('鲸'*2667,name)
                self.write('新约定',name)
                with self.assertRaises(ValueError):self.write('旧上下文覆盖',name,revision=first['revision'])
                ctx=dict(self.ctx,session_key='heartbeat')
                revision=self.read(name)['revision']
                result=self.r.call('memory_note_write',{'path':name,'content':'整理后的有效约定','expected_revision':revision,'_context':ctx})
                self.assertEqual(result['status'],'saved')

    @unittest.skipIf(os.name=='nt','POSIX profile link boundary')
    def test_managed_symlinks_and_hardlinks_refused(self):
        outside=self.r.directory/'other';outside.write_text('private')
        for name in ('USER.md','HEARTBEAT.md'):
            target=self.r.notes.profile/name
            target.symlink_to(outside)
            with self.assertRaises(ValueError):self.read(name)
            target.unlink();os.link(outside,target)
            with self.assertRaises(ValueError):self.read(name)
            target.unlink()


if __name__=='__main__':unittest.main(verbosity=2)
