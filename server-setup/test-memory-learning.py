import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock
from runtime_policy import Runtime
from memory_learning import EXPRESSION_PREFIX,revised_reference


class LearningTests(unittest.TestCase):
    def test_negated_correction_does_not_rewrite_the_correct_value_twice(self):
        self.assertEqual(revised_reference('当前喜欢猫；其他字段不喜欢猫','喜欢猫','不喜欢猫'),'当前不喜欢猫；其他字段不喜欢猫')
        self.assertEqual(revised_reference('不喜欢猫','喜欢猫','不喜欢猫'),'不喜欢猫')

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        root=Path(self.tmp.name);(root/'catalog.json').write_text('[]')
        self.config=root/'settings.json';self.config.write_text(json.dumps({'owner_id':'123','state_dir':str(root),'catalog_file':str(root/'catalog.json'),'group_chat_mode':'members','explicit_internal_note_filter_enabled':True}))
        self.r=Runtime(self.config);self.ctx={'channel':'telegram','chat_id':'123','message_id':'1','session_key':'fixture'}

    def source(self,text,kind='user'):
        return self.r.event('123',kind,text)

    def save(self,text,source,quote,**extra):
        payload=dict(category='interaction',content=text,source_id=source,quote=quote,_context=self.ctx);payload.update(extra)
        return self.r.call('memory_save',payload)

    def test_related_expression_is_small_and_not_a_reply_or_model_call(self):
        source=self.source('别反复核验，小事轻一点')
        for i in range(4):self.save(EXPRESSION_PREFIX+f'情境：被提醒核验太多{i} → 习惯：先接眼前的话，小事轻巧处理。',source,'别反复核验')
        self.r.telegram=Mock()
        result=self.r.inbound({'chat_id':'123','sender_id':'123','message_id':'22','text':'你又在反复核验了'})
        self.assertEqual(result['context'].count('experience'),3)
        self.assertIn('当前相关的表达经验',result['context']);self.r.telegram.assert_not_called()
        result=self.r.inbound({'chat_id':'123','sender_id':'123','message_id':'23','text':'今天香蕉很好吃'})
        self.assertNotIn('表达经验',result.get('context',''))

    def test_expression_does_not_match_generic_words_or_response_advice(self):
        source=self.source('体谅体谅我嘛，饶了我吧。你真的不情愿吗？又赶我走。好吧，逗逗你，真的不应吗')
        for text,quote in [
            ('写完亲密场景要收住时 → 他问不情愿就直接老实答。','你真的不情愿吗？又赶我走'),
            ('jg难受后撒赖、叫妈妈，问真的不应吗 → 别急着转正事。','好吧，逗逗你，真的不应吗'),
            ('忙一天、回话只剩乖鱼鱼、我委屈时 → 闹可以是调味。','体谅体谅我嘛，饶了我吧'),
        ]:self.save(EXPRESSION_PREFIX+text,source,quote)
        for query in ['现在的摘要怎么样，有啥想反馈的直接说...',
                      'soul和user你能改了吧。有重复的去掉。反正这5个每轮注入的不要有重复',
                      '现在好了，你可以改咯','😁😘']:
            with self.subTest(query=query),self.r.db() as db:
                self.assertEqual(self.r.learning.expression_context(db,'123',query),'')

    def test_expression_uses_source_quote_and_informative_query_coverage(self):
        source=self.source('体谅体谅我嘛，饶了我吧')
        self.save(EXPRESSION_PREFIX+'适用情境：忙了一天后委屈 → 收口要快。',source,'体谅体谅我嘛，饶了我吧')
        with self.r.db() as db:
            self.assertIn('experience',self.r.learning.expression_context(db,'123','体谅体谅我嘛，饶了我吧'))
            self.assertEqual(self.r.learning.expression_context(db,'123','编译报错，需要查日志、检查端口、修改配置，忙了一天也得把部署问题处理完'), '')

    def test_expression_selection_is_not_crowded_out_by_ordinary_interactions(self):
        source=self.source('别反复核验，小事轻一点')
        for i in range(14):self.save(f'普通经历{i}：反复核验，小事轻一点。',source,'反复核验')
        for i in range(4):self.save(EXPRESSION_PREFIX+f'情境：核验太多{i} → 习惯：小事轻巧处理。',source,'反复核验')
        with self.r.db() as db:self.assertEqual(self.r.learning.expression_context(db,'123','你又在反复核验了').count('experience'),3)

    def test_expression_selection_skips_retired_and_expired_records(self):
        source=self.source('别反复核验，小事轻一点')
        old=self.save(EXPRESSION_PREFIX+'情境：核验 → 习惯：轻巧处理。',source,'反复核验')
        expired=self.save(EXPRESSION_PREFIX+'情境：核验太多 → 习惯：接话。',source,'反复核验')
        with self.r.db() as db:
            db.execute('UPDATE memories SET active=0 WHERE id=?',(old['id'],))
            db.execute('UPDATE memories SET expires=? WHERE id=?',(time.time()-1,expired['id']))
            self.assertEqual(self.r.learning.expression_context(db,'123','反复核验'),'')

    def test_group_does_not_receive_owner_experience_or_corrections(self):
        source=self.source('核验太多');old=self.save('jg喜欢旧内容',source,'核验')
        self.save('jg喜欢新内容',source,'核验',replace_id=old['id'])
        self.save(EXPRESSION_PREFIX+'情境：核验 → 习惯：轻巧接话',source,'核验')
        self.r.inbound({'chat_id':'-99','sender_id':'123','text':'/bindgroup','message_id':'7'})
        result=self.r.inbound({'chat_id':'-99','sender_id':'888','text':'核验','message_id':'8'})
        self.assertNotIn('jg喜欢新内容',result.get('context',''));self.assertNotIn('表达经验',result.get('context',''))

    def test_replacement_retires_fact_updates_note_with_revision_and_exports_summary_correction(self):
        source=self.source('以前是旧偏好');old=self.save('jg喜欢旧偏好',source,'旧偏好',category='core')
        self.r.notes.write({'path':'USER.md','content':'偏好：jg喜欢旧偏好','expected_revision':self.r.notes.read('USER.md')['revision']},self.ctx,'123')
        historical='昨天的真实记录：jg喜欢旧偏好'
        self.r.notes.write({'path':'MEMORY.md','content':historical,'expected_revision':self.r.notes.read('MEMORY.md')['revision']},self.ctx,'123')
        source=self.source('现在是新偏好');new=self.save('jg喜欢新偏好',source,'新偏好',category='core',replace_id=old['id'])
        self.assertEqual(new['updated_notes'],['USER.md'])
        self.assertIn('新偏好',self.r.notes.read('USER.md')['content']);self.assertEqual(len(self.r.notes.read('USER.md')['versions']),2)
        self.assertEqual(self.r.notes.read('MEMORY.md')['content'],historical)
        found=self.r.call('memory_find',{'query':'旧偏好','_context':self.ctx})
        self.assertNotIn(old['id'],[row['id'] for row in found['memories']])
        self.assertTrue(all(row['content']=='jg喜欢新偏好' for row in found['memories']))
        with self.r.db() as db:self.assertEqual(db.execute('SELECT active FROM memories WHERE id=?',(old['id'],)).fetchone()[0],0)
        data=json.loads((self.r.directory/'corrections.json').read_text(encoding='utf-8'))
        self.assertEqual(data['corrections'][0]['new'],'jg喜欢新偏好')
        self.assertTrue(found['pending_corrections'])

    def test_replacement_chain_targets_latest_value(self):
        s=self.source('A B C');a=self.save('A',s,'A');b=self.save('B',s,'B',replace_id=a['id']);c=self.save('C',s,'C',replace_id=b['id'])
        rows=json.loads((self.r.directory/'corrections.json').read_text(encoding='utf-8'))['corrections']
        self.assertEqual({r['old']:r['new'] for r in rows},{'A':'C','B':'C'})
        self.assertTrue(all(r['new_id']==c['id'] for r in rows))
        with self.r.db() as db:self.assertEqual([r['current'] for r in self.r.learning.correction_rows(db,'123',pending=True)],['C'])

    def test_only_new_exchanges_reviewed_and_arrivals_during_review_stay_pending(self):
        self.source('新增一句');self.source('接住这句','assistant')
        with self.r.db() as db:
            hint=self.r.learning.review_context(db,'123','review-one');batch=db.execute('SELECT through_id FROM memory_review_batches WHERE wake_id=?',('review-one',)).fetchone()[0]
        self.assertIn('新增交流',hint);newer=self.source('整理期间的新话')
        with self.r.db() as db:
            db.execute("INSERT INTO diary_runs(wake_id,day,started,outcome) VALUES('review-one','2026-10-30',?,'completed')",(time.time(),))
            self.assertTrue(self.r.learning.finish_review(db,'review-one'))
            window=self.r.learning.review_window(db,'123')
        self.assertEqual(window['after_id'],batch);self.assertEqual(window['through_id'],newer);self.assertEqual(window['new_exchanges'],1)

    def test_failed_review_does_not_advance_and_restart_preserves_cursor(self):
        self.source('新交流')
        with self.r.db() as db:
            self.r.learning.review_context(db,'123','failed')
            db.execute("INSERT INTO diary_runs(wake_id,day,started,outcome) VALUES('failed','2026-10-30',?,'error')",(time.time(),))
            self.assertFalse(self.r.learning.finish_review(db,'failed'))
        other=Runtime(self.config)
        with other.db() as db:self.assertEqual(other.learning.review_window(db,'123')['after_id'],0)

    def test_existing_ordinary_wake_can_review_without_a_new_timer_and_yield_is_not_completion(self):
        self.source('普通空档整理这句')
        with self.r.db() as db:
            db.execute("INSERT INTO wakes(id,created,day,kind) VALUES('ordinary',?,'2026-10-30','ordinary')",(time.time(),))
            self.r.learning.review_context(db,'123','ordinary')
            self.assertFalse(self.r.learning.finish_review(db,'ordinary','yielded'))
            self.assertTrue(self.r.learning.finish_review(db,'ordinary','completed'))
            self.assertEqual(self.r.learning.review_window(db,'123')['new_exchanges'],0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM wakes').fetchone()[0],1)

    def test_ordinary_conversation_opportunity_is_not_turned_into_routine_memory_work(self):
        self.source('随口的新聊天')
        with self.r.db() as db:
            self.assertEqual(self.r.learning.ordinary_review_context(db,'123','chat-opportunity'),'')
            self.assertEqual(db.execute('SELECT COUNT(*) FROM memory_review_batches').fetchone()[0],0)
            db.execute('UPDATE memory_review_cursor SET updated=? WHERE chat=?',(time.time()-86401,'123'))
            self.assertIn('新增交流',self.r.learning.ordinary_review_context(db,'123','missed-daily-review'))

    def test_unchanged_projection_and_core_do_not_churn_files(self):
        source=self.source('稳定');self.save('稳定事实',source,'稳定',category='core')
        paths=[self.r.directory/name for name in ('core.md','corrections.json')]
        before=[(p.stat().st_ino,p.stat().st_mtime_ns) for p in paths]
        with self.r.db() as db:self.r._write_core(db)
        self.assertEqual(before,[(p.stat().st_ino,p.stat().st_mtime_ns) for p in paths])

    def test_source_evidence_is_still_required(self):
        source=self.source('真实原话')
        with self.assertRaises(ValueError):self.save(EXPRESSION_PREFIX+'想象的习惯',source,'没有说过')

    def test_same_result_id_records_once_but_new_turn_same_words_allowed(self):
        data={'chat_id':'123','message_id':'one','turn':'one','content':'同一句话','delivery_origin':'send_tool'}
        first=self.r.outbound(data);self.r.complete({'ticket':first['ticket'],'status':'sent','message_ids':['88']})
        duplicate=self.r.outbound(data);self.assertTrue(duplicate['skip']);self.assertEqual(duplicate['ticket'],first['ticket'])
        self.assertIn('ticket',self.r.outbound(dict(data,message_id='two',turn='two')))
        with self.r.db() as db:self.assertEqual(db.execute("SELECT COUNT(*) FROM events WHERE msg='88' AND kind='assistant'").fetchone()[0],1)

    def test_silence_and_internal_outputs_do_not_block_explicit_shares(self):
        for text in ('NO_REPLY','内部记录（静默）：这轮先不发。','我先看一眼\nHEARTBEAT_OK'):
            result=self.r.outbound({'chat_id':'123','mode':'background','message_id':'heartbeat:fixture','delivery_origin':'send_tool','content':text})
            self.assertTrue(result.get('skip'))
        share=self.r.outbound({'chat_id':'123','mode':'background','message_id':'share','delivery_origin':'send_tool','content':'刚看到一个挺好笑的视频'})
        self.assertIn('ticket',share)

if __name__=='__main__':unittest.main(verbosity=2)
